"""Главный модуль Telegram-бота.

Запоминает сообщения пользователей в SQLite-базе, обучает на них модель
марковских цепей, генерирует ответы в стиле чата. Поддерживает мутации
текста, генерацию демотиваторов и панель управления.
"""
import os
import re
import time
import random
import logging
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import config
from db import (
    init_db,
    save_message,
    save_media,
    get_all_messages,
    get_recent_messages,
    count_all_messages,
    count_media,
    get_random_media,
    delete_messages,
    get_setting,
    set_setting,
    get_allowed_threads,
    set_allowed_threads,
)
from markov import (
    generate, get_model, invalidate_cache, AUTO_STATE2_THRESHOLD,
    generate_with_start, generate_long, generate_fixed_length, generate_word,
    warmup_caches, _is_garbage,
)
from mutations import mutate
from demotivator import make_demotivator


# ── Логирование ──────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ── Настройки панели управления ──────────────────────────
TUNABLES = {
    "reply":       ("Шанс текстового ответа",   "REPLY_CHANCE"),
    "media":       ("Шанс медиа вместо текста",  "MEDIA_REPLY_CHANCE"),
    "demotivator": ("Шанс авто-демотиватора",    "DEMOTIVATOR_CHANCE"),
    "mutation":    ("Шанс мутации текста",        "MUTATION_CHANCE"),
    "sanity":      ("Разумность генерации",       "SANITY"),
    "context":     ("Шанс ответа в тему",         "CONTEXT_CHANCE"),
}

PANEL_LABELS = {
    "reply":       "Ответ",
    "media":       "Медиа",
    "demotivator": "Демотиватор",
    "mutation":    "Мутация",
    "sanity":      "Разумность",
    "context":     "Контекст",
}

PANEL_HINTS = {
    "reply":       "шанс ответить на сообщение",
    "media":       "шанс прислать стикер/гифку вместо текста",
    "demotivator": "шанс сделать демотиватор из присланного фото",
    "mutation":    "шанс исковеркать текст (замена букв, регистр)",
    "sanity":      "0% — бред по марковским цепям, 100% — копирует реальные сообщения",
    "context":     "0% — случайные ответы, 100% — всегда отвечает по теме",
}

STEP = 5  # шаг изменения настроек в процентах

# Время старта (для аптайма)
_start_time: float = time.time()

# Бот включён глобально
_bot_enabled: bool = True

# Лимит длины сообщения Telegram
TG_MAX_LEN = 4096

# Антиповтор: последние ответы бота, чтобы не спамить одним и тем же
RECENT_REPLIES_MAX = 20
_recent_replies: list[str] = []

# Рейт-лимит: не отвечать чаще чем раз в N секунд
RATE_LIMIT_SEC = 2
_last_reply_time: float = 0.0


# ── Утилиты ──────────────────────────────────────────────

def _chat_key(chat_id: int, key: str) -> str:
    """Ключ настройки для конкретного чата (per-chat настройки)."""
    return f"{chat_id}:{key}"


def _load_settings():
    """Загрузить глобальный флаг включённости при старте."""
    global _bot_enabled
    val = get_setting("bot_enabled")
    if val is not None:
        _bot_enabled = val == "1"


def _is_muted(chat_id: int) -> bool:
    """Замучен ли бот в данном чате."""
    val = get_setting(_chat_key(chat_id, "mute_until"))
    if val is None:
        return False
    mute_until = float(val)
    return mute_until > 0 and time.time() < mute_until


def _get_chance(key: str, chat_id: int) -> float:
    """Получить настройку для чата. Фолбэк: per-chat → глобальная → config."""
    _, attr = TUNABLES[key]
    default = getattr(config, attr)
    val = get_setting(_chat_key(chat_id, key))
    if val is not None:
        return float(val)
    val = get_setting(key)
    if val is not None:
        return float(val)
    return default


def _set_chance(key: str, value: float, chat_id: int):
    """Сохранить настройку для конкретного чата."""
    set_setting(_chat_key(chat_id, key), str(value))


async def _is_admin(update: Update) -> bool:
    """Доступ к админке:
    - в личке — всегда (личный диалог пользователя с ботом);
    - владелец бота (OWNER_ID) — всегда;
    - в группе — только создатель чата (статус creator).
    """
    user = update.effective_user
    if user is None:
        return False
    if user.id == config.OWNER_ID:
        return True
    chat = update.effective_chat
    if chat.type == "private":
        return True
    try:
        member = await chat.get_member(user.id)
    except Exception:
        return False
    return member.status == "creator"


def _trim(text: str) -> str:
    """Обрезает текст до лимита Telegram."""
    if len(text) <= TG_MAX_LEN:
        return text
    return text[:TG_MAX_LEN - 3] + "..."


