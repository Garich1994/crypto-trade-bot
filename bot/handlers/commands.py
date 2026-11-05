"""Telegram-хендлеры (aiogram 3.x)."""
from __future__ import annotations

import logging
import os

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from agents.alerts_agent import (
    CONDITION_TYPES,
    AlertError,
    add_alert,
    list_alerts,
    remove_alert,
)
from agents.autopush_agent import (
    AutopushError,
    get_autopush,
    set_autopush,
)
from agents.backtest_agent import BacktestError, backtest
from agents.broker_agent import (
    BrokerError,
    cancel_order,
    confirm_order,
    list_orders,
    place_limit_order,
    place_order,
    set_mode as broker_set_mode,
    status as broker_status,
)
from agents.broker_agent import engage_killswitch
from agents.budget_agent import BudgetError, calc_position
from agents.chart_agent import plot_analysis, plot_backtest
from agents.coordinator_agent import (
    AutobriefError,
    build_brief,
    get_autobrief,
    set_autobrief,
)
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
import agents.best_setups as best_setups
from agents.scanner_agent import scan
from agents.skeptic_agent import SkepticError, skeptic
from agents.ta_agent import analyze
from agents.whale_agent import LIQUIDATIONS_NOTE, get_whales
from agents.ta_agent.analyzer import DEFAULT_TIMEFRAME
from miniapp import DASHBOARD_URL
from bot.formatting import (
    DISCLAIMER,
    format_alert_added,
    format_analysis,
    format_best,
    format_brief,
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
    format_skeptic,
    format_trade_closed,
    format_watches,
    format_whales,
)

logger = logging.getLogger(__name__)
router = Router()

HELP_TEXT = (
    "📖 Все команды CryptoPilot\n\n"
    "• /start — приветствие и кнопка приложения\n"
    "• /help — этот список\n"
    "• /analyze BTCUSDT [таймфрейм] — технический разбор пары (с графиком)\n"
    "• /chart BTCUSDT [таймфрейм] — график пары (свечи, EMA, объём, RSI)\n"
    f"   таймфреймы: {', '.join(ALLOWED_INTERVALS)} (по умолчанию {DEFAULT_TIMEFRAME})\n"
    "   пример: /analyze ETHUSDT 4h\n"
    "• /backtest BTCUSDT long вход стоп тейк [таймфрейм] — прогон уровней по истории\n"
    "   пример: /backtest BTCUSDT long 80000 78000 84000 1h\n"
    "• /scan [таймфрейм] — обзор топ-30 USDT-пар и подсветка ситуаций\n"
    "• /news — свежие крипто-новости\n"
    "• /entry BTCUSDT long [таймфрейм] — оценка условий входа (балл 0–100)\n"
    "• /exit BTCUSDT long 80300 [таймфрейм] — расчёт стопа и тейков по ATR и уровням\n"
    "• /skeptic BTCUSDT long вход стоп тейк — проверка идеи сделки на прочность\n"
    "   пример: /skeptic BTCUSDT long 80300 79500 81200\n"
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
    "• /liquidations — о ликвидациях фьючерсов (недоступны из региона)\n"
    "• /best — топ-идеи: лучшие сетапы по правилам (скоринг ≥70, скептик, риск-гейт)\n"
    "• /brief — автобриф по рынку (новости + топ идеи + риск-гейт)\n"
    "• /autobrief on [часы] | off — авторассылка брифа каждые N часов (по умолчанию 4)\n"
    "• /autopush on [часы] | off | status — автопуш сильных наблюдений (по умолчанию выкл)\n"
    "• /dashboard — ссылка на веб-дашборд (демо на этом ПК)\n"
    "• /broker — режим исполнения (paper/testnet/live); /broker testnet|paper — переключить\n"
    "• /buy ПАРА объём [стоп] [тейк] · /sell … — маркет-ордер (в live — с подтверждением)\n"
    "• /buylimit ПАРА объём цена стоп [тейк] · /selllimit … — лимитный ордер\n"
    "• /orders — открытые лимитные ордера · /cancelorder id [ПАРА] — отмена\n"
    "• /killswitch — мгновенно заблокировать testnet/live\n\n"
    "ℹ️ Сторож (подписки) работает только пока запущен этот бот.\n"
    "🧪 Paper-режим строго виртуальный: без ключей биржи и без реальных ордеров.\n\n"
    f"{DISCLAIMER}"
)

