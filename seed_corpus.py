"""Заливка стартового корпуса фраз в базу бота.

Запуск:
    python seed_corpus.py path/to/phrases.csv

Ожидаемый формат CSV: должна быть колонка `text` с фразой.
Дополнительно учитывается колонка `nsfw_level` — фразы с уровнем > 0
пропускаются. Уже существующие в БД фразы (по точному совпадению)
не дублируются.

Импортированные фразы сохраняются в таблицу messages с user_id = 0
(условный «seed-пользователь», чтобы их можно было отличить от
реальных сообщений).
"""
import os
import sys
import csv
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from markov import _is_garbage

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")
SEED_USER_ID = 0


def main():
    if len(sys.argv) < 2:
        print("Использование: python seed_corpus.py path/to/phrases.csv")
        sys.exit(1)

    csv_path = sys.argv[1]
    if not os.path.exists(csv_path):
        print(f"Файл не найден: {csv_path}")
        sys.exit(1)

    # Загружаем фразы из CSV
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    print(f"Прочитано из CSV: {len(rows)} записей")

    # Открываем БД, создаём таблицы при необходимости
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text    TEXT NOT NULL
        )
    """)
    conn.commit()

    # Существующие фразы — для дедупликации
    existing = set()
    for row in conn.execute("SELECT text FROM messages").fetchall():
        existing.add(row[0].lower().strip())
    print(f"Уже в БД: {len(existing)} сообщений")

    # Фильтрация
    good = []
    skipped = {"no_text": 0, "nsfw": 0, "garbage": 0, "duplicate": 0}
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            skipped["no_text"] += 1
            continue
        nsfw = row.get("nsfw_level", "0")
        try:
            if int(nsfw) > 0:
                skipped["nsfw"] += 1
                continue
        except ValueError:
            pass
        if _is_garbage(text):
            skipped["garbage"] += 1
            continue
        lower = text.lower().strip()
        if lower in existing:
            skipped["duplicate"] += 1
            continue
        existing.add(lower)
        good.append(text)

    print(f"\nГотово к импорту: {len(good)} фраз")
    print("Пропущено:")
    for reason, count in sorted(skipped.items()):
        if count > 0:
            print(f"  {reason}: {count}")

    if not good:
        print("\nНечего импортировать.")
        conn.close()
        return

    print("\nПримеры (первые 5):")
    for text in good[:5]:
        print(f"  > {text[:80]}")

    answer = input(f"\nИмпортировать {len(good)} фраз? (y/n): ").strip().lower()
    if answer != "y":
        print("Отменено.")
        conn.close()
        return

    BATCH = 500
    for i in range(0, len(good), BATCH):
        batch = [(SEED_USER_ID, text) for text in good[i:i + BATCH]]
        conn.executemany(
            "INSERT INTO messages (user_id, text) VALUES (?, ?)",
            batch,
        )
        conn.commit()
        print(f"  Импортировано {min(i + BATCH, len(good))}/{len(good)}...")

    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    print(f"\nГотово. Импортировано: {len(good)}. Всего в корпусе: {total}.")


if __name__ == "__main__":
    main()