def _thread(message) -> int | None:
    """Возвращает id темы (для супергрупп с темами) или None."""
    return getattr(message, "message_thread_id", None)


def _remember_topic_name(message):
    """Если в сообщении есть имя темы — кешируем в settings.

    Источники имени:
      1) service-сообщение «тема создана» (forum_topic_created);
      2) service-сообщение «тема переименована» (forum_topic_edited);
      3) reply на «тема создана» — иногда клиенты Telegram подставляют
         его как reply_to_message в первое сообщение темы.
    """
    if message is None or message.chat is None:
        return
    name = None
    created = getattr(message, "forum_topic_created", None)
    edited = getattr(message, "forum_topic_edited", None)
    if created:
        name = created.name
    elif edited and getattr(edited, "name", None):
        name = edited.name
    else:
        reply = getattr(message, "reply_to_message", None)
        if reply is not None:
            reply_created = getattr(reply, "forum_topic_created", None)
            if reply_created:
                name = reply_created.name
    if name:
        thread_id = _thread(message) or 0
        set_setting(f"thread_name:{message.chat.id}:{thread_id}", name)


def _get_topic_name(chat_id: int, thread_id: int) -> str | None:
    """Достаёт кешированное имя темы (None, если ни разу не видели)."""
    return get_setting(f"thread_name:{chat_id}:{thread_id}")


def _is_thread_allowed(message) -> bool:
    """Бот пишет в этой теме?

    В личке (private) — всегда True, ограничение не применяется.
    В группах — по умолчанию False: пока админ не дал `v сиди здесь`
    хотя бы в одной теме, бот молчит везде. После `v сиди здесь` бот
    отвечает только в темах из списка разрешённых.
    """
    chat = message.chat
    if chat.type == "private":
        return True
    allowed = get_allowed_threads(chat.id)
    if not allowed:
        return False  # по умолчанию в группах — молчим
    thread_id = _thread(message) or 0
    return thread_id in allowed


# Команды, которые работают в группе ВСЕГДА, в обход ограничения по темам.
# Это единственный способ для админа разрешить / запретить тему изнутри
# самой темы. Все остальные команды (`v g`, `v c`, `/help`, …) в не-
# разрешённой теме молча игнорируются.
_THREAD_BYPASS_COMMANDS = ("v сиди здесь", "v сиди", "v уходи")


def _thread_gated(handler):
    """Декоратор: если команда пришла из не-разрешённой темы — молча игнорируем.

    Применяется к /start, /help, /reset, /panel, /demotivator и к
    обработчику callback-кнопок панели. В личке всегда пропускает
    (там `_is_thread_allowed` возвращает True). В группах работает
    только в темах, добавленных через `v сиди здесь`.
    """
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if msg is not None and not _is_thread_allowed(msg):
            return  # тихо игнорируем
        return await handler(update, context)
    return wrapped


def _rate_limited() -> bool:
    """Возвращает True, если ответ запрещён рейт-лимитом."""
    global _last_reply_time
    now = time.time()
    if now - _last_reply_time < RATE_LIMIT_SEC:
        return True
    _last_reply_time = now
    return False


def _extract_phrases(text: str, min_words: int = 2) -> list[str]:
    """Извлекает биграммы и триграммы из текста."""
    words = text.lower().split()
    phrases = []
    for length in range(min_words, min(len(words) + 1, min_words + 2)):
        for i in range(len(words) - length + 1):
            phrases.append(" ".join(words[i:i + length]))
    return phrases


def _is_duplicate(text: str) -> bool:
    """Проверяет, не повторяется ли бот."""
    normalized = text.lower().strip()
    for recent in _recent_replies:
        if normalized == recent or normalized in recent or recent in normalized:
            return True
    if not _recent_replies:
        return False
    phrases = _extract_phrases(normalized)
    if not phrases:
        return False
    recent_text = " | ".join(_recent_replies)
    hits = sum(1 for p in phrases if p in recent_text)
    return hits / len(phrases) > 0.5


def _remember_reply(text: str):
    normalized = text.lower().strip()
    _recent_replies.append(normalized)
    if len(_recent_replies) > RECENT_REPLIES_MAX:
        _recent_replies.pop(0)


