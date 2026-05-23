"""Лаунчер MarkovBot (точка входа для .exe-сборки).

Реализует:
- красивый баннер при старте (rich);
- проверку наличия токена / прокси через config.ini рядом с .exe;
- мини-визард первого запуска (создание config.ini);
- проверку доступности Telegram API через прокси;
- автоимпорт стартового корпуса из встроенного phrases_telegram.csv;
- запуск bot.main() с цветными логами.
"""
import configparser
import logging
import os
import sys
import sqlite3
import warnings
from typing import Optional

# Подавляем ложное PTBUserWarning «Application instances should be built via
# the ApplicationBuilder» — оно срабатывает изнутри самого ApplicationBuilder
# в PTB 21.x (особенно в PyInstaller-сборках) и помечено разработчиками PTB
# как known false-positive. Бот при этом работает корректно.
warnings.filterwarnings(
    "ignore",
    message=r".*Application.*built via.*ApplicationBuilder.*",
)

# Принудительно UTF-8 в stdout/stderr — иначе на Windows кириллица и
# спецсимволы (стрелки) падают через cp1251 в pipe-режиме.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.logging import RichHandler
    from rich.text import Text
except ImportError:                                        # pragma: no cover
    print("Не установлен пакет 'rich'. Запустите:")
    print("    pip install rich")
    sys.exit(1)


# legacy_windows=False — заставляет rich писать ANSI/UTF-8 даже на старых
# консолях Windows, иначе через pipe всё падает на cp1251.
console = Console(legacy_windows=False, force_terminal=True)


# ---------- Пути ----------
def app_dir() -> str:
    """Папка рядом с .exe (или с launcher.py при запуске из исходников).

    PyInstaller --onefile распаковывает ресурсы во временный каталог в
    sys._MEIPASS, но «пользовательские» данные должны лежать рядом с самим
    исполняемым файлом (sys.executable).
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def bundled_resource(name: str) -> str:
    """Ресурс, упакованный в .exe (CSV-корпус, ogg)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


CONFIG_PATH = os.path.join(app_dir(), "config.ini")
DB_PATH = os.path.join(app_dir(), "bot.db")


# ---------- Баннер ----------
def print_banner():
    title = Text.assemble(
        ("MarkovBot", "bold cyan"),
        (" — Telegram-бот с генерацией текста на цепях Маркова\n", "white"),
        ("Курсовой проект, Комаревцев М.А., группа 253-325", "dim"),
    )
    console.print(Panel(title, expand=False, border_style="cyan"))


def print_proxy_warning():
    body = Text.assemble(
        ("Перед запуском проверьте, что:\n\n", "white"),
        ("  1. ", "bold yellow"),
        ("У вас запущен VPN (TUN-mode) либо локальный SOCKS5/HTTP прокси.\n",
         "white"),
        ("     Без него бот не достучится до api.telegram.org.\n\n", "dim"),
        ("  2. ", "bold yellow"),
        ("Если используете прокси с правилами routing — пропустите\n",
         "white"),
        ("     через прокси домены: api.telegram.org, core.telegram.org,\n",
         "white"),
        ("     *.cdn-telegram.org. В v2rayN/Nekoray проще включить\n",
         "white"),
        ("     geosite:telegram → proxy.\n\n", "white"),
        ("  3. ", "bold yellow"),
        ("Токен бота получен у @BotFather, и у бота отключён\n", "white"),
        ("     Group Privacy (Bot Settings → Group Privacy → Turn off).\n",
         "white"),
    )
    console.print(Panel(body, title="[yellow]Перед запуском[/yellow]",
                        border_style="yellow"))


# ---------- Конфиг ----------
DEFAULT_CONFIG = """\
# Конфигурация MarkovBot.
# Параметры можно править вручную в этом файле или через визард при старте.

[telegram]
# Токен бота от @BotFather. Получается командой /newbot.
token =

# Username бота без @ (например, bestInfo253bot).
username = MarkovBot

# Telegram user_id владельца. Получить можно у @userinfobot.
owner_id = 0

[proxy]
# Адрес локального прокси (SOCKS5 или HTTP).
# Если используете VPN в TUN-режиме — оставьте пустым.
# Пример: socks5://127.0.0.1:10808
url =

[logging]
# Уровень детализации логов.
# false — стандартный (INFO): видны входящие сообщения, ответы, ошибки.
# true  — расширенный (DEBUG): + полный traceback ошибок,
#         HTTP-запросы к Telegram API, внутренние решения генерации.
verbose = false
"""


