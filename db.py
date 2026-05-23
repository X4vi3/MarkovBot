"""Слой работы с SQLite.

Карта SQL-запросов (всего 12 разных, превышает требование «минимум 5»):

  DDL — создание схемы (выполняется один раз при первом запуске)
    init_db()                       CREATE TABLE messages
                                    CREATE TABLE media
                                    CREATE TABLE settings
                                    CREATE INDEX idx_messages_user_id

  DML — основные запросы:
    №1   save_message()             INSERT INTO messages
    №2   get_all_messages()         SELECT всех строк
    №3   get_recent_messages()      SELECT с ORDER BY + LIMIT
    №4   count_all_messages()       SELECT COUNT(*)               (агрегация)
    №5   delete_messages()          DELETE FROM messages WHERE    (по условию)
    №6   delete_messages()          DELETE FROM media WHERE       (по условию)
    №7   save_media()               INSERT OR IGNORE              (анти-дубликат)
    №8   get_random_media(kind=)    SELECT WHERE + ORDER BY RANDOM LIMIT 1
    №9   get_random_media()         SELECT ORDER BY RANDOM LIMIT 1
    №10  count_media()              SELECT COUNT(*)               (агрегация)
    №11  get_setting()              SELECT по ключу
    №12  set_setting()              INSERT OR REPLACE             (UPSERT)
"""
import sqlite3
import os
import sys
import logging

logger = logging.getLogger(__name__)


def _db_path() -> str:
    """Путь к bot.db.

    При запуске из .exe (PyInstaller --onefile) база лежит рядом с самим
    исполняемым файлом, а не во временной распаковке sys._MEIPASS.
    Это позволяет данным сохраняться между запусками.
    """
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "bot.db")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")


DB_PATH = _db_path()


def _connect():
    return sqlite3.connect(DB_PATH)


# ───────────────────────────────────────────────────────────────────────
# DDL: создаём три пользовательские таблицы и индекс.
# Кроме них SQLite автоматически создаёт служебную таблицу
# sqlite_sequence для поддержки AUTOINCREMENT в messages и media —
# она содержит последние выданные значения id и в коде не используется.
# Выполняется один раз при первом запуске.
# ───────────────────────────────────────────────────────────────────────
def init_db():
    with _connect() as conn:
        # CREATE TABLE messages — корпус сообщений для обучения модели
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text    TEXT NOT NULL
            )
            """
        )
        # CREATE TABLE media — стикеры/гифки (file_id UNIQUE против дубликатов)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                file_id  TEXT NOT NULL UNIQUE,
                kind     TEXT NOT NULL
            )
            """
        )
        # CREATE TABLE settings — ключ-значение для глобальных и per-chat параметров
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        # CREATE INDEX — для быстрого удаления сообщений конкретного пользователя
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages (user_id)"
        )
        conn.commit()


# ── Запрос №1: INSERT ─────────────────────────────────────────────────
# Добавляет новое сообщение пользователя в корпус.
def save_message(user_id: int, text: str):
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO messages (user_id, text) VALUES (?, ?)",
                (user_id, text),
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"DB save_message error: {e}")


# ── Запрос №2: SELECT всех строк ──────────────────────────────────────
# Возвращает весь текстовый корпус для обучения марковской модели.
def get_all_messages() -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT text FROM messages").fetchall()
    return [row[0] for row in rows]


# ── Запрос №3: SELECT с ORDER BY + LIMIT ──────────────────────────────
# Возвращает последние N сообщений — нужно для контекста при ответе.
def get_recent_messages(limit: int = 10) -> list[str]:
    """Последние N сообщений (по порядку добавления)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT text FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [row[0] for row in reversed(rows)]


# ── Запрос №4: SELECT COUNT(*) — агрегация ────────────────────────────
# Сколько сообщений в корпусе. Используется для приветствия и порога обучения.
def count_all_messages() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
    return row[0]


# ── Запросы №5 и №6: DELETE WHERE ─────────────────────────────────────
# Удаляет все сообщения и медиа конкретного пользователя (команда /reset).
# В одной функции — два запроса по условию.
def delete_messages(user_id: int):
    with _connect() as conn:
        # Запрос №5
        conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        # Запрос №6
        conn.execute("DELETE FROM media WHERE user_id = ?", (user_id,))
        conn.commit()


# ── Запрос №7: INSERT OR IGNORE — анти-дубликат ───────────────────────
# Сохраняет стикер/гиф. UNIQUE-ограничение на file_id защищает от дубликатов:
# если такой file_id уже есть — INSERT тихо игнорируется.
def save_media(user_id: int, file_id: str, kind: str):
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO media (user_id, file_id, kind) VALUES (?, ?, ?)",
                (user_id, file_id, kind),
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"DB save_media error: {e}")


# ── Запросы №8 и №9: SELECT с ORDER BY RANDOM() LIMIT 1 ───────────────
# Случайный медиа-объект из корпуса. Опциональный фильтр по типу (sticker/gif).
def get_random_media(kind: str | None = None) -> tuple[str, str] | None:
    with _connect() as conn:
        if kind:
            # Запрос №8: SELECT с WHERE-фильтром + случайная выборка
            row = conn.execute(
                "SELECT file_id, kind FROM media WHERE kind = ? ORDER BY RANDOM() LIMIT 1",
                (kind,),
            ).fetchone()
        else:
            # Запрос №9: SELECT без фильтра + случайная выборка
            row = conn.execute(
                "SELECT file_id, kind FROM media ORDER BY RANDOM() LIMIT 1"
            ).fetchone()
    if row:
        return (row[0], row[1])
    return None


# ── Запрос №10: SELECT COUNT(*) — агрегация ───────────────────────────
# Сколько медиа-объектов в корпусе. Используется в команде «v пинг».
def count_media() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM media").fetchone()
    return row[0]


# Per-chat обёртка над settings: парсит строку с id тем,
# разделённых запятой. Сам SQL — внутри get_setting/set_setting (запросы №11 и №12).
def get_allowed_threads(chat_id: int) -> set[int]:
    """Возвращает множество разрешённых тем (message_thread_id) для чата.

    Пустое множество означает «нет ограничений» — бот пишет во всех
    темах. 0 в множестве — общий чат (сообщения без темы).
    """
    val = get_setting(f"{chat_id}:allowed_threads")
    if not val:
        return set()
    return {int(x) for x in val.split(",") if x.strip().lstrip("-").isdigit()}


def set_allowed_threads(chat_id: int, threads: set[int]):
    set_setting(
        f"{chat_id}:allowed_threads",
        ",".join(str(t) for t in sorted(threads)),
    )


# ── Запрос №11: SELECT по ключу ───────────────────────────────────────
# Получает значение параметра конфигурации (один из ключей в таблице settings).
def get_setting(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else None


# ── Запрос №12: INSERT OR REPLACE (UPSERT) ────────────────────────────
# Сохраняет параметр конфигурации. Если ключ уже существует — обновляет
# значение; если нет — создаёт новую запись. Одной операцией.
def set_setting(key: str, value: str):
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
