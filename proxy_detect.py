"""Авто-детект рабочего пути до api.telegram.org.

Используется лаунчером при первом запуске MarkovBot.exe, когда в
config.ini поле [proxy] url пустое. Перебирает кандидатов (прямое
подключение, типичные локальные SOCKS5 / HTTP-прокси) и возвращает
первый рабочий.

Тест — HTTP GET к https://api.telegram.org/. Любой ответ сервера
(200/400/401/404 и т.п.) считается успехом: значит, до Telegram
действительно достучались. Сетевые ошибки и таймаут — провал.
"""
from __future__ import annotations
import logging
from typing import Optional, Callable

import httpx

logger = logging.getLogger(__name__)


# Кандидаты по приоритету. Первый рабочий — победитель.
# None означает «без прокси, прямое подключение».
CANDIDATES: list[Optional[str]] = [
    None,                          # без прокси (VPN в TUN-режиме / отсутствие блокировок)
    "socks5://127.0.0.1:10808",    # v2rayN, Nekoray (SOCKS-порт по умолчанию)
    "socks5://127.0.0.1:1080",     # sing-box, byedpi, общеупотребительный SOCKS
    "socks5://127.0.0.1:1086",     # альтернативный порт некоторых клиентов
    "http://127.0.0.1:10809",      # v2rayN (HTTP-порт по умолчанию)
    "http://127.0.0.1:10810",      # Hiddify (HTTP)
    "http://127.0.0.1:7890",       # Clash
    "http://127.0.0.1:8080",       # generic HTTP proxy
]

TEST_URL = "https://api.telegram.org/"
PER_CANDIDATE_TIMEOUT_SEC = 2.0


def _test_one(proxy_url: Optional[str]) -> bool:
    """Один HTTP-запрос к api.telegram.org через указанный прокси.

    Возвращает True, если сервер ответил любым кодом (2xx/3xx/4xx/5xx) —
    значит, соединение реально дошло до Telegram. Сетевые ошибки и
    таймаут — False.
    """
    try:
        with httpx.Client(proxy=proxy_url,
                          timeout=PER_CANDIDATE_TIMEOUT_SEC) as client:
            r = client.get(TEST_URL)
        return 200 <= r.status_code < 600
    except Exception as e:
        label = proxy_url or "direct"
        logger.debug(f"Test {label!r} failed: {type(e).__name__}: {e}")
        return False


def detect_proxy(
    on_progress: Optional[Callable[[Optional[str], int, int], None]] = None,
) -> tuple[bool, Optional[str]]:
    """Перебирает CANDIDATES, возвращает (success, first_working_proxy).

    on_progress(candidate, current_index, total) вызывается ПЕРЕД каждой
    пробой — даёт лаунчеру шанс обновить UI / спиннер.

    Возвращает:
      (True,  None)              — прямое подключение работает
      (True,  "socks5://...")    — нашли локальный прокси
      (False, None)              — ни один из кандидатов не отвечает
    """
    total = len(CANDIDATES)
    for i, candidate in enumerate(CANDIDATES, start=1):
        if on_progress is not None:
            try:
                on_progress(candidate, i, total)
            except Exception:
                # колбэк не должен ронять детект
                pass
        if _test_one(candidate):
            return True, candidate
    return False, None