def _render_config(token: str, username: str, owner_id: str,
                   proxy: str, verbose: str = "false") -> str:
    """Собирает текст config.ini вручную — БЕЗ configparser.write().

    Это важно: configparser.write() стирает комментарии при перезаписи и
    может вмешиваться в значения через интерполяцию (`%`). Ручная сборка
    f-строкой пишет ровно то, что ввёл пользователь, и сохраняет
    подсказки-комментарии для последующего ручного редактирования.
    """
    return (
        "# Конфигурация MarkovBot.\n"
        "# Параметры можно править вручную в этом файле или через визард при старте.\n"
        "\n"
        "[telegram]\n"
        "# Токен бота от @BotFather. Получается командой /newbot.\n"
        f"token = {token}\n"
        "\n"
        "# Username бота без @ (например, bestInfo253bot).\n"
        f"username = {username}\n"
        "\n"
        "# Telegram user_id владельца. Получить можно у @userinfobot.\n"
        f"owner_id = {owner_id}\n"
        "\n"
        "[proxy]\n"
        "# Адрес локального прокси (SOCKS5 или HTTP).\n"
        "# Если используете VPN в TUN-режиме — оставьте пустым.\n"
        "# Пример: socks5://127.0.0.1:10808\n"
        f"url = {proxy}\n"
        "\n"
        "[logging]\n"
        "# Уровень детализации логов.\n"
        "# false — стандартный (INFO): видны входящие сообщения, ответы, ошибки.\n"
        "# true  — расширенный (DEBUG): + полный traceback ошибок,\n"
        "#         HTTP-запросы к Telegram API, внутренние решения генерации.\n"
        f"verbose = {verbose}\n"
    )


def ensure_config_file_exists() -> None:
    """Создаёт config.ini с шаблоном, если его нет. Без интерактива."""
    if not os.path.exists(CONFIG_PATH):
        console.print(
            "[cyan]Первый запуск: создаю[/cyan] "
            f"[bold]{os.path.basename(CONFIG_PATH)}[/bold]")
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG)


def read_config() -> configparser.ConfigParser:
    """Читает config.ini. Если каких-то секций нет — добавляет пустые
    (чтобы дальнейший cfg["..."]... не падал KeyError на повреждённом файле).
    """
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    for section in ("telegram", "proxy", "logging"):
        if not cfg.has_section(section):
            cfg.add_section(section)
    return cfg


def save_config(cfg: configparser.ConfigParser) -> None:
    """Записывает cfg в config.ini через _render_config (сохраняет комментарии)."""
    content = _render_config(
        token=cfg["telegram"].get("token", "").strip(),
        username=cfg["telegram"].get("username", "MarkovBot").strip(),
        owner_id=cfg["telegram"].get("owner_id", "0").strip() or "0",
        proxy=cfg["proxy"].get("url", "").strip(),
        verbose=cfg["logging"].get("verbose", "false").strip(),
    )
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def run_auto_detect(cfg: configparser.ConfigParser) -> bool:
    """Авто-детект рабочего прокси.

    Запускается, только когда [proxy] url пуст. По итогу записывает в cfg:
      • "direct"          — нашли прямое подключение;
      • "socks5://..."    — нашли локальный прокси (или юзер ввёл вручную);
      • не сохраняет ничего — если юзер отказался ввести прокси при провале.

    Возвращает True, если можно продолжать запуск (есть какой-то рабочий
    канал или ручной ввод). False — если ничего, и юзер не ввёл вручную;
    в этом случае лаунчер должен завершиться.
    """
    from proxy_detect import detect_proxy

    found_ok = False
    found_proxy: Optional[str] = None

    def _progress(candidate, idx, total):
        label = "без прокси" if candidate is None else candidate
        status.update(f"[cyan]Проверка ({idx}/{total}):[/cyan] {label}")

    with console.status(
            "[cyan]Проверка подключения к Telegram...[/cyan]") as status:
        found_ok, found_proxy = detect_proxy(on_progress=_progress)

    if found_ok:
        if found_proxy is None:
            console.print("[green]✓[/green] Прямое подключение работает.")
            cfg["proxy"]["url"] = "direct"
        else:
            console.print(
                f"[green]✓[/green] Найден прокси: [bold]{found_proxy}[/bold]")
            cfg["proxy"]["url"] = found_proxy
        save_config(cfg)
        return True

    # Авто-детект не справился — даём шанс ввести вручную.
    console.print()
    console.print(
        "[red]✗[/red] Не удалось достучаться до api.telegram.org "
        "ни напрямую, ни через типичные локальные прокси.")
    console.print()
    console.print("Возможные причины:")
    console.print("  • VPN/прокси не запущен")
    console.print("  • Прокси на нестандартном порту")
    console.print("  • Прокси требует логин/пароль")
    console.print("  • Провайдер блокирует исходящие соединения")
    console.print()
    custom = Prompt.ask(
        "Введите адрес прокси вручную (Enter — выйти)",
        default="").strip()
    if not custom:
        return False
    cfg["proxy"]["url"] = custom
    save_config(cfg)
    return True