# Публичный URL Mini App (HTTPS для Telegram WebApp); на ПК — демо на localhost.
MINIAPP_PUBLIC_URL = os.environ.get("MINIAPP_PUBLIC_URL", "http://localhost:8080").strip()

START_TEXT = (
    "👋 CryptoPilot — нейтральная аналитика крипторынка.\n"
    "Открой приложение кнопкой ниже или пользуйся командами.\n\n"
    "Ключевые команды:\n"
    "• /analyze BTCUSDT — теханализ пары с графиком\n"
    "• /scan — обзор рынка и сильные ситуации\n"
    "• /best — топ-идеи (лучшие сетапы по правилам)\n"
    "• /brief — автобриф (новости + топ идеи)\n"
    "• /entry BTCUSDT long — оценка условий входа\n"
    "• /paper — виртуальный портфель\n"
    "• /help — полный список команд\n\n"
    f"{DISCLAIMER}"
)


def _miniapp_keyboard() -> InlineKeyboardMarkup | None:
    """Кнопка запуска Mini App: WebApp для HTTPS, обычная ссылка для демо-localhost."""
    url = MINIAPP_PUBLIC_URL
    if url.startswith("https://"):
        btn = InlineKeyboardButton(
            text="📱 Открыть приложение", web_app=WebAppInfo(url=url)
        )
    else:
        # Telegram не принимает WebApp по http/localhost — отдаём обычную ссылку.
        btn = InlineKeyboardButton(text="📱 Открыть приложение (демо)", url=url)
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


def _user_id(message: Message) -> int:
    return message.from_user.id if message.from_user else 0


async def _send_and_cleanup(message: Message, path: str) -> None:
    """Отправить PNG и удалить временный файл после отправки."""
    try:
        await message.answer_photo(FSInputFile(path))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    try:
        await message.answer(START_TEXT, reply_markup=_miniapp_keyboard())
    except Exception:
        # Если Telegram не принял кнопку (напр. localhost) — отправим хотя бы текст.
        logger.exception("Не удалось отправить /start с кнопкой")
        await message.answer(
            START_TEXT + f"\n\n📱 Приложение: {MINIAPP_PUBLIC_URL}"
        )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


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

    # График прикладываем отдельно; его сбой не ломает текстовый ответ.
    try:
        path = await plot_analysis(symbol, timeframe)
        await _send_and_cleanup(message, path)
    except Exception:
        logger.exception("Не удалось построить график для /analyze")


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


@router.message(Command("chart"))
async def cmd_chart(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if not args:
        await message.answer("Формат: /chart ПАРА [таймфрейм]\nПример: /chart BTCUSDT 4h")
        return

    symbol = args[0].upper()
    timeframe = args[1].lower() if len(args) > 1 else DEFAULT_TIMEFRAME
    if timeframe not in ALLOWED_INTERVALS:
        await message.answer(
            f"Неподдерживаемый таймфрейм. Доступны: {', '.join(ALLOWED_INTERVALS)}."
        )
        return

    try:
        path = await plot_analysis(symbol, timeframe)
    except DataAgentError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /chart")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await _send_and_cleanup(message, path)


@router.message(Command("liquidations"))
async def cmd_liquidations(message: Message) -> None:
    await message.answer(LIQUIDATIONS_NOTE)


_MODE_RU = {"paper": "🧪 paper (виртуально)",
            "testnet": "🧪 testnet (фейк-деньги, настоящий API)",
            "live": "🔴 live (реальная биржа)"}


@router.message(Command("broker"))
async def cmd_broker(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip().lower()
    if arg in ("paper", "testnet"):
        try:
            broker_set_mode(arg)
        except BrokerError as exc:
            await message.answer(f"Не получилось: {exc}")
            return
        await message.answer(f"Режим переключён: {_MODE_RU.get(arg, arg)}")
        return
    if arg == "live":
        await message.answer(
            "Режим live включается только строкой BROKER_MODE=live в .env, не командой."
        )
        return

    st = broker_status()
    keys = "есть" if st["keys_present"] else "нет"
    await message.answer(
        "🏦 Брокер\n"
        f"Режим: {_MODE_RU.get(st['mode'], st['mode'])}\n"
        f"Killswitch: {'🔒 активен' if st['killswitch'] else 'выкл'}\n"
        f"Ключи в .env: {keys} (в коде ключей нет)\n"
        f"Базовый URL: {st['base_url']}\n\n"
        "Переключение: /broker testnet | /broker paper (live — только через .env)."
    )


async def _broker_order(message: Message, command: CommandObject, side: str) -> None:
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(
            f"Формат: /{side} ПАРА объём [стоп] [тейк]\nПример: /{side} BTCUSDT 0.01"
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
        res = await place_order(_user_id(message), symbol, side, qty, stop, take)
    except (BrokerError, DataAgentError) as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /%s", side)
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    st = res["status"]
    if st == "blocked":
        await message.answer(f"⛔ Ордер не отправлен: {res['reason']}")
    elif st == "needs_confirmation":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"brk:{res['token']}"),
            InlineKeyboardButton(text="Отмена", callback_data="brk:cancel"),
        ]])
        await message.answer(
            f"🔴 {res['warning']}\n\n"
            f"{side.upper()} {res['symbol']} · {res['qty']} по ~{res['price']}\n"
            "Нажми «Подтвердить», чтобы отправить ордер.",
            reply_markup=kb,
        )
    else:  # executed
        await message.answer(
            f"✅ Ордер исполнен ({res['mode']}): {side.upper()} {res['symbol']} "
            f"{res['qty']} по {res['price']}."
        )


