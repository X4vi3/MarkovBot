"""Конфигурация бота. Читает значения из config.ini.

Два сценария запуска:
  - `python bot.py` — этот модуль сам открывает config.ini рядом и
    подтягивает токен / прокси / OWNER_ID.
  - `MarkovBot.exe`  — лаунчер внутри .exe заранее выставляет переменные
    окружения с теми же именами; модуль их подхватит как приоритетные
    (чтобы мастер первого запуска работал, не трогая config.ini ручками).

Токены в коде НЕ хранятся.
"""
import os
import sys
import configparser


def _config_ini_path() -> str:
    """Путь к config.ini: рядом с .exe для frozen-сборки, иначе рядом с config.py."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "config.ini")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")


def _load_ini() -> dict:
    """Читает config.ini и возвращает плоский dict значений."""
    path = _config_ini_path()
    if not os.path.exists(path):
        return {}
    cp = configparser.ConfigParser()
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        return {}
    out = {}
    if cp.has_section("telegram"):
        out["TELEGRAM_TOKEN"] = cp.get("telegram", "token", fallback="").strip()
        out["BOT_USERNAME"] = cp.get("telegram", "username", fallback="").strip()
        out["OWNER_ID"] = cp.get("telegram", "owner_id", fallback="0").strip()
    if cp.has_section("proxy"):
        out["PROXY_URL"] = cp.get("proxy", "url", fallback="").strip()
    return out


_INI = _load_ini()


def _get(name: str, default: str = "") -> str:
    """Возвращает значение: сначала env, потом config.ini, иначе default."""
    val = os.environ.get(name)
    if val:
        return val
    return _INI.get(name, default)


# --- Подключение ---
TELEGRAM_TOKEN = _get("TELEGRAM_TOKEN", "")
BOT_USERNAME = _get("BOT_USERNAME", "")

# Telegram user_id владельца бота. Имеет доступ к админке в любом чате.
# В каждом конкретном чате доступ к админке также имеет создатель чата
# (определяется через Telegram API). 0 = «нет владельца, только создатели чатов».
OWNER_ID = int(_get("OWNER_ID", "0") or "0")

# Прокси для подключения к Telegram API.
# Пустая строка — без прокси (прямое подключение).
# Пример SOCKS5: "socks5://127.0.0.1:10808" (v2rayN, Nekoray и т.п.)
# Пример HTTP:   "http://127.0.0.1:10809"
# Sentinel "direct" в config.ini пишет лаунчер, когда авто-детект
# подтвердил прямое подключение. Для bot.py его надо превратить
# в пустую строку — иначе python-telegram-bot попробует использовать
# "direct" как реальный URL и упадёт.
PROXY_URL = _get("PROXY_URL", "")
if PROXY_URL.strip().lower() == "direct":
    PROXY_URL = ""

# --- Параметры модели Маркова ---
MIN_MESSAGES = 20      # минимум сообщений в корпусе для генерации
MAX_ATTEMPTS = 8       # сколько раз пробовать сгенерировать неповторяющийся ответ
MARKOV_STATE = 1       # размер состояния марковской цепи (1 или 2)

# --- Шансы поведения бота (0.0..1.0) ---
REPLY_CHANCE = 0.05         # шанс ответа на обычное сообщение
MEDIA_REPLY_CHANCE = 0.25   # шанс ответа стикером/гифкой вместо текста
DEMOTIVATOR_CHANCE = 0.05   # шанс сделать демотиватор из присланного фото
MUTATION_CHANCE = 0.3       # шанс применить мутацию к сгенерированному тексту
SANITY = 0.7                # «разумность»: 1.0 — копирует реальные сообщения,
                            # 0.0 — чистый бред от марковских цепей
CONTEXT_CHANCE = 0.5        # шанс ответа «в тему» (по контексту)