def run_token_wizard(cfg: configparser.ConfigParser) -> bool:
    """Интерактивный мастер заполнения токена / username / OWNER_ID.

    Прокси здесь НЕ спрашивается — он уже разрешён авто-детектом раньше.
    Возвращает True, если токен валиден и поля сохранены. False — если
    юзер отказался от мастера или ввёл пустой токен (в обоих случаях
    мастер сам печатает понятное объяснение).
    """
    if not Confirm.ask(
            "\n[yellow]Заполнить параметры бота сейчас?[/yellow]",
            default=True):
        console.print(
            "\n[yellow]ОК, мастер пропущен.[/yellow]\n"
            "Бот не запустится, пока не задан токен. Откройте файл "
            "[bold]config.ini[/bold] рядом с программой, впишите токен в "
            "строку [cyan]token =[/cyan] в секции [cyan][telegram][/cyan] "
            "и запустите MarkovBot заново.")
        return False

    token = Prompt.ask(
        "Токен бота от @BotFather",
        default=cfg["telegram"].get("token", "")).strip()

    if not token:
        console.print(
            "\n[red]Токен не введён — без него бот не сможет подключиться "
            "к Telegram.[/red]\n"
            "Получите токен у [bold]@BotFather[/bold] (команда "
            "[cyan]/newbot[/cyan]) и запустите MarkovBot заново.\n"
            "Либо отредактируйте [bold]config.ini[/bold] вручную: "
            "впишите значение от BotFather в строку "
            "[cyan]token =[/cyan].")
        return False

    username = Prompt.ask(
        "Username бота (без @)",
        default=cfg["telegram"].get("username", "MarkovBot")).strip()
    owner_id = Prompt.ask(
        "Ваш Telegram user_id (0 — без выделенного владельца)",
        default=cfg["telegram"].get("owner_id", "0")).strip()

    cfg["telegram"]["token"] = token
    cfg["telegram"]["username"] = username
    cfg["telegram"]["owner_id"] = owner_id
    save_config(cfg)
    console.print("[green]Сохранено в config.ini.[/green]\n")
    return True


def apply_config_to_env(cfg: configparser.ConfigParser) -> bool:
    """Прокидывает параметры из config.ini в окружение, чтобы config.py их подобрал."""
    token = cfg["telegram"].get("token", "").strip()
    if not token:
        console.print("[red]Ошибка:[/red] не задан токен в config.ini "
                      "[telegram] token = ...")
        return False
    os.environ["TELEGRAM_TOKEN"] = token
    os.environ["BOT_USERNAME"] = cfg["telegram"].get("username", "MarkovBot").strip()
    os.environ["OWNER_ID"] = cfg["telegram"].get("owner_id", "0").strip() or "0"
    os.environ["PROXY_URL"] = cfg["proxy"].get("url", "").strip()
    return True