@router.message(Command("buy"))
async def cmd_buy(message: Message, command: CommandObject) -> None:
    await _broker_order(message, command, "buy")


@router.message(Command("sell"))
async def cmd_sell(message: Message, command: CommandObject) -> None:
    await _broker_order(message, command, "sell")


@router.message(Command("killswitch"))
async def cmd_killswitch(message: Message) -> None:
    engage_killswitch()
    await message.answer(
        "🔒 Killswitch активирован. Режимы testnet и live заблокированы.\n"
        "Разблокировка — вручную в .env (BROKER_KILLSWITCH_RESET=1) и перезапуск бота."
    )


async def _broker_limit(message: Message, command: CommandObject, side: str) -> None:
    cmd = "buylimit" if side == "buy" else "selllimit"
    args = (command.args or "").split()
    if len(args) < 4:
        await message.answer(
            f"Формат: /{cmd} ПАРА объём цена стоп [тейк]\n"
            f"Пример: /{cmd} BTCUSDT 0.01 65000 63000 70000"
        )
        return
    symbol = args[0].upper()
    try:
        qty = float(args[1].replace(",", "."))
        price = float(args[2].replace(",", "."))
        stop = float(args[3].replace(",", "."))
        take = float(args[4].replace(",", ".")) if len(args) > 4 else None
    except ValueError:
        await message.answer("Объём, цена, стоп и тейк должны быть числами.")
        return

    try:
        res = await place_limit_order(_user_id(message), symbol, side, qty, price, stop, take)
    except (BrokerError, DataAgentError) as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /%s", cmd)
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    st = res["status"]
    if st == "blocked":
        await message.answer(f"⛔ Ордер не выставлен: {res['reason']}")
    elif st == "needs_confirmation":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"brk:{res['token']}"),
            InlineKeyboardButton(text="Отмена", callback_data="brk:cancel"),
        ]])
        await message.answer(
            f"🔴 {res['warning']}\n\n"
            f"ЛИМИТ {side.upper()} {res['symbol']} · {res['qty']} по {res['price']} "
            f"(сумма {res['amount']:.2f})\nНажми «Подтвердить», чтобы выставить ордер.",
            reply_markup=kb,
        )
    else:  # placed
        await message.answer(
            f"✅ Лимитный ордер выставлен ({res['mode']}): {side.upper()} {res['symbol']} "
            f"· {res['qty']} по {res['limit_price']} (сумма {res['amount']:.2f})\n"
            f"orderId: {res['orderId']}"
        )


@router.message(Command("buylimit"))
async def cmd_buylimit(message: Message, command: CommandObject) -> None:
    await _broker_limit(message, command, "buy")


@router.message(Command("selllimit"))
async def cmd_selllimit(message: Message, command: CommandObject) -> None:
    await _broker_limit(message, command, "sell")


