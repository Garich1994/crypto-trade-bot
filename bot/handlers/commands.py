"""Telegram-хендлеры (aiogram 3.x)."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from agents.backtest_agent import BacktestError, backtest
from agents.data_agent import DataAgentError
from agents.data_agent.client import ALLOWED_INTERVALS
from agents.ta_agent import analyze
from agents.ta_agent.analyzer import DEFAULT_TIMEFRAME
from bot.formatting import DISCLAIMER, format_analysis, format_backtest

logger = logging.getLogger(__name__)
router = Router()

START_TEXT = (
    "👋 Привет! Я бот для нейтральной аналитики крипторынка.\n"
    "Я не даю торговых указаний — только факты о цене и индикаторах.\n\n"
    "Команды:\n"
    "• /start — это сообщение\n"
    "• /analyze BTCUSDT [таймфрейм] — технический разбор пары\n"
    f"   таймфреймы: {', '.join(ALLOWED_INTERVALS)} (по умолчанию {DEFAULT_TIMEFRAME})\n"
    "   пример: /analyze ETHUSDT 4h\n"
    "• /backtest BTCUSDT long вход стоп тейк [таймфрейм] — прогон уровней по истории\n"
    "   пример: /backtest BTCUSDT long 80000 78000 84000 1h\n\n"
    f"{DISCLAIMER}"
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("analyze"))
async def cmd_analyze(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if not args:
        await message.answer(
            "Укажи пару, например: /analyze BTCUSDT 1h\n"
            f"Доступные таймфреймы: {', '.join(ALLOWED_INTERVALS)}."
        )
        return

    symbol = args[0].upper()
    timeframe = args[1].lower() if len(args) > 1 else DEFAULT_TIMEFRAME

    try:
        result = await analyze(symbol, timeframe)
    except DataAgentError as exc:
        # Понятный текст пользователю, без трейсбека.
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        # Любая иная ошибка — не показываем внутренности наружу.
        logger.exception("Непредвиденная ошибка в /analyze")
        await message.answer(
            "Произошла внутренняя ошибка. Попробуй позже."
        )
        return

    await message.answer(format_analysis(result))


_BACKTEST_USAGE = (
    "Формат: /backtest ПАРА сторона вход стоп тейк [таймфрейм]\n"
    "Пример: /backtest BTCUSDT long 80000 78000 84000 1h\n"
    "Сторона: long или short. Для лонга стоп < вход < тейк, для шорта наоборот."
)


@router.message(Command("backtest"))
async def cmd_backtest(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 5:
        await message.answer(_BACKTEST_USAGE)
        return

    symbol = args[0].upper()
    side = args[1]
    timeframe = args[5].lower() if len(args) > 5 else DEFAULT_TIMEFRAME

    try:
        entry = float(args[2].replace(",", "."))
        stop = float(args[3].replace(",", "."))
        take = float(args[4].replace(",", "."))
    except ValueError:
        await message.answer(
            "Вход, стоп и тейк должны быть числами.\n\n" + _BACKTEST_USAGE
        )
        return

    try:
        result = await backtest(symbol, timeframe, side, entry, stop, take)
    except (BacktestError, DataAgentError) as exc:
        # Понятный текст пользователю, без трейсбека.
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /backtest")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_backtest(result))
