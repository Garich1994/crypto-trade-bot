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
from agents.coordinator_agent import (
    build_brief,
    due_subscriptions,
    mark_brief_sent,
)
from agents.paper_agent import run_paper_check
from bot.formatting import format_alert_fired, format_brief, format_paper_closed
from bot.handlers import router
from core.config import require_bot_token
from miniapp import start_miniapp

#: Период фоновой проверки подписок (Сторож), секунды.
_ALERTS_INTERVAL = 120
#: Период проверки подписок на автодайджест, секунды.
_BRIEF_INTERVAL = 900


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


async def _brief_loop(bot: Bot) -> None:
    """Фоновая авторассылка дайджеста подписчикам. Работает пока жив процесс бота."""
    while True:
        try:
            due = due_subscriptions()
            if due:
                brief = await build_brief(top=5)
                text = format_brief(brief, auto=True)
                for user_id in due:
                    try:
                        await bot.send_message(user_id, text)
                        mark_brief_sent(user_id)
                    except Exception:
                        logging.exception("Не удалось отправить автодайджест")
        except Exception:
            logging.exception("Ошибка в цикле автодайджеста")
        await asyncio.sleep(_BRIEF_INTERVAL)


async def main() -> None:
    token = require_bot_token()  # упадёт понятно, если токена нет
    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)

    logging.info("Бот запускается (long polling)...")
    # Чистим апдейты, накопившиеся пока бот был офлайн.
    await bot.delete_webhook(drop_pending_updates=True)

    alerts_task = asyncio.create_task(_alerts_loop(bot))  # Сторож
    brief_task = asyncio.create_task(_brief_loop(bot))    # Автодайджест

    miniapp_runner = None
    try:
        miniapp_runner = await start_miniapp()  # веб-дашборд на :8080
        logging.info("Дашборд запущен на http://localhost:8080")
    except Exception:
        logging.exception("Не удалось запустить дашборд (бот продолжит работу)")

    try:
        await dp.start_polling(bot)
    finally:
        alerts_task.cancel()
        brief_task.cancel()
        if miniapp_runner is not None:
            await miniapp_runner.cleanup()
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