@router.message(Command("orders"))
async def cmd_orders(message: Message) -> None:
    try:
        res = await list_orders(_user_id(message))
    except (BrokerError, DataAgentError) as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /orders")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    orders = res.get("orders") or []
    if not orders:
        await message.answer(f"Открытых лимитных ордеров нет ({res['mode']}).")
        return
    lines = [f"📋 Лимитные ордера ({res['mode']}):", ""]
    for o in orders:
        lines.append(
            f"№{o['orderId']} · {o['symbol']} · {str(o['side']).upper()} "
            f"{o['qty']} по {o['price']}"
        )
    lines.append("\nОтмена: /cancelorder id [ПАРА]")
    await message.answer("\n".join(lines))


@router.message(Command("cancelorder"))
async def cmd_cancelorder(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if not args:
        await message.answer("Формат: /cancelorder id [ПАРА]\nПример: /cancelorder 3")
        return
    try:
        order_id = int(args[0])
    except ValueError:
        await message.answer("id ордера — целое число.")
        return
    symbol = args[1].upper() if len(args) > 1 else None

    try:
        res = await cancel_order(_user_id(message), order_id, symbol)
    except (BrokerError, DataAgentError) as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /cancelorder")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(f"🗑 Лимитный ордер №{res['orderId']} отменён ({res['mode']}).")


@router.callback_query(F.data.startswith("brk:"))
async def cb_broker_confirm(callback: CallbackQuery) -> None:
    token = callback.data.split(":", 1)[1]
    if token == "cancel":
        await callback.message.edit_text("Ордер отменён.")
        await callback.answer()
        return

    user_id = callback.from_user.id if callback.from_user else 0
    try:
        res = await confirm_order(user_id, token)
    except (BrokerError, DataAgentError) as exc:
        await callback.message.edit_text(f"Не получилось: {exc}")
        await callback.answer()
        return
    except Exception:
        logger.exception("Ошибка подтверждения ордера")
        await callback.message.edit_text("Внутренняя ошибка при исполнении.")
        await callback.answer()
        return

    if res["status"] == "executed":
        if res.get("order_type") == "limit":
            await callback.message.edit_text(
                f"✅ Лимитный ордер выставлен (live): {res['side'].upper()} "
                f"{res['symbol']} {res['qty']} по {res['limit_price']} "
                f"(orderId: {res.get('orderId')})."
            )
        else:
            await callback.message.edit_text(
                f"✅ Ордер исполнен (live): {res['side'].upper()} {res['symbol']} "
                f"{res['qty']} по {res['price']}."
            )
    else:
        await callback.message.edit_text(f"⛔ {res.get('reason', 'ордер не отправлен')}")
    await callback.answer()


@router.message(Command("dashboard"))
async def cmd_dashboard(message: Message) -> None:
    text = (
        "📊 Веб-дашборд CryptoPilot.\n"
        "ℹ️ Демо работает на этом ПК. Чтобы открывать прямо из Telegram как Mini App, "
        "нужен HTTPS-хостинг — это следующий шаг."
    )
    try:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Открыть дашборд", url=DASHBOARD_URL)]
            ]
        )
        await message.answer(text, reply_markup=kb)
    except Exception:
        # Telegram может не принять localhost-URL в кнопке — отдаём ссылкой текстом.
        await message.answer(f"{text}\n\nАдрес: {DASHBOARD_URL}")


@router.message(Command("best"))
async def cmd_best(message: Message) -> None:
    """Топ-идеи: лучшие сетапы по правилам (скоринг ≥70, скептик, риск-гейт)."""
    data = best_setups.get_cached()
    if data is None:
        await message.answer("💡 Считаю топ-идеи, это займёт до минуты…")
        try:
            data = await best_setups.refresh_now()
        except Exception:
            logger.exception("Непредвиденная ошибка в /best")
            await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
            return
    await message.answer(format_best(data))


@router.message(Command("brief"))
async def cmd_brief(message: Message) -> None:
    await message.answer("📋 Собираю бриф, это займёт несколько секунд…")
    try:
        brief = await build_brief(top=5)
    except DataAgentError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /brief")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return
    await message.answer(format_brief(brief, auto=False))

    # График топ-1 идеи с уровнями.
    ideas = brief.get("ideas") or []
    if ideas:
        top = ideas[0]
        try:
            path = await plot_backtest(
                top["symbol"], top["entry"], top["stop"],
                [top["take1"], top["take2"]], brief.get("timeframe", "1h"),
            )
            await _send_and_cleanup(message, path)
        except Exception:
            logger.exception("Не удалось построить график для /brief")


