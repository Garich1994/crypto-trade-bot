"""Telegram-хендлеры (aiogram 3.x)."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from agents.backtest_agent import BacktestError, backtest
from agents.budget_agent import BudgetError, calc_position
from agents.data_agent import DataAgentError
from agents.data_agent.client import ALLOWED_INTERVALS
from agents.entry_agent import EntryError, score_entry
from agents.exit_agent import ExitError, plan_exit
from agents.journal_agent import (
    JournalError,
    add_trade,
    close_trade,
    open_trades,
    stats,
)
from agents.news_agent import get_crypto_news
from agents.risk_agent import RiskError, check_risk
from agents.scanner_agent import scan
from agents.ta_agent import analyze
from agents.ta_agent.analyzer import DEFAULT_TIMEFRAME
from bot.formatting import (
    DISCLAIMER,
    format_analysis,
    format_backtest,
    format_entry,
    format_exit,
    format_journal,
    format_news,
    format_open_trades,
    format_risk,
    format_scan,
    format_size,
    format_trade_closed,
)

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
    "   пример: /backtest BTCUSDT long 80000 78000 84000 1h\n"
    "• /scan [таймфрейм] — обзор топ-30 USDT-пар и подсветка ситуаций\n"
    "• /news — свежие крипто-новости\n"
    "• /entry BTCUSDT long [таймфрейм] — оценка условий входа (балл 0–100)\n"
    "• /exit BTCUSDT long 80300 [таймфрейм] — расчёт стопа и тейков по ATR и уровням\n"
    "• /size депозит риск% вход стоп [плечо] — размер позиции по риску\n"
    "   пример: /size 10000 1 80300 79500 5\n"
    "• /risk депозит риск% открытых_сделок — проверка риск-лимитов\n"
    "   пример: /risk 10000 1 2\n"
    "• /trade ПАРА сторона вход стоп тейк объём — записать сделку в журнал\n"
    "   пример: /trade BTCUSDT long 80300 79500 81200 0.125\n"
    "• /close id цена — закрыть сделку по цене выхода\n"
    "• /journal — статистика по закрытым сделкам\n"
    "• /opentrades — открытые сделки\n\n"
    f"{DISCLAIMER}"
)


def _user_id(message: Message) -> int:
    return message.from_user.id if message.from_user else 0


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


@router.message(Command("scan"))
async def cmd_scan(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    timeframe = args[0].lower() if args else DEFAULT_TIMEFRAME
    if timeframe not in ALLOWED_INTERVALS:
        await message.answer(
            f"Неподдерживаемый таймфрейм. Доступны: {', '.join(ALLOWED_INTERVALS)}."
        )
        return

    await message.answer("🔭 Сканирую топ-30, это займёт около минуты…")

    try:
        findings = await scan(timeframe=timeframe, top_n=30)
    except DataAgentError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /scan")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_scan(findings, timeframe, top=10))


@router.message(Command("news"))
async def cmd_news(message: Message) -> None:
    try:
        items = await get_crypto_news(limit=5)
    except Exception:
        logger.exception("Непредвиденная ошибка в /news")
        await message.answer(
            "Не удалось получить новости. Попробуй позже."
        )
        return

    await message.answer(format_news(items))


_ENTRY_USAGE = (
    "Формат: /entry ПАРА сторона [таймфрейм]\n"
    "Пример: /entry BTCUSDT long 1h\n"
    "Сторона: long или short."
)


@router.message(Command("entry"))
async def cmd_entry(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(_ENTRY_USAGE)
        return

    symbol = args[0].upper()
    side = args[1]
    timeframe = args[2].lower() if len(args) > 2 else DEFAULT_TIMEFRAME

    try:
        result = await score_entry(symbol, timeframe, side)
    except (EntryError, DataAgentError) as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /entry")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_entry(result))


_EXIT_USAGE = (
    "Формат: /exit ПАРА сторона цена_входа [таймфрейм]\n"
    "Пример: /exit BTCUSDT long 80300 1h\n"
    "Сторона: long или short."
)


@router.message(Command("exit"))
async def cmd_exit(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 3:
        await message.answer(_EXIT_USAGE)
        return

    symbol = args[0].upper()
    side = args[1]
    timeframe = args[3].lower() if len(args) > 3 else DEFAULT_TIMEFRAME

    try:
        entry = float(args[2].replace(",", "."))
    except ValueError:
        await message.answer("Цена входа должна быть числом.\n\n" + _EXIT_USAGE)
        return

    try:
        result = await plan_exit(symbol, timeframe, side, entry)
    except (ExitError, DataAgentError) as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /exit")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_exit(result))


_SIZE_USAGE = (
    "Формат: /size депозит риск% вход стоп [плечо]\n"
    "Пример: /size 10000 1 80300 79500 5\n"
    "Плечо опционально (по умолчанию 1)."
)


@router.message(Command("size"))
async def cmd_size(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 4:
        await message.answer(_SIZE_USAGE)
        return

    try:
        nums = [float(a.replace(",", ".")) for a in args[:5]]
    except ValueError:
        await message.answer("Все параметры должны быть числами.\n\n" + _SIZE_USAGE)
        return

    deposit, risk_pct, entry, stop = nums[0], nums[1], nums[2], nums[3]
    leverage = nums[4] if len(nums) > 4 else 1.0

    try:
        result = calc_position(deposit, risk_pct, entry, stop, leverage)
    except BudgetError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /size")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_size(result))


_RISK_USAGE = (
    "Формат: /risk депозит риск% открытых_сделок\n"
    "Пример: /risk 10000 1 2"
)


@router.message(Command("risk"))
async def cmd_risk(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(_RISK_USAGE)
        return

    try:
        deposit = float(args[0].replace(",", "."))
        risk_pct = float(args[1].replace(",", "."))
        open_trades = int(args[2]) if len(args) > 2 else 0
    except ValueError:
        await message.answer(
            "Депозит и риск — числа, открытых сделок — целое.\n\n" + _RISK_USAGE
        )
        return

    try:
        result = check_risk(deposit, risk_pct, open_trades)
    except RiskError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /risk")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_risk(result))


_TRADE_USAGE = (
    "Формат: /trade ПАРА сторона вход стоп тейк объём\n"
    "Пример: /trade BTCUSDT long 80300 79500 81200 0.125"
)


@router.message(Command("trade"))
async def cmd_trade(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 6:
        await message.answer(_TRADE_USAGE)
        return

    symbol, side = args[0].upper(), args[1]
    try:
        entry = float(args[2].replace(",", "."))
        stop = float(args[3].replace(",", "."))
        take = float(args[4].replace(",", "."))
        qty = float(args[5].replace(",", "."))
    except ValueError:
        await message.answer("Цены и объём должны быть числами.\n\n" + _TRADE_USAGE)
        return

    try:
        trade_id = add_trade(_user_id(message), symbol, side, entry, stop, take, qty)
    except JournalError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /trade")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(
        f"📝 Сделка записана, №{trade_id}.\n"
        f"{symbol} · {side.lower()}: вход {entry}, стоп {stop}, тейк {take}, объём {qty}.\n"
        f"Закрыть потом: /close {trade_id} цена_выхода"
    )


@router.message(Command("close"))
async def cmd_close(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Формат: /close id цена_выхода\nПример: /close 3 81000")
        return

    try:
        trade_id = int(args[0])
        exit_price = float(args[1].replace(",", "."))
    except ValueError:
        await message.answer("id — целое число, цена выхода — число. Пример: /close 3 81000")
        return

    try:
        result = close_trade(_user_id(message), trade_id, exit_price)
    except JournalError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /close")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_trade_closed(result))


@router.message(Command("journal"))
async def cmd_journal(message: Message) -> None:
    try:
        st = stats(_user_id(message))
    except Exception:
        logger.exception("Непредвиденная ошибка в /journal")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return
    await message.answer(format_journal(st))


@router.message(Command("opentrades"))
async def cmd_opentrades(message: Message) -> None:
    try:
        trades = open_trades(_user_id(message))
    except Exception:
        logger.exception("Непредвиденная ошибка в /opentrades")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return
    await message.answer(format_open_trades(trades))