def _try_generate_reply(user_text: str, chat_id: int) -> str | None:
    """Сгенерировать ответ. Возвращает None, если все попытки — дубликаты."""
    count = count_all_messages()
    model = get_model(count, get_all_messages)
    if model is None:
        return None
    messages = get_all_messages()
    sanity = _get_chance("sanity", chat_id)
    context_chance = _get_chance("context", chat_id)

    recent = get_recent_messages(8)

    if random.random() < context_chance and user_text:
        parts = recent[-4:] + [user_text] if recent else [user_text]
        context = " ".join(parts)
    else:
        context = ""

    recent_texts = recent + ([user_text] if user_text else [])
    mutation_chance = _get_chance("mutation", chat_id)

    for _ in range(config.MAX_ATTEMPTS):
        candidate = generate(model, context=context, messages=messages,
                             sanity=sanity, recent_texts=recent_texts)
        if candidate is None:
            continue
        if _is_duplicate(candidate):
            continue
        _remember_reply(candidate)
        return mutate(candidate, chance=mutation_chance)

    # Все кандидаты — дубликаты. Сбрасываем буфер и пробуем ещё раз.
    if _recent_replies:
        logger.info("Антиповтор забит, сброс буфера")
        _recent_replies.clear()
        candidate = generate(model, context=context, messages=messages,
                             sanity=sanity, recent_texts=recent_texts)
        if candidate:
            _remember_reply(candidate)
            return mutate(candidate, chance=mutation_chance)
    return None


# ── Команды (/start, /help, /reset, /panel, /demotivator) ────

@_thread_gated
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = count_all_messages()
    remaining = max(0, config.MIN_MESSAGES - count)
    if remaining > 0:
        text = (
            f"Привет! Это {config.BOT_USERNAME}. Я учусь на сообщениях чата и "
            f"потом отвечаю в его стиле.\n\n"
            f"Сейчас у меня {count} сообщений. "
            f"Нужно ещё {remaining}, чтобы я начал генерировать ответы."
        )
    else:
        text = (
            f"Привет! Это {config.BOT_USERNAME}. У меня уже {count} сообщений — "
            f"могу генерировать ответы. Просто пишите в чат."
        )
    await update.message.reply_text(text)


@_thread_gated
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Команды:</b>\n\n"
        "<b>Генерация:</b>\n"
        "<code>v g</code> — сгенерировать текст\n"
        "<code>v g</code> <i>начало</i> — текст с заданным началом\n"
        "<code>v g</code> <i>число</i> — текст из N слов (1–250)\n"
        "<code>v g l</code> — длинный текст (несколько предложений)\n"
        "<code>v g w</code> — случайное слово из корпуса\n"
        "<code>v g w</code> <i>число</i> — слово длиной N букв\n"
        "<code>v g p</code> — опрос из сгенерированных вариантов\n"
        "<code>v g d</code> — демотиватор (ответом на фото)\n"
        "\n<b>Управление (только админ):</b>\n"
        "<code>v c</code> или /panel — панель настроек\n"
        "<code>v тихо</code> <i>минуты</i> — замутить бота\n"
        "<code>v говори</code> — снять мут\n"
        "<code>v set</code> <i>ключ</i> <i>значение</i> — изменить настройку\n"
        "<code>v сиди здесь</code> — отвечать только в этой теме (группы с темами)\n"
        "<code>v уходи</code> — убрать тему из разрешённых\n"
        "<code>v где сижу</code> — список разрешённых тем\n"
        "/reset — удалить свои сообщения из корпуса\n"
        "<code>v пинг</code> — проверка, что бот жив"
    )
    await update.message.reply_text(text, parse_mode="HTML")


@_thread_gated
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update):
        await update.message.reply_text("Команда доступна только администратору.")
        return
    user_id = update.effective_user.id
    delete_messages(user_id)
    invalidate_cache()
    await update.message.reply_text("Твои сообщения удалены из корпуса.")


# ── Панель управления ────────────────────────────────────

def _progress_bar(value: float, length: int = 10) -> str:
    filled = round(value * length)
    return "▰" * filled + "▱" * (length - filled)


def _build_panel_text(chat_id: int) -> str:
    msg_count = count_all_messages()
    med_count = count_media()
    state = 2 if msg_count >= AUTO_STATE2_THRESHOLD else 1
    remaining = max(0, config.MIN_MESSAGES - msg_count)

    status = "🟢 Бот включён" if _bot_enabled else "🔴 Бот выключен (сообщения всё равно запоминаются)"
    if _is_muted(chat_id):
        mute_val = get_setting(_chat_key(chat_id, "mute_until"))
        if mute_val:
            remaining_min = int((float(mute_val) - time.time()) / 60) + 1
            status += f"\n🤐 Замучен ещё ~{remaining_min} мин."

    lines = [f"<b>📊 {config.BOT_USERNAME} — панель управления</b>\n{status}\n"]
    lines.append(f"💬 Сообщений: <b>{msg_count}</b>  |  🎭 Медиа: <b>{med_count}</b>")
    if state == 1:
        lines.append(f"🧠 Модель: state_size=1 (→2 после {AUTO_STATE2_THRESHOLD})")
    else:
        lines.append("🧠 Модель: state_size=2")
    if remaining > 0:
        lines.append(f"⏳ До генерации нужно ещё: <b>{remaining}</b>")
    else:
        lines.append("✅ Генерация активна")
    lines.append("")

    for key in TUNABLES:
        val = _get_chance(key, chat_id)
        pct = int(val * 100)
        bar = _progress_bar(val)
        label = PANEL_LABELS[key]
        hint = PANEL_HINTS.get(key, "")
        lines.append(f"{bar}  <b>{pct:>3}%</b>  {label}")
        if hint:
            lines.append(f"  <i>{hint}</i>")
    return "\n".join(lines)