# ---------- Импорт корпуса ----------
def seed_corpus_if_empty():
    """При первом запуске импортирует встроенный CSV-корпус в bot.db."""
    if not os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, text TEXT NOT NULL)""")
        conn.commit()
        conn.close()

    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()

    if n > 0:
        console.print(f"[dim]В базе уже {n} сообщений, импорт не нужен.[/dim]")
        return

    csv_path = bundled_resource("phrases_telegram.csv")
    if not os.path.exists(csv_path):
        console.print("[yellow]Стартовый корпус не найден, бот начнёт с нуля. "
                      "Дайте чату накопить ≥200 сообщений.[/yellow]")
        return

    console.print(f"[cyan]Импортирую стартовый корпус из[/cyan] "
                  f"{os.path.basename(csv_path)}...")
    import csv as _csv
    conn = sqlite3.connect(DB_PATH)
    count = 0
    with open(csv_path, "r", encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            text = (row.get("text") or "").strip()
            if not text:
                continue
            try:
                if int(row.get("nsfw_level", "0")) > 0:
                    continue
            except ValueError:
                pass
            conn.execute("INSERT INTO messages (user_id, text) VALUES (0, ?)",
                         (text,))
            count += 1
            if count % 5000 == 0:
                console.print(f"  [dim]импортировано {count}...[/dim]")
    conn.commit()
    conn.close()
    console.print(f"[green]Готово. Импортировано {count} фраз.[/green]")


# ---------- Логи ----------
def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, show_path=False,
                              rich_tracebacks=True, markup=False)],
    )
    # Подавляем шум от библиотек, оставляем только нужное
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)
        logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)
    else:
        # В verbose режиме показываем подробности HTTP и telegram-бота
        logging.getLogger("httpx").setLevel(logging.INFO)
        logging.getLogger("httpcore").setLevel(logging.INFO)
        logging.getLogger("telegram").setLevel(logging.INFO)


# ---------- Точка входа ----------
def _main():
    print_banner()
    print_proxy_warning()

    # 1. Гарантируем существование config.ini (создаём шаблон, если нет).
    ensure_config_file_exists()
    cfg = read_config()

    # 2. Если url не задан — запускаем авто-детект ДО любых интерактивов
    #    с пользователем. Бессмысленно спрашивать токен, если нет сети.
    if not cfg["proxy"].get("url", "").strip():
        if not run_auto_detect(cfg):
            console.print(
                "\n[red]Не удалось установить соединение с Telegram.[/red]"
                "\nНастройте VPN/прокси и перезапустите MarkovBot.")
            input("\nНажмите Enter, чтобы закрыть окно...")
            sys.exit(1)
        # Состояние cfg обновилось внутри run_auto_detect; на всякий
        # случай перечитываем с диска, чтобы все секции были актуальны.
        cfg = read_config()

    # 3. Если токен не задан — запускаем wizard. Прокси здесь уже не
    #    спрашиваем: он либо найден детектом, либо введён вручную при
    #    провале детекта.
    if not cfg["telegram"].get("token", "").strip():
        if not run_token_wizard(cfg):
            input("\nНажмите Enter, чтобы закрыть окно...")
            sys.exit(1)
        cfg = read_config()

    # 4. Проброс env vars + запуск бота.
    if not apply_config_to_env(cfg):
        console.print("\n[red]Откройте config.ini, заполните параметры, "
                      "запустите заново.[/red]\n")
        input("Нажмите Enter, чтобы закрыть окно...")
        sys.exit(1)

    seed_corpus_if_empty()

    verbose = False
    try:
        verbose = cfg.getboolean("logging", "verbose", fallback=False)
    except Exception:
        pass
    setup_logging(verbose=verbose)
    if verbose:
        console.print("[dim]Verbose-логи включены (logging.verbose=true).[/dim]")

    # Импортируем bot ПОСЛЕ настройки окружения, чтобы config.py подхватил токен.
    console.print("\n[cyan]Запускаю бота...[/cyan]\n")
    try:
        from bot import main as bot_main
        bot_main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Остановлено пользователем.[/yellow]")


def main():
    """Внешняя обёртка: ловит ВСЕ ошибки и не даёт окну закрыться,
    пока пользователь не прочитает сообщение."""
    try:
        _main()
    except SystemExit as e:
        if e.code not in (0, None):
            input("\nНажмите Enter, чтобы закрыть окно...")
        raise
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        print("\n" + "=" * 70)
        print("ОШИБКА при запуске MarkovBot:")
        print("=" * 70)
        traceback.print_exc()
        print("=" * 70)
        # Лог ошибки в файл рядом с exe для отладки
        try:
            log_path = os.path.join(app_dir(), "error.log")
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("Ошибка запуска MarkovBot\n")
                f.write("=" * 70 + "\n")
                traceback.print_exc(file=f)
            print(f"\nПолный лог записан в:\n  {log_path}")
        except Exception:
            pass
        print()
        input("Нажмите Enter, чтобы закрыть окно...")
        sys.exit(1)


if __name__ == "__main__":
    main()