@router.message(Command("autobrief"))
async def cmd_autobrief(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if not args or args[0].lower() not in ("on", "off"):
        current = get_autobrief(_user_id(message))
        state = (
            f"сейчас: каждые {current['interval_h']:g} ч"
            if current and current["active"]
            else "сейчас: выключен"
        )
        await message.answer(
            "Формат: /autobrief on [часы] | off\n"
            "Пример: /autobrief on 4\n" + state
        )
        return

    if args[0].lower() == "off":
        try:
            set_autobrief(_user_id(message), active=False)
        except Exception:
            logger.exception("Ошибка /autobrief off")
            await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
            return
        await message.answer("🔕 Автодайджест выключен.")
        return

    interval = 4.0
    if len(args) > 1:
        try:
            interval = float(args[1].replace(",", "."))
        except ValueError:
            await message.answer("Часы должны быть числом. Пример: /autobrief on 4")
            return

    try:
        sub = set_autobrief(_user_id(message), active=True, interval_h=interval)
    except AutobriefError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Ошибка /autobrief on")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(
        f"🔔 Автодайджест включён: каждые {sub['interval_h']:g} ч.\n"
        "🤖 Автодайджест. Наблюдения, не рекомендации. Не финансовый совет.\n"
        "ℹ️ Работает только пока запущен бот. Выключить: /autobrief off"
    )


@router.message(Command("autopush"))
async def cmd_autopush(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    action = args[0].lower() if args else "status"

    if action == "status":
        sub = get_autopush(_user_id(message))
        if sub and sub["active"]:
            txt = f"включён, каждые {sub['interval_h']:g} ч"
        else:
            txt = "выключен"
        await message.answer(
            f"🔔 Автопуш наблюдений: {txt}.\n"
            "Порог: сила сетапа ≥ 75, риск-гейт PASS, скептик без FAIL.\n"
            "Антиспам: пара+сторона раз в 12ч, ≤5 в сутки, тихий час 00:00–07:00 (NY).\n"
            "Включить: /autopush on [часы] · выключить: /autopush off"
        )
        return

    if action == "off":
        try:
            set_autopush(_user_id(message), active=False)
        except Exception:
            logger.exception("Ошибка /autopush off")
            await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
            return
        await message.answer("🔕 Автопуш наблюдений выключен.")
        return

    if action != "on":
        await message.answer("Формат: /autopush on [часы] | off | status")
        return

    interval = 2.0
    if len(args) > 1:
        try:
            interval = float(args[1].replace(",", "."))
        except ValueError:
            await message.answer("Часы должны быть числом. Пример: /autopush on 2")
            return

    try:
        sub = set_autopush(_user_id(message), active=True, interval_h=interval)
    except AutopushError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Ошибка /autopush on")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(
        f"🔔 Автопуш наблюдений включён: каждые {sub['interval_h']:g} ч.\n"
        "Пушим только сильные сетапы (≥75) с PASS риск-гейта и без FAIL скептика.\n"
        "Наблюдения, не рекомендации. Не финансовый совет.\n"
        "ℹ️ Работает только пока запущен бот. Выключить: /autopush off"
    )


_SKEPTIC_USAGE = (
    "Формат: /skeptic ПАРА сторона вход стоп тейк\n"
    "Пример: /skeptic BTCUSDT long 80300 79500 81200"
)


@router.message(Command("skeptic"))
async def cmd_skeptic(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 5:
        await message.answer(_SKEPTIC_USAGE)
        return

    symbol, side = args[0].upper(), args[1]
    try:
        entry = float(args[2].replace(",", "."))
        stop = float(args[3].replace(",", "."))
        take = float(args[4].replace(",", "."))
    except ValueError:
        await message.answer("Вход, стоп и тейк должны быть числами.\n\n" + _SKEPTIC_USAGE)
        return

    try:
        result = await skeptic(symbol, side, entry, stop, take)
    except (SkepticError, DataAgentError) as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    except Exception:
        logger.exception("Непредвиденная ошибка в /skeptic")
        await message.answer("Произошла внутренняя ошибка. Попробуй позже.")
        return

    await message.answer(format_skeptic(result))