def _build_panel_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    cid = str(chat_id)
    toggle_text = "🔴 Выключить" if _bot_enabled else "🟢 Включить"
    rows.append([InlineKeyboardButton(toggle_text, callback_data=f"toggle:{cid}")])
    for key in TUNABLES:
        label = PANEL_LABELS[key]
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"dec:{key}:{cid}"),
            InlineKeyboardButton(label, callback_data="noop"),
            InlineKeyboardButton("➕", callback_data=f"inc:{key}:{cid}"),
        ])
    rows.append([
        InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh:{cid}"),
        InlineKeyboardButton("✅ Готово", callback_data="done"),
    ])
    return InlineKeyboardMarkup(rows)


@_thread_gated
async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update):
        await update.message.reply_text("Команда доступна только администратору.")
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        _build_panel_text(chat_id),
        reply_markup=_build_panel_keyboard(chat_id),
        parse_mode="HTML",
    )


@_thread_gated
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not await _is_admin(update):
        await query.answer("Команда доступна только администратору.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "noop":
        return
    if data == "done":
        await query.delete_message()
        return

    parts = data.split(":")
    action = parts[0]

    if action == "toggle":
        chat_id = int(parts[1])
        global _bot_enabled
        _bot_enabled = not _bot_enabled
        set_setting("bot_enabled", "1" if _bot_enabled else "0")
        await query.edit_message_text(
            _build_panel_text(chat_id),
            reply_markup=_build_panel_keyboard(chat_id),
            parse_mode="HTML",
        )
        return

    if action == "refresh":
        chat_id = int(parts[1])
        await query.edit_message_text(
            _build_panel_text(chat_id),
            reply_markup=_build_panel_keyboard(chat_id),
            parse_mode="HTML",
        )
        return

    # inc:key:chat_id / dec:key:chat_id
    if action not in ("inc", "dec") or len(parts) < 3:
        return
    key = parts[1]
    chat_id = int(parts[2])
    if key not in TUNABLES:
        return

    current = _get_chance(key, chat_id)
    pct = int(current * 100)
    if action == "inc":
        pct = min(100, pct + STEP)
    else:
        pct = max(0, pct - STEP)
    _set_chance(key, pct / 100.0, chat_id)
    await query.edit_message_text(
        _build_panel_text(chat_id),
        reply_markup=_build_panel_keyboard(chat_id),
        parse_mode="HTML",
    )


# ── Демотиваторы ─────────────────────────────────────────

def _make_demotivator_sync(photo_bytes: bytes, chat_id: int = 0) -> bytes | None:
    """Синхронная генерация демотиватора (вызывается в thread)."""
    count = count_all_messages()
    model = get_model(count, get_all_messages)
    if model is None:
        return None
    messages = get_all_messages()
    title = generate(model, messages=messages)
    if title is None:
        return None
    title = mutate(title, chance=_get_chance("mutation", chat_id))
    return make_demotivator(photo_bytes, title)


@_thread_gated
async def cmd_demotivator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update):
        await update.message.reply_text("Команда доступна только администратору.")
        return
    msg = update.message
    reply = msg.reply_to_message

    if not reply or not reply.photo:
        await msg.reply_text("Ответь этой командой на фотографию, и я сделаю демотиватор.")
        return

    count = count_all_messages()
    if count < config.MIN_MESSAGES:
        await msg.reply_text("В корпусе мало сообщений для подписи.")
        return

    photo_file = await reply.photo[-1].get_file()
    photo_bytes = bytes(await photo_file.download_as_bytearray())
    result = await asyncio.to_thread(_make_demotivator_sync, photo_bytes, msg.chat.id)
    if result is None:
        await msg.reply_text("Не получилось сгенерировать подпись, попробуй ещё раз.")
        return
    await msg.reply_photo(photo=result)


