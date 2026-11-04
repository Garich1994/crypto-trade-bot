"""Конфигурация проекта.

Единственный источник секретов — файл .env в корне проекта. Токен берётся только
отсюда, нигде не логируется и не печатается.
"""
from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"


def _load_dotenv() -> None:
    """Минимальный загрузчик .env без внешних зависимостей.

    Читает строки вида KEY=VALUE, игнорирует пустые строки и комментарии.
    Уже существующие переменные окружения не перезатираются.
    """
    if not _ENV_PATH.exists():
        return
    for raw in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

# Токен держим приватным на уровне модуля, наружу отдаём только через функцию.
_BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Таймаут HTTP-запросов к внешним API (секунды).
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "10"))


def require_bot_token() -> str:
    """Вернуть токен бота или понятно упасть, если он не задан.

    Токен намеренно не выводится ни в логи, ни в текст ошибки.
    """
    if not _BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не задан. Скопируй .env.example в .env и впиши токен от @BotFather."
        )
    return _BOT_TOKEN
