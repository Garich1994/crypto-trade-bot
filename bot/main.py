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

from agents.alerts_agent import run_check
from agents.paper_agent import run_paper_check
from bot.formatting import format_alert_fired, format_paper_closed
from bot.handlers import router
from core.config import require_bot_token

#: Период фоновой проверки подписок (Сторож), секунды.
_ALERTS_INTERVAL = 120


async def _alerts_loop(bot: Bot) -> None:
    """Фоновый Сторож: каждые 120с проверяет подписки и шлёт уведомления.

    Работает только пока жив процесс бота. Ошибки не роняют цикл.
    """
    while True:
        try:
            # 1) Пользовательские подписки.
            for event in await run_check():
                try:
                    await bot.send_message(
                        event["user_id"], format_alert_fired(event)
                    )
                except Exception:
                    logging.exception("Не удалось отправить уведомление подписки")
            # 2) Автозакрытие виртуальных позиций по стоп/тейк.
            for event in await run_paper_check():
                try:
                    await bot.send_message(
                        event["user_id"], format_paper_closed(event)
                    )
                except Exception:
                    logging.exception("Не удалось отправить уведомление paper")
        except Exception:
            logging.exception("Ошибка в цикле Сторожа")
        await asyncio.sleep(_ALERTS_INTERVAL)


async def main() -> None:
    token = require_bot_token()  # упадёт понятно, если токена нет
    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)

    logging.info("Бот запускается (long polling)...")
    # Чистим апдейты, накопившиеся пока бот был офлайн.
    await bot.delete_webhook(drop_pending_updates=True)

    alerts_task = asyncio.create_task(_alerts_loop(bot))  # Сторож
    try:
        await dp.start_polling(bot)
    finally:
        alerts_task.cancel()
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
