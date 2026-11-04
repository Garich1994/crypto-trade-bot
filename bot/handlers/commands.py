"""Telegram-хендлеры (aiogram 3.x)."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from agents.alerts_agent import (
    CONDITION_TYPES,
    AlertError,
    add_alert,
    list_alerts,
    remove_alert,
)
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
from agents.data_agent import get_ticker_24h
from agents.news_agent import get_crypto_news
from agents.paper_agent import (
    PaperError,
    close_position,
    get_position,
    open_position,
    portfolio,
    reset as paper_reset,
)
from agents.risk_agent import RiskError, check_risk
from agents.scanner_agent import scan
from agents.ta_agent import analyze
from agents.whale_agent import LIQUIDATIONS_NOTE, get_whales
from agents.ta_agent.analyzer import DEFAULT_TIMEFRAME
from bot.formatting import (
    DISCLAIMER,
    format_alert_added,
    format_analysis,
    format_backtest,
    format_entry,
    format_exit,
    format_journal,
    format_news,
    format_open_trades,
    format_paper_closed,
    format_paper_opened,
    format_paper_portfolio,
    format_risk,
    format_scan,
    format_size,
    format_trade_closed,
    format_watches,
    format_whales,
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
    "• /opentrades — открытые сделки\n"
    "• /watch ПАРА тип порог — подписка на условие (одноразовая)\n"
    "   типы: price_above, price_below, rsi_above, rsi_below, change24h_abs, volume_spike\n"
    "   пример: /watch BTCUSDT price_above 81000\n"
    "• /watches — активные подписки, /unwatch id — отменить\n"
    "• /paper — виртуальный портфель и позиции\n"
    "• /paperbuy BTCUSDT 0.01 [стоп] [тейк] — открыть виртуальный лонг по рынку\n"
    "• /papersell BTCUSDT 0.01 [стоп] [тейк] — открыть виртуальный шорт\n"
    "• /paperclose id — закрыть виртуальную позицию по рынку\n"
    "• /paperreset ДА — сбросить виртуальный портфель к 10 000\n"
    "• /whales [ПАРА] — крупные (китовые) сделки на споте за недавнее окно\n"
    "• /liquidations — о ликвидациях фьючерсов (недоступны из региона)\n\n"
    "ℹ️ Сторож (подписки) работает только пока запущен этот бот.\n"
    "🧪 Paper-режим строго виртуальный: без ключей биржи и без реальных ордеров.\n\n"
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


_WATCH_USAGE = (
    "Формат: /watch ПАРА тип порог\n"
    "Типы: " + ", ".join(CONDITION_TYPES) + "\n"
    "Пример: /watch BTCUSDT price_above 81000"
)


@router.message(Command("watch"))
async def cmd_watch(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 3:
        await message.answer(_WATCH_USAGE)
        return

    symbol, condition_type = args[0].upper(), args[1].lower()
    try:
        threshold = float(args[2].replace(",", "."))
    except ValueError:
        await message.answer("Порог должен быть числом.\n\n" + _WATCH_USAGE)
        return

    try:
        alert = add_alert(_user_id(message), symbol, condition_type, threshold)
    except AlertError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /watch")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_alert_added(alert))


@router.message(Command("watches"))
async def cmd_watches(message: Message) -> None:
    try:
        alerts = list_alerts(_user_id(message))
    except Exception:
        logger.exception("Непредвиденная ошибка в /watches")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return
    await message.answer(format_watches(alerts))


@router.message(Command("unwatch"))
async def cmd_unwatch(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if not args:
        await message.answer("Формат: /unwatch id\nПример: /unwatch 2")
        return

    try:
        alert_id = int(args[0])
    except ValueError:
        await message.answer("id подписки — целое число. Пример: /unwatch 2")
        return

    try:
        remove_alert(_user_id(message), alert_id)
    except AlertError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /unwatch")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(f"🗑 Подписка №{alert_id} отменена.")


@router.message(Command("paper"))
async def cmd_paper(message: Message) -> None:
    try:
        data = portfolio(_user_id(message))
    except Exception:
        logger.exception("Непредвиденная ошибка в /paper")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return
    await message.answer(format_paper_portfolio(data))


async def _paper_open(message: Message, command: CommandObject, side: str) -> None:
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(
            f"Формат: /{'paperbuy' if side == 'long' else 'papersell'} ПАРА объём [стоп] [тейк]\n"
            f"Пример: /{'paperbuy' if side == 'long' else 'papersell'} BTCUSDT 0.01"
        )
        return

    symbol = args[0].upper()
    try:
        qty = float(args[1].replace(",", "."))
        stop = float(args[2].replace(",", ".")) if len(args) > 2 else None
        take = float(args[3].replace(",", ".")) if len(args) > 3 else None
    except ValueError:
        await message.answer("Объём, стоп и тейк должны быть числами.")
        return

    try:
        ticker = await get_ticker_24h(symbol)
        price = float(ticker["lastPrice"])
        pos = open_position(_user_id(message), symbol, side, qty, price, stop, take)
    except (PaperError, DataAgentError) as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /paperbuy|/papersell")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_paper_opened(pos))


@router.message(Command("paperbuy"))
async def cmd_paperbuy(message: Message, command: CommandObject) -> None:
    await _paper_open(message, command, "long")


@router.message(Command("papersell"))
async def cmd_papersell(message: Message, command: CommandObject) -> None:
    await _paper_open(message, command, "short")


@router.message(Command("paperclose"))
async def cmd_paperclose(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if not args:
        await message.answer("Формат: /paperclose id\nПример: /paperclose 1")
        return

    try:
        position_id = int(args[0])
    except ValueError:
        await message.answer("id позиции — целое число. Пример: /paperclose 1")
        return

    pos = get_position(_user_id(message), position_id)
    if pos is None or pos["status"] != "open":
        await message.answer(f"Не получилось: открытая позиция №{position_id} не найдена.")
        return

    try:
        ticker = await get_ticker_24h(pos["symbol"])
        price = float(ticker["lastPrice"])
        result = close_position(_user_id(message), position_id, price)
    except (PaperError, DataAgentError) as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /paperclose")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_paper_closed(result))


@router.message(Command("paperreset"))
async def cmd_paperreset(message: Message, command: CommandObject) -> None:
    confirm = (command.args or "").strip().upper()
    if confirm != "ДА":
        await message.answer(
            "Сброс виртуального портфеля к 10 000 USDT (позиции будут удалены).\n"
            "Для подтверждения отправь: /paperreset ДА"
        )
        return

    try:
        balance = paper_reset(_user_id(message))
    except Exception:
        logger.exception("Непредвиденная ошибка в /paperreset")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(
        f"🧪 Виртуальный портфель сброшен. Баланс: {balance:.2f} USDT.\n\n"
        "🧪 Виртуальный портфель. Не финансовый совет."
    )


@router.message(Command("whales"))
async def cmd_whales(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    symbol = args[0].upper() if args else "BTCUSDT"

    try:
        result = await get_whales(symbol)
    except DataAgentError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /whales")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_whales(result))


@router.message(Command("liquidations"))
async def cmd_liquidations(message: Message) -> None:
    await message.answer(LIQUIDATIONS_NOTE)
