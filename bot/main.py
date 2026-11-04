"""Точка входа бота: long polling для локального запуска на Windows.

Запуск:  python -m bot.main
Токен берётся из .env (BOT_TOKEN) и нигде не логируется.
"""
from __future__ import annotations

import asyncio
import logging
import sys

# На Windows консоль по умолчанию cp1252 и портит кириллицу в логах — переключаем в UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from aiogram import Bot, Dispatcher

from bot.handlers import router
from core.config import require_bot_token


async def main() -> None:
    token = require_bot_token()  # упадёт понятно, если токена нет
    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)

    logging.info("Бот запускается (long polling)...")
    # Чистим апдейты, накопившиеся пока бот был офлайн.
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