# ── Хэндлеры медиа-сообщений ─────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return
    user_id = update.effective_user.id
    file_id = update.message.photo[-1].file_id
    save_media(user_id, file_id, "photo")

    if not _bot_enabled:
        return
    if not _is_thread_allowed(update.message):
        return
    if random.random() > _get_chance("demotivator", update.message.chat.id):
        return
    count = count_all_messages()
    if count < config.MIN_MESSAGES:
        return

    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = bytes(await photo_file.download_as_bytearray())
        result = await asyncio.to_thread(
            _make_demotivator_sync, photo_bytes, update.message.chat.id)
        if result is None:
            return
        await update.message.chat.send_action(action=ChatAction.UPLOAD_PHOTO)
        await update.message.chat.send_photo(photo=result,
                                             message_thread_id=_thread(update.message))
    except Exception as e:
        logger.warning(f"Ошибка авто-демотиватора: {e}")


async def _reply_with_random(message) -> bool:
    """Ответить случайным медиа или сгенерированным текстом."""
    chat = message.chat
    thread_id = _thread(message)
    if random.random() < _get_chance("media", chat.id):
        media = get_random_media()
        if media:
            file_id, kind = media
            if kind == "sticker":
                await chat.send_sticker(sticker=file_id, message_thread_id=thread_id)
            elif kind == "gif":
                await chat.send_animation(animation=file_id, message_thread_id=thread_id)
            return True
    count = count_all_messages()
    if count >= config.MIN_MESSAGES:
        reply = await asyncio.to_thread(_try_generate_reply, "", chat.id)
        if reply:
            await chat.send_message(text=_trim(reply), message_thread_id=thread_id)
            return True
    return False


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.sticker:
        return
    user_id = update.effective_user.id
    file_id = update.message.sticker.file_id
    save_media(user_id, file_id, "sticker")

    if not _bot_enabled:
        return
    if not _is_thread_allowed(update.message):
        return
    if random.random() <= _get_chance("reply", update.message.chat.id):
        await _reply_with_random(update.message)


async def handle_animation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.animation:
        return
    user_id = update.effective_user.id
    file_id = update.message.animation.file_id
    save_media(user_id, file_id, "gif")

    if not _bot_enabled:
        return
    if not _is_thread_allowed(update.message):
        return
    if random.random() <= _get_chance("reply", update.message.chat.id):
        await _reply_with_random(update.message)


# ── Текстовые команды «v ...» ────────────────────────────

_KEY_ALIASES = {
    "ответ": "reply", "reply": "reply",
    "медиа": "media", "media": "media",
    "демотиватор": "demotivator", "demotivator": "demotivator", "демо": "demotivator",
    "мутация": "mutation", "mutation": "mutation", "мут": "mutation",
    "разумность": "sanity", "sanity": "sanity",
    "контекст": "context", "context": "context",
}

_V_PREFIXES = ("v g", "v c", "v тихо", "v тише", "v говори",
               "v пинг", "v set", "v settings", "v настройки",
               "v сиди", "v уходи", "v где")


async def _handle_v_command(update: Update, text: str,
                            context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обработка текстовых команд вида «v g», «v c», «v set ...».
    Возвращает True, если команда обработана."""
    msg = update.message
    lower = text.strip().lower()

    # v пинг — проверка, что бот жив
    if lower == "v пинг":
        uptime = int(time.time() - _start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, secs = divmod(remainder, 60)
        await msg.reply_text(f"понг 🏓 аптайм: {hours}ч {minutes}м {secs}с")
        return True

    # v c — открыть панель
    if lower == "v c":
        if not await _is_admin(update):
            await msg.reply_text("Команда доступна только администратору.")
        else:
            chat_id = msg.chat.id
            await msg.reply_text(
                _build_panel_text(chat_id),
                reply_markup=_build_panel_keyboard(chat_id),
                parse_mode="HTML",
            )
        return True

    # v тихо [минуты] — замутить бота в этом чате
    if lower.startswith(("v тихо", "v тише")):
        if not await _is_admin(update):
            await msg.reply_text("Команда доступна только администратору.")
            return True
        parts = lower.split()
        minutes = 30
        if len(parts) >= 3 and parts[2].isdigit():
            minutes = max(1, min(1440, int(parts[2])))
        mute_until = time.time() + minutes * 60
        set_setting(_chat_key(msg.chat.id, "mute_until"), str(mute_until))
        await msg.reply_text(f"🤐 Молчу {minutes} мин.")
        return True

    # v говори — снять мут досрочно
    if lower == "v говори":
        if not await _is_admin(update):
            await msg.reply_text("Команда доступна только администратору.")
            return True
        set_setting(_chat_key(msg.chat.id, "mute_until"), "0")
        await msg.reply_text("🗣 Снова говорю.")
        return True

    # v сиди здесь — разрешить боту писать в этой теме (и больше нигде в этом чате)
    if lower in ("v сиди здесь", "v сиди"):
        if not await _is_admin(update):
            await msg.reply_text("Команда доступна только администратору.")
            return True
        if msg.chat.type == "private":
            await msg.reply_text("Эта команда — для групп с темами.")
            return True
        thread_id = _thread(msg) or 0
        allowed = get_allowed_threads(msg.chat.id)
        if thread_id in allowed:
            await msg.reply_text("Я уже сижу здесь.")
        else:
            allowed.add(thread_id)
            set_allowed_threads(msg.chat.id, allowed)
            await msg.reply_text(
                f"Сижу здесь. 👍 Всего разрешённых тем: {len(allowed)}."
            )
        return True

    # v уходи — убрать эту тему из разрешённых (если других нет — бот снова везде)
    if lower == "v уходи":
        if not await _is_admin(update):
            await msg.reply_text("Команда доступна только администратору.")
            return True
        if msg.chat.type == "private":
            await msg.reply_text("Эта команда — для групп с темами.")
            return True
        thread_id = _thread(msg) or 0
        allowed = get_allowed_threads(msg.chat.id)
        if thread_id not in allowed:
            await msg.reply_text("Меня тут и так не было.")
        else:
            allowed.discard(thread_id)
            set_allowed_threads(msg.chat.id, allowed)
            if allowed:
                await msg.reply_text(
                    f"Ушёл. Осталось разрешённых тем: {len(allowed)}."
                )
            else:
                await msg.reply_text(
                    "Ушёл. Список разрешённых тем пуст — молчу везде, "
                    "пока не дашь `v сиди здесь` в нужной теме."
                )
        return True

    # v где сижу — показать список разрешённых тем
    if lower in ("v где сижу", "v где"):
        if msg.chat.type == "private":
            await msg.reply_text("Это диалог в личке, тем тут нет.")
            return True
        allowed = get_allowed_threads(msg.chat.id)
        if not allowed:
            await msg.reply_text(
                "Список разрешённых тем пуст — молчу везде. "
                "Дай `v сиди здесь` в нужной теме, чтобы я там заговорил."
            )
        else:
            parts = []
            for t in sorted(allowed):
                if t == 0:
                    parts.append("общий чат")
                    continue
                name = _get_topic_name(msg.chat.id, t)
                if name:
                    parts.append(f"«{name}» (#{t})")
                else:
                    parts.append(f"#{t} (имя не известно)")
            await msg.reply_text(f"Разрешённые темы: {', '.join(parts)}")
        return True

    # v set <ключ> <значение> — изменить настройку
    set_match = re.match(r"^v\s+set\s+(\w+)\s+(\d+)$", lower)
    if set_match:
        key = set_match.group(1)
        value = int(set_match.group(2))
        resolved_key = _KEY_ALIASES.get(key)
        if not resolved_key or resolved_key not in TUNABLES:
            await msg.reply_text(
                f"Неизвестная настройка: <b>{key}</b>.\n"
                f"Доступные: {', '.join(TUNABLES.keys())}",
                parse_mode="HTML",
            )
            return True

        if not await _is_admin(update):
            await msg.reply_text("Команда доступна только администратору.")
            return True

        value = max(0, min(100, value))
        chat_id = msg.chat.id
        _set_chance(resolved_key, value / 100.0, chat_id)
        label = PANEL_LABELS[resolved_key]
        hint = PANEL_HINTS.get(resolved_key, "")
        await msg.reply_text(
            f"✅ <b>{label}</b> → <b>{value}%</b>\n<i>{hint}</i>",
            parse_mode="HTML",
        )
        return True

    # v set / v settings — показать все текущие значения
    if lower.strip() in ("v set", "v settings", "v настройки"):
        chat_id = msg.chat.id
        lines = ["<b>⚙️ Текущие настройки:</b>\n"]
        for key in TUNABLES:
            val = _get_chance(key, chat_id)
            pct = int(val * 100)
            label = PANEL_LABELS[key]
            lines.append(f"  <b>{pct:>3}%</b>  {label}  (<code>v set {key} {pct}</code>)")
        lines.append("\n<i>Пример: </i><code>v set sanity 80</code>")
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
        return True

    # Всё остальное — это варианты команды v g ...
    if not lower.startswith("v g"):
        return False

    count = count_all_messages()
    if count < config.MIN_MESSAGES:
        await msg.reply_text(f"В корпусе мало сообщений. Нужно ещё {config.MIN_MESSAGES - count}.")
        return True

    model = get_model(count, get_all_messages)
    if model is None:
        await msg.reply_text("Не получилось собрать модель.")
        return True

    args = text.strip()[3:].strip()
    args_lower = args.lower()
    await msg.chat.send_action(action=ChatAction.TYPING)

    # v g d — демотиватор (по реплаю на фото или случайному из БД)
    if args_lower == "d":
        reply_msg = msg.reply_to_message
        if reply_msg and reply_msg.photo:
            photo_file = await reply_msg.photo[-1].get_file()
            photo_bytes = bytes(await photo_file.download_as_bytearray())
        else:
            media = get_random_media("photo")
            if media is None:
                await msg.reply_text("Нет фотографий в базе. Пришлите фото в чат.")
                return True
            file_id, _ = media
            try:
                photo_file = await context.bot.get_file(file_id)
                photo_bytes = bytes(await photo_file.download_as_bytearray())
            except Exception as e:
                logger.warning(f"Не удалось скачать фото {file_id}: {e}")
                await msg.reply_text("Не удалось получить фотографию.")
                return True
        result = await asyncio.to_thread(_make_demotivator_sync, photo_bytes, msg.chat.id)
        if result:
            await msg.reply_photo(photo=result)
        else:
            await msg.reply_text("Не получилось.")
        return True

    # v g p — опрос из сгенерированных вариантов
    if args_lower == "p":
        msgs = get_all_messages()
        question = generate(model, messages=msgs)
        if not question:
            await msg.reply_text("Не получилось сгенерировать вопрос.")
            return True
        options = []
        for _ in range(20):
            opt = generate(model, messages=msgs)
            if opt and opt != question and opt not in options and len(opt) <= 100:
                options.append(opt)
            if len(options) >= 4:
                break
        if len(options) < 2:
            options = ["да", "нет"]
        await msg.reply_poll(
            question=question[:300],
            options=options[:10],
            is_anonymous=True,
        )
        return True

    # v g w [число] — случайное слово из корпуса
    if args_lower.startswith("w"):
        w_args = args_lower[1:].strip()
        word_len = None
        if w_args.isdigit():
            word_len = max(1, min(50, int(w_args)))
        word = generate_word(get_all_messages(), word_len)
        await msg.reply_text(word or "не нашёл")
        return True

    mutation_chance = _get_chance("mutation", msg.chat.id)

    # v g l — длинный текст
    if args_lower == "l":
        result = await asyncio.to_thread(generate_long, model, 8)
        if result:
            result = mutate(result, chance=mutation_chance)
        await msg.reply_text(_trim(result) if result else "Не получилось.")
        return True

    # v g <число> — текст фиксированной длины
    if args_lower.isdigit():
        n = max(1, min(250, int(args_lower)))
        result = await asyncio.to_thread(generate_fixed_length, model, n)
        if result:
            result = mutate(result, chance=mutation_chance)
        await msg.reply_text(_trim(result) if result else "Не получилось.")
        return True

    # v g <начало> — текст с заданным началом
    if args:
        result = await asyncio.to_thread(
            generate_with_start, model, args, get_all_messages()
        )
        if result:
            result = mutate(result, chance=mutation_chance)
        await msg.reply_text(_trim(result) if result else "Не получилось.")
        return True

    # v g — обычная генерация
    result = await asyncio.to_thread(_try_generate_reply, "", msg.chat.id)
    await msg.reply_text(_trim(result) if result else "Не получилось.")
    return True


# ── Service-сообщения о темах (создание / переименование) ─

async def handle_topic_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запоминает имя темы при создании/переименовании.

    Молчит (не отвечает в чат) — просто обновляет кеш `thread_name:*`
    в settings, чтобы `v где сижу` мог показать имя вместо ID.
    """
    _remember_topic_name(update.message)


# ── Главный обработчик текстовых сообщений ───────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # Пробуем выудить имя темы из любого приходящего сообщения
    # (бывает в reply_to_message.forum_topic_created у первого сообщения темы).
    _remember_topic_name(update.message)

    user_id = update.effective_user.id
    user_text = update.message.text
    lower = user_text.strip().lower()

    chat = update.effective_chat
    chat_label = f"{chat.type}/{chat.id}"
    preview = user_text[:60].replace("\n", " ")
    logger.info(f"[msg] {chat_label} user={user_id}: {preview!r}")

    # Не сохраняем в корпус: команды, упоминания бота, мусорные сообщения
    is_forward = re.search(r"\[\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\]", user_text)
    has_bot_mention = f"@{config.BOT_USERNAME}" in user_text
    skip = (
        lower.startswith(_V_PREFIXES)
        or is_forward
        or has_bot_mention
        or _is_garbage(user_text)
    )
    if not skip:
        save_message(user_id, user_text)

    if not _bot_enabled:
        return

    # В не-разрешённой теме разрешены только bypass-команды управления темами.
    # Всё остальное (включая `v g`, `v c`, обычные сообщения) молча игнорим.
    is_bypass = lower in _THREAD_BYPASS_COMMANDS
    if not _is_thread_allowed(update.message) and not is_bypass:
        return

    # Текстовые команды «v ...» работают даже в режиме мута
    if await _handle_v_command(update, user_text, context):
        return

    # В режиме мута на остальные сообщения не отвечаем
    if _is_muted(update.message.chat.id):
        return

    # Прямое упоминание @<bot> — отвечаем всегда
    if has_bot_mention:
        await update.message.chat.send_action(action=ChatAction.TYPING)
        reply = await asyncio.to_thread(_try_generate_reply, user_text, update.message.chat.id)
        if reply:
            await update.message.chat.send_message(
                text=_trim(reply),
                message_thread_id=_thread(update.message),
            )
        return

    # Иначе — с заданной вероятностью
    chance = _get_chance("reply", update.message.chat.id)
    if random.random() > chance:
        return

    if _rate_limited():
        return

    count = count_all_messages()
    if count < config.MIN_MESSAGES:
        remaining = config.MIN_MESSAGES - count
        await update.message.reply_text(
            f"Я ещё учусь. Нужно ещё {remaining} сообщений."
        )
        return

    # Сначала пробуем ответить медиа
    if random.random() < _get_chance("media", update.message.chat.id):
        media = get_random_media()
        if media:
            file_id, kind = media
            thread_id = _thread(update.message)
            if kind == "sticker":
                await update.message.chat.send_sticker(
                    sticker=file_id, message_thread_id=thread_id)
            elif kind == "gif":
                await update.message.chat.send_animation(
                    animation=file_id, message_thread_id=thread_id)
            return

    try:
        reply = await asyncio.wait_for(
            asyncio.to_thread(_try_generate_reply, user_text, update.message.chat.id),
            timeout=30,
        )
    except asyncio.TimeoutError:
        logger.warning("Генерация зависла (таймаут 30 с)")
        return

    if reply:
        await update.message.chat.send_action(action=ChatAction.TYPING)
        reply_text = _trim(reply)
        logger.info(f"[reply] {chat_label}: {reply_text[:60]!r}")
        await update.message.chat.send_message(
            text=reply_text,
            message_thread_id=_thread(update.message),
        )


# ── Обработчик ошибок ────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Ловит любые исключения, чтобы бот не падал. Печатает полный контекст."""
    import traceback

    err = context.error
    err_type = type(err).__name__
    err_msg = str(err) or "(нет описания)"

    # Информация об update — что именно вызвало ошибку
    update_info = "no update"
    if isinstance(update, Update):
        try:
            chat = update.effective_chat
            user = update.effective_user
            msg = update.effective_message
            parts = []
            if chat:
                parts.append(f"chat={chat.id} ({chat.type})")
            if user:
                parts.append(f"user={user.id} (@{user.username})")
            if msg and msg.text:
                preview = msg.text[:80].replace("\n", " ")
                parts.append(f"text={preview!r}")
            update_info = ", ".join(parts) if parts else "empty update"
        except Exception:
            update_info = "(не удалось распарсить update)"

    logger.warning(f"Ошибка {err_type}: {err_msg}  |  {update_info}")

    # Полный traceback на уровне DEBUG (включается через config.ini verbose=true)
    tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    logger.debug("Полный traceback:\n%s", tb)


# ── Запуск ───────────────────────────────────────────────

def main():
    if not config.TELEGRAM_TOKEN:
        raise SystemExit(
            "Не задан TELEGRAM_TOKEN. Установите переменную окружения "
            "TELEGRAM_TOKEN или впишите токен в config.py."
        )

    init_db()
    _load_settings()

    # Предсобираем модель и прогреваем все кэши (pymorphy3, IDF, леммы
    # каждого сообщения корпуса). Иначе первое контекстное сообщение
    # пользователю висит 30–60 секунд на холодной лемматизации.
    count = count_all_messages()
    if count >= config.MIN_MESSAGES:
        logger.info(f"Предсборка модели Маркова ({count} сообщений)...")
        get_model(count, get_all_messages)
        warmup_caches(get_all_messages())
        logger.info("Модель и кэши готовы.")

    builder = ApplicationBuilder().token(config.TELEGRAM_TOKEN)
    if config.PROXY_URL:
        builder = builder.proxy(config.PROXY_URL).get_updates_proxy(config.PROXY_URL)
        logger.info(f"Используется прокси: {config.PROXY_URL}")
    # Подкручиваем сетевые таймауты — через прокси Telegram часто
    # отвечает дольше дефолтных 5 секунд.
    builder = builder.connect_timeout(15.0).read_timeout(30.0).write_timeout(30.0)
    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("demotivator", cmd_demotivator))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_handler(MessageHandler(filters.ANIMATION, handle_animation))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    # Service-сообщения о темах форума: кешируем имя темы для `v где сижу`.
    app.add_handler(MessageHandler(
        filters.StatusUpdate.FORUM_TOPIC_CREATED
        | filters.StatusUpdate.FORUM_TOPIC_EDITED,
        handle_topic_service,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
