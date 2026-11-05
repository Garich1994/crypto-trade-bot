"""Самопроверка: прогон analyze на BTCUSDT и ETHUSDT (таймфрейм 1h).

Проверяет, что все индикаторы посчитались и сообщение сформировалось.
Зелёные галочки = всё ок. Запуск:  python selftest.py
"""
from __future__ import annotations

import asyncio
import math
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

# На Windows консоль по умолчанию cp1252 и не печатает кириллицу — переключаем в UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from agents.alerts_agent import (
    add_alert,
    evaluate_condition,
    list_alerts,
    remove_alert,
    run_check,
)
from agents.alerts_agent import set_db_path as set_alerts_db
from agents.backtest_agent import BacktestError, backtest
import agents.autopush_agent.autopush as autopush
from agents.autopush_agent import evaluate_push
import agents.broker_agent.broker as broker
from agents.budget_agent import calc_position
from agents.chart_agent import CHART_LABELS, render_analysis, render_backtest
import agents.coordinator_agent.pipeline as coord_pipeline
from agents.coordinator_agent import (
    build_brief,
    due_subscriptions,
    get_autobrief,
    set_autobrief,
)
from agents.coordinator_agent import set_db_path as set_brief_db
from agents.data_agent import DataAgentError, get_klines, get_klines_history
from agents.entry_agent import EntryError, score_entry
from agents.exit_agent import ExitError, plan_exit
from agents.journal_agent import (
    add_trade,
    close_trade,
    open_trades,
    set_db_path,
    stats,
)
import miniapp.server as miniapp_server
from agents.market_index import compute as mi_compute
from agents.news_agent import get_crypto_news
from agents.scanner_agent import score_setup
from agents.paper_agent import (
    close_position,
    get_balance,
    open_position,
    portfolio,
    reset as paper_reset,
    run_limit_check,
    run_paper_check,
)
from agents.paper_agent import set_db_path as set_paper_db
from agents.risk_agent import check_risk
from agents.scanner_agent import scan
from agents.skeptic_agent import analyze_idea
from agents.ta_agent import analyze
from agents.whale_agent import LIQUIDATIONS_NOTE, analyze_whales
from bot.formatting import (
    DISCLAIMER,
    DISCLAIMER_BACKTEST,
    format_alert_fired,
    format_analysis,
    format_backtest,
    format_brief,
    format_entry,
    format_exit,
    format_journal,
    format_news,
    format_open_trades,
    format_paper_closed,
    format_paper_opened,
    format_paper_portfolio,
    format_push,
    format_risk,
    format_scan,
    format_size,
    format_skeptic,
    format_whales,
)

OK = "\033[92m[OK]\033[0m"
FAIL = "\033[91m[X]\033[0m"

# Числовые поля, которые обязаны посчитаться (конечное число).
REQUIRED_NUMBERS = ("price", "rsi", "atr", "atr_pct")
# Текстовые поля состояния, которые обязаны быть непустыми.
REQUIRED_TEXTS = ("trend", "rsi_state", "macd_state", "volatility_state", "volume_state")
# Вложенные блоки индикаторов.
REQUIRED_NESTED = {
    "ema": ("ema20", "ema50", "ema200"),
    "bollinger": ("upper", "middle", "lower"),
}
# Запрещённые слова в пользовательском тексте (нейтральность).
FORBIDDEN_WORDS = ("покупай", "продавай", "сигнал", "входи")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _check_result(result: dict, errors: list[str]) -> None:
    tag = f"{result.get('symbol')}/{result.get('timeframe')}"

    for key in REQUIRED_NUMBERS:
        if not _is_number(result.get(key)):
            errors.append(f"{tag}: поле '{key}' не посчиталось ({result.get(key)!r})")

    for key in REQUIRED_TEXTS:
        if not str(result.get(key, "")).strip():
            errors.append(f"{tag}: поле '{key}' пустое")

    for block, subkeys in REQUIRED_NESTED.items():
        data = result.get(block, {})
        for sub in subkeys:
            if not _is_number(data.get(sub)):
                errors.append(f"{tag}: {block}.{sub} не посчиталось")

    # Уровни S/R могут быть None (не нашлись в окне) — это допустимо,
    # но ключи должны присутствовать.
    for key in ("support", "resistance", "funding_rate", "change_24h"):
        if key not in result:
            errors.append(f"{tag}: нет ключа '{key}'")


def _check_message(result: dict, errors: list[str]) -> str:
    tag = f"{result.get('symbol')}/{result.get('timeframe')}"
    message = format_analysis(result)

    if not message.strip():
        errors.append(f"{tag}: сообщение не сформировалось")
    if DISCLAIMER not in message:
        errors.append(f"{tag}: в сообщении нет дисклеймера")

    low = message.lower()
    for word in FORBIDDEN_WORDS:
        if word in low:
            errors.append(f"{tag}: в тексте запрещённое слово «{word}»")
    return message


async def _check_backtest(errors: list[str]) -> None:
    """Прогон бэктеста на BTCUSDT 1h с реалистичными уровнями из истории."""
    symbol, timeframe = "BTCUSDT", "1h"
    print(f"\n— Проверяю бэктест {symbol} ({timeframe}) …")

    # Уровни берём из самой истории, чтобы вход гарантированно касался цены.
    try:
        candles = await get_klines_history(symbol, timeframe, 5000)
    except DataAgentError as exc:
        errors.append(f"backtest {symbol}: история недоступна — {exc}")
        print(f"  {FAIL} не удалось получить историю: {exc}")
        return

    lows = [float(c[3]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lo, hi = min(lows), max(highs)
    span = hi - lo
    entry = (lo + hi) / 2            # середина диапазона — её цена проходит
    stop = entry - span * 0.1
    take = entry + span * 0.1

    try:
        result = await backtest(symbol, timeframe, "long", entry, stop, take)
    except (BacktestError, DataAgentError) as exc:
        errors.append(f"backtest {symbol}: {exc}")
        print(f"  {FAIL} {exc}")
        return

    before = len(errors)
    if not (isinstance(result.get("trades"), int) and result["trades"] > 0):
        errors.append(f"backtest {symbol}: trades не > 0 ({result.get('trades')!r})")
    wr = result.get("win_rate")
    if not (_is_number(wr) and 0.0 <= wr <= 1.0):
        errors.append(f"backtest {symbol}: win_rate вне [0,1] ({wr!r})")
    if not _is_number(result.get("expectancy")):
        errors.append(f"backtest {symbol}: expectancy не число ({result.get('expectancy')!r})")

    message = format_backtest(result)
    if not message.strip():
        errors.append(f"backtest {symbol}: сообщение не сформировалось")
    if DISCLAIMER_BACKTEST not in message:
        errors.append(f"backtest {symbol}: нет дисклеймера")
    low = message.lower()
    for word in FORBIDDEN_WORDS:
        if word in low:
            errors.append(f"backtest {symbol}: запрещённое слово «{word}»")

    if len(errors) == before:
        print(f"  {OK} бэктест посчитан, сообщение сформировано")
        print("  ----- превью ответа -----")
        for line in message.splitlines():
            print(f"  {line}")
        print("  --------------------------")


def _check_forbidden(message: str, tag: str, errors: list[str]) -> None:
    low = message.lower()
    for word in FORBIDDEN_WORDS:
        if word in low:
            errors.append(f"{tag}: запрещённое слово «{word}»")


async def _check_scan(errors: list[str]) -> None:
    """Скан по топ-5 парам: должен вернуть список (возможно пустой)."""
    print("\n— Проверяю скан (топ-5, 1h) …")
    try:
        findings = await scan(timeframe="1h", top_n=5)
    except DataAgentError as exc:
        errors.append(f"scan: данные недоступны — {exc}")
        print(f"  {FAIL} {exc}")
        return

    before = len(errors)
    if not isinstance(findings, list):
        errors.append(f"scan: ожидался список, получено {type(findings).__name__}")
    else:
        for f in findings:
            if not isinstance(f.get("patterns"), list) or not f["patterns"]:
                errors.append(f"scan: находка без паттернов ({f.get('symbol')})")

    message = format_scan(findings, "1h", top=10)
    if not message.strip():
        errors.append("scan: сообщение не сформировалось")
    _check_forbidden(message, "scan", errors)

    if len(errors) == before:
        print(f"  {OK} скан вернул список ({len(findings)} находок), сообщение ок")
        print("  ----- превью ответа -----")
        for line in message.splitlines():
            print(f"  {line}")
        print("  --------------------------")


async def _check_news(errors: list[str]) -> None:
    """Новости: get_crypto_news возвращает список (>=0) без падения."""
    print("\n— Проверяю новости …")
    try:
        items = await get_crypto_news(limit=5)
    except Exception as exc:  # noqa: BLE001 — агент не должен падать
        errors.append(f"news: упал с ошибкой — {exc!r}")
        print(f"  {FAIL} упал: {exc!r}")
        return

    before = len(errors)
    if not isinstance(items, list):
        errors.append(f"news: ожидался список, получено {type(items).__name__}")
    else:
        for it in items:
            if not it.get("title") or not it.get("url"):
                errors.append("news: новость без заголовка или ссылки")

    message = format_news(items)
    if not message.strip():
        errors.append("news: сообщение не сформировалось")
    _check_forbidden(message, "news", errors)

    if len(errors) == before:
        note = "" if items else " (сейчас свежих новостей/доступа нет — это ок)"
        print(f"  {OK} новости: {len(items)} шт.{note}")


async def _check_entry(errors: list[str]) -> None:
    """Скоринг входа: score в [0,100], непустые factors, нет запрещённых слов."""
    print("\n— Проверяю скоринг входа BTCUSDT long (1h) …")
    try:
        result = await score_entry("BTCUSDT", "1h", "long")
    except (EntryError, DataAgentError) as exc:
        errors.append(f"entry: {exc}")
        print(f"  {FAIL} {exc}")
        return

    before = len(errors)
    score = result.get("score")
    if not (isinstance(score, int) and 0 <= score <= 100):
        errors.append(f"entry: score вне [0,100] ({score!r})")
    if not result.get("factors"):
        errors.append("entry: factors пустые")
    if not str(result.get("summary", "")).strip():
        errors.append("entry: summary пустой")

    message = format_entry(result)
    if not message.strip():
        errors.append("entry: сообщение не сформировалось")
    _check_forbidden(message, "entry", errors)

    if len(errors) == before:
        print(f"  {OK} score={score}, факторов {len(result['factors'])}, текст ок")
        print("  ----- превью ответа -----")
        for line in message.splitlines():
            print(f"  {line}")
        print("  --------------------------")


async def _check_exit(errors: list[str]) -> None:
    """План выхода: для лонга stop < entry < take1 < take2, нет запрещённых слов."""
    print("\n— Проверяю план выхода BTCUSDT long (1h) …")
    try:
        raw = await get_klines("BTCUSDT", "1h", limit=5)
        entry = float(raw[-1][4])  # реалистичный вход = последняя цена
        result = await plan_exit("BTCUSDT", "1h", "long", entry)
    except (ExitError, DataAgentError) as exc:
        errors.append(f"exit: {exc}")
        print(f"  {FAIL} {exc}")
        return

    before = len(errors)
    stop, e, t1, t2 = result["stop"], result["entry"], result["take1"], result["take2"]
    if not (stop < e < t1 < t2):
        errors.append(
            f"exit: нарушен порядок уровней (stop={stop}, entry={e}, t1={t1}, t2={t2})"
        )
    for key in ("rr1", "rr2"):
        if not _is_number(result.get(key)):
            errors.append(f"exit: {key} не число ({result.get(key)!r})")

    message = format_exit(result)
    if not message.strip():
        errors.append("exit: сообщение не сформировалось")
    _check_forbidden(message, "exit", errors)

    if len(errors) == before:
        print(f"  {OK} уровни корректны: stop < entry < take1 < take2, текст ок")
        print("  ----- превью ответа -----")
        for line in message.splitlines():
            print(f"  {line}")
        print("  --------------------------")


def _check_size(errors: list[str]) -> None:
    """Размер позиции: risk_usdt==100, qty≈0.125 для (10000,1,80300,79500)."""
    print("\n— Проверяю размер позиции (10000, 1%, 80300/79500) …")
    result = calc_position(10000, 1, 80300, 79500)

    before = len(errors)
    if abs(result["risk_usdt"] - 100.0) > 1e-6:
        errors.append(f"size: risk_usdt != 100 ({result['risk_usdt']!r})")
    if abs(result["qty"] - 0.125) > 1e-6:
        errors.append(f"size: qty != 0.125 ({result['qty']!r})")

    message = format_size(result)
    if not message.strip():
        errors.append("size: сообщение не сформировалось")
    _check_forbidden(message, "size", errors)

    if len(errors) == before:
        print(f"  {OK} risk_usdt={result['risk_usdt']:.2f}, qty={result['qty']}, текст ок")
        print("  ----- превью ответа -----")
        for line in message.splitlines():
            print(f"  {line}")
        print("  --------------------------")


def _check_risk(errors: list[str]) -> None:
    """Риск-лимиты: risk_pct=5 должен дать предупреждение (не 'в пределах лимитов')."""
    print("\n— Проверяю риск-лимиты (10000, 5%, 2 сделки) …")
    result = check_risk(10000, 5, 2)

    before = len(errors)
    if result["per_trade_ok"]:
        errors.append("risk: risk_pct=5 ошибочно признан в норме (ожидалось предупреждение)")
    if result["verdict"] == "В пределах лимитов.":
        errors.append("risk: при risk_pct=5 вердикт не должен быть 'в пределах лимитов'")
    if not str(result.get("advice", "")).strip():
        errors.append("risk: пустой совет")

    message = format_risk(result)
    if not message.strip():
        errors.append("risk: сообщение не сформировалось")
    _check_forbidden(message, "risk", errors)

    if len(errors) == before:
        print(f"  {OK} предупреждение есть: «{result['verdict']}», текст ок")
        print("  ----- превью ответа -----")
        for line in message.splitlines():
            print(f"  {line}")
        print("  --------------------------")


def _check_journal(errors: list[str]) -> None:
    """Журнал на временной БД: 2 сделки (прибыль+убыток), stats сходится."""
    print("\n— Проверяю журнал сделок (временная БД) …")
    tmp_dir = tempfile.mkdtemp(prefix="journal_selftest_")
    uid, other = 111, 222
    before = len(errors)
    try:
        set_db_path(f"{tmp_dir}/journal.db")

        # Прибыльная: long 100→110 при стопе 90 (qty 1): pnl=+10, R=+1.
        t1 = add_trade(uid, "BTCUSDT", "long", 100, 90, 120, 1)
        # Убыточная: long 100→95 при стопе 90 (qty 1): pnl=-5, R=-0.5.
        t2 = add_trade(uid, "ETHUSDT", "long", 100, 90, 120, 1)

        if len(open_trades(uid)) != 2:
            errors.append("journal: ожидалось 2 открытые сделки")

        r1 = close_trade(uid, t1, 110)
        r2 = close_trade(uid, t2, 95)
        if abs(r1["pnl_usdt"] - 10.0) > 1e-9 or abs(r1["pnl_r"] - 1.0) > 1e-9:
            errors.append(f"journal: PnL сделки 1 неверен ({r1})")
        if abs(r2["pnl_usdt"] + 5.0) > 1e-9 or abs(r2["pnl_r"] + 0.5) > 1e-9:
            errors.append(f"journal: PnL сделки 2 неверен ({r2})")

        st = stats(uid)
        if st["total"] != 2:
            errors.append(f"journal: total != 2 ({st['total']})")
        if abs(st["win_rate"] - 0.5) > 1e-9:
            errors.append(f"journal: win_rate != 50% ({st['win_rate']})")
        # Ручная сверка суммарного PnL: +10 + (-5) = +5.
        if abs(st["total_pnl_usdt"] - 5.0) > 1e-9:
            errors.append(f"journal: total_pnl_usdt != 5 ({st['total_pnl_usdt']})")

        # Изоляция: у другого пользователя этих сделок нет.
        if stats(other)["total"] != 0:
            errors.append("journal: нарушена изоляция пользователей")
        try:
            close_trade(other, t1, 110)
            errors.append("journal: чужую сделку удалось закрыть")
        except Exception:
            pass  # ожидаемо: чужая сделка «не найдена»

        message = format_journal(st)
        _check_forbidden(message, "journal", errors)
        _check_forbidden(format_open_trades(open_trades(uid)), "opentrades", errors)

        if len(errors) == before:
            print(f"  {OK} total=2, win_rate=50%, PnL=+5 USDT, изоляция работает")
            print("  ----- превью ответа -----")
            for line in message.splitlines():
                print(f"  {line}")
            print("  --------------------------")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _check_alerts(errors: list[str]) -> None:
    """Сторож на мок-данных: срабатывание при пересечении, отмена, изоляция."""
    print("\n— Проверяю Сторож (мок-данные, временная БД) …")
    tmp_dir = tempfile.mkdtemp(prefix="alerts_selftest_")
    uid, other = 111, 222
    before = len(errors)

    # Подменяем источник метрик — без обращения к сети.
    price_box = {"price": 80000.0, "change24h": 2.0}

    async def fake_fetch(symbol, need_klines):
        return dict(price_box)

    try:
        set_alerts_db(f"{tmp_dir}/alerts.db")

        a = add_alert(uid, "BTCUSDT", "price_above", 81000)
        if len(list_alerts(uid)) != 1:
            errors.append("alerts: подписка не создалась")

        # Чистая проверка условия: порог не пересечён.
        fired, _ = evaluate_condition("price_above", 81000, {"price": 80000})
        if fired:
            errors.append("alerts: сработало без пересечения порога")
        fired, actual = evaluate_condition("price_above", 81000, {"price": 81042})
        if not fired or actual != 81042:
            errors.append("alerts: не сработало при пересечении порога")

        # run_check без пересечения — событий нет, подписка активна.
        events = await run_check(fetcher=fake_fetch)
        if events:
            errors.append("alerts: run_check сработал без пересечения")
        if len(list_alerts(uid)) != 1:
            errors.append("alerts: подписка пропала раньше времени")

        # Цена пересекла порог — ровно одно событие, подписка гасится.
        price_box["price"] = 81042.0
        events = await run_check(fetcher=fake_fetch)
        if len(events) != 1 or events[0]["symbol"] != "BTCUSDT":
            errors.append(f"alerts: ожидалось 1 событие, получено {events}")
        if list_alerts(uid):
            errors.append("alerts: подписка не погашена после срабатывания")

        # Изоляция: чужую подписку нельзя отменить.
        b = add_alert(uid, "ETHUSDT", "rsi_below", 30)
        try:
            remove_alert(other, b["id"])
            errors.append("alerts: чужую подписку удалось отменить")
        except Exception:
            pass
        if len(list_alerts(other)) != 0:
            errors.append("alerts: нарушена изоляция пользователей")
        remove_alert(uid, b["id"])
        if list_alerts(uid):
            errors.append("alerts: /unwatch не удалил подписку")

        if events:
            _check_forbidden(format_alert_fired(events[0]), "alerts", errors)

        if len(errors) == before:
            print(f"  {OK} подписка/срабатывание/отмена/изоляция — ок")
            if events:
                print("  ----- превью уведомления -----")
                print("  " + format_alert_fired(events[0]))
                print("  -------------------------------")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _check_paper(errors: list[str]) -> None:
    """Виртуальный портфель на мок-ценах: PnL, шорт, reset, изоляция, автозакрытие."""
    print("\n— Проверяю виртуальный портфель (мок-цены, временная БД) …")
    tmp_dir = tempfile.mkdtemp(prefix="paper_selftest_")
    uid, other = 111, 222
    before = len(errors)
    try:
        set_paper_db(f"{tmp_dir}/paper.db")

        if abs(get_balance(uid) - 10000.0) > 1e-9:
            errors.append("paper: стартовый баланс не 10 000")

        # Лонг: вход 80000, qty 0.01 -> номинал 800, баланс 9200.
        p = open_position(uid, "BTCUSDT", "long", 0.01, 80000)
        if abs(get_balance(uid) - 9200.0) > 1e-9:
            errors.append(f"paper: покупка не уменьшила баланс ({get_balance(uid)})")
        # Выход 81000 -> PnL +10. Номинал 800 возвращается + PnL: 9200+810=10010
        # (чистый эффект круга = PnL, т.е. 10000 + 10).
        r = close_position(uid, p["id"], 81000)
        if abs(r["pnl_usdt"] - 10.0) > 1e-9:
            errors.append(f"paper: PnL лонга != +10 ({r['pnl_usdt']})")
        if abs(get_balance(uid) - 10010.0) > 1e-9:
            errors.append(f"paper: баланс после лонга != 10010 ({get_balance(uid)})")

        # Шорт зеркально: вход 80000, выход 79000, qty 0.01 -> PnL +10.
        ps = open_position(uid, "BTCUSDT", "short", 0.01, 80000)
        rs = close_position(uid, ps["id"], 79000)
        if abs(rs["pnl_usdt"] - 10.0) > 1e-9:
            errors.append(f"paper: PnL шорта != +10 ({rs['pnl_usdt']})")

        # Превышение баланса -> ошибка (плечо 1x).
        try:
            open_position(uid, "BTCUSDT", "long", 1, 80000)  # номинал 80000 > баланса
            errors.append("paper: позволил позицию больше баланса")
        except Exception:
            pass

        # Автозакрытие по стопу через run_paper_check (мок-цена).
        pa = open_position(uid, "ETHUSDT", "long", 0.01, 2000, stop=1900, take=2200)

        async def fake_price(symbol):
            return 1850.0  # ниже стопа 1900

        events = await run_paper_check(fetcher=fake_price)
        if not any(e["id"] == pa["id"] and e["reason"] == "stop" for e in events):
            errors.append("paper: позиция не закрылась по стопу в run_paper_check")

        # reset -> 10 000, без позиций.
        bal = paper_reset(uid)
        if abs(bal - 10000.0) > 1e-9 or portfolio(uid)["positions"]:
            errors.append("paper: reset не вернул портфель к 10 000 без позиций")

        # Изоляция: у другого пользователя свой старт, чужую позицию не закрыть.
        if abs(get_balance(other) - 10000.0) > 1e-9:
            errors.append("paper: изоляция баланса нарушена")
        p2 = open_position(other, "BTCUSDT", "long", 0.01, 80000)
        try:
            close_position(uid, p2["id"], 81000)
            errors.append("paper: удалось закрыть чужую позицию")
        except Exception:
            pass

        # Нет запрещённых слов в текстах.
        _check_forbidden(format_paper_portfolio(portfolio(other)), "paper", errors)
        _check_forbidden(format_paper_opened(p2), "paper", errors)
        _check_forbidden(format_paper_closed(r), "paper", errors)

        if len(errors) == before:
            print(f"  {OK} лонг +10, шорт +10, лимит 1x, автостоп, reset, изоляция — ок")
            print("  ----- превью ответа -----")
            for line in format_paper_closed(r).splitlines():
                print(f"  {line}")
            print("  --------------------------")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _check_whales(errors: list[str]) -> None:
    """Киты на мок-aggTrades: порог, флаг m, топ-3, давление; честная заглушка."""
    print("\n— Проверяю Whale Tracker (мок-aggTrades) …")
    now = 1_000_000_000_000
    trades = [
        {"p": "80000", "q": "0.1", "T": now, "m": False},          # мелкая (8k) — отсечь порогом
        {"p": "80000", "q": "5", "T": now, "m": False},            # кит-покупка 400k
        {"p": "80000", "q": "4", "T": now, "m": True},             # кит-продажа 320k
        {"p": "80000", "q": "10", "T": now, "m": False},           # кит-покупка 800k (крупнейшая)
        {"p": "80000", "q": "5", "T": now - 20 * 60_000, "m": False},  # 400k, но вне окна 15м
    ]
    result = analyze_whales(trades, "BTCUSDT", now_ms=now)

    before = len(errors)
    if result["whale_count"] != 3:
        errors.append(f"whales: whale_count != 3 ({result['whale_count']})")
    if abs(result["buy_notional"] - 1_200_000) > 1e-6:
        errors.append(f"whales: buy_notional != 1.2M ({result['buy_notional']})")
    if abs(result["sell_notional"] - 320_000) > 1e-6:
        errors.append(f"whales: sell_notional != 320k ({result['sell_notional']})")
    if result["pressure"] != "покупки":
        errors.append(f"whales: давление != покупки ({result['pressure']})")
    top = result["top"]
    if len(top) != 3 or abs(top[0]["notional"] - 800_000) > 1e-6:
        errors.append(f"whales: топ-3 неверно отсортирован ({[t['notional'] for t in top]})")
    if not any(t["side"] == "sell" for t in top):
        errors.append("whales: флаг m не разделил buy/sell")

    # Честная заглушка ликвидаций.
    if "451" not in LIQUIDATIONS_NOTE or "недоступны" not in LIQUIDATIONS_NOTE.lower():
        errors.append("whales: заглушка /liquidations не честная")

    _check_forbidden(format_whales(result), "whales", errors)
    _check_forbidden(LIQUIDATIONS_NOTE, "liquidations", errors)

    if len(errors) == before:
        print(f"  {OK} порог/флаг m/топ-3/давление ок; заглушка ликвидаций честная")
        print("  ----- превью ответа -----")
        for line in format_whales(result).splitlines():
            print(f"  {line}")
        print("  --------------------------")


def _check_skeptic(errors: list[str]) -> None:
    """Скептик на мок-данных: идеальная 5/5, перекупленность и тесный стоп — FAIL."""
    print("\n— Проверяю Скептика (мок-данные) …")
    before = len(errors)

    def _status(result, name):
        for c in result["checks"]:
            if c["name"] == name:
                return c["status"]
        return None

    base = {
        "price": 80000.0, "rsi": 55.0, "atr": 1000.0, "ema200": 75000.0,
        "volume_ratio": 1.2, "support": 78000.0, "resistance": 85000.0,
    }

    # Идеальная идея -> 5/5.
    ideal = analyze_idea("long", 80000, 78500, 83000, dict(base))
    if ideal["passed"] != 5 or ideal["total"] != 5:
        errors.append(f"skeptic: идеальная идея != 5/5 ({ideal['passed']}/{ideal['total']})")
    _check_forbidden(format_skeptic({**ideal, "symbol": "BTCUSDT"}), "skeptic", errors)

    # Лонг в перекупленности -> FAIL по RSI.
    over = dict(base); over["rsi"] = 80.0
    r_over = analyze_idea("long", 80000, 78500, 83000, over)
    if _status(r_over, "RSI против направления") != "FAIL":
        errors.append("skeptic: перекупленность не дала FAIL по RSI")

    # Тесный стоп (0.5×ATR) -> FAIL по волатильности.
    r_tight = analyze_idea("long", 80000, 79500, 83000, dict(base))
    if _status(r_tight, "Стоп vs волатильность") != "FAIL":
        errors.append("skeptic: тесный стоп не дал FAIL по ATR")

    if len(errors) == before:
        print(f"  {OK} идеальная 5/5, перекупленность и тесный стоп — FAIL")
        print("  ----- превью ответа -----")
        for line in format_skeptic({**ideal, "symbol": "BTCUSDT"}).splitlines():
            print(f"  {line}")
        print("  --------------------------")


async def _check_coordinator(errors: list[str]) -> None:
    """Координатор end-to-end на мок-функциях: FAIL-идеи отсеиваются, dict валиден."""
    print("\n— Проверяю Координатор (мок-цепочка) …")
    before = len(errors)

    async def fake_scan(tf, n):
        return [
            {"symbol": "AAAUSDT", "patterns": ["x"], "change_24h": 9.0},
            {"symbol": "BBBUSDT", "patterns": ["y"], "change_24h": 6.0},
        ]

    async def fake_analyze(symbol, tf):
        return {
            "price": 80000.0,
            "ema": {"ema200": 75000.0},
            "change_24h": 5.0,
            "trend": "восходящий",
            "rsi": 80.0 if symbol == "AAAUSDT" else 55.0,
        }

    async def fake_score(symbol, tf, side):
        return {"score": 40 if symbol == "AAAUSDT" else 60, "factors": [], "summary": ""}

    async def fake_exit(symbol, tf, side, price):
        return {"stop": 78500.0, "take1": 83000.0, "take2": 85000.0, "rr1": 2.0, "rr2": 3.0}

    async def fake_skeptic(symbol, side, entry, stop, take):
        rsi_fail = symbol == "AAAUSDT"
        checks = [
            {"name": "RSI против направления",
             "status": "FAIL" if rsi_fail else "PASS", "detail": "RSI проверка"},
            {"name": "Стоп vs волатильность", "status": "PASS", "detail": "стоп ок"},
            {"name": "Уровни против", "status": "PASS", "detail": "уровни ок"},
            {"name": "Участие объёма", "status": "PASS", "detail": "объём ок"},
            {"name": "Тренд против", "status": "PASS", "detail": "тренд ок"},
        ]
        passed = sum(1 for c in checks if c["status"] == "PASS")
        weak = [c["detail"] for c in checks if c["status"] == "FAIL"]
        return {"checks": checks, "passed": passed, "total": len(checks), "weak_points": weak}

    async def fake_news(n):
        return [{"title": "Заголовок", "source": "CoinDesk", "time": None, "url": "u"}]

    saved = {
        name: getattr(coord_pipeline, name)
        for name in ("scan", "analyze", "score_entry", "plan_exit", "skeptic", "get_crypto_news")
    }
    coord_pipeline.scan = fake_scan
    coord_pipeline.analyze = fake_analyze
    coord_pipeline.score_entry = fake_score
    coord_pipeline.plan_exit = fake_exit
    coord_pipeline.skeptic = fake_skeptic
    coord_pipeline.get_crypto_news = fake_news
    try:
        brief = await build_brief(top=5)
    finally:
        for name, fn in saved.items():
            setattr(coord_pipeline, name, fn)

    if not isinstance(brief, dict):
        errors.append("coordinator: build_brief вернул не dict")
    else:
        for key in ("generated_at", "market_summary", "news", "ideas", "filtered_out"):
            if key not in brief:
                errors.append(f"coordinator: нет ключа '{key}'")
        idea_syms = [i["symbol"] for i in brief.get("ideas", [])]
        filt_syms = [f["symbol"] for f in brief.get("filtered_out", [])]
        if "BBBUSDT" not in idea_syms:
            errors.append("coordinator: годная идея не попала в ideas")
        if "AAAUSDT" not in filt_syms or "AAAUSDT" in idea_syms:
            errors.append("coordinator: FAIL-идея не отсеяна риск-гейтом")
        for i in brief.get("ideas", []):
            for f in ("symbol", "side", "score", "entry", "stop", "take1", "take2"):
                if f not in i:
                    errors.append(f"coordinator: в идее нет поля '{f}'")

        text = format_brief(brief, auto=True)
        if "Автодайджест" not in text:
            errors.append("coordinator: в автодайджесте нет честной шапки")
        _check_forbidden(text, "coordinator", errors)

    # Подписки на автодайджест (временная БД).
    tmp_dir = tempfile.mkdtemp(prefix="brief_selftest_")
    try:
        set_brief_db(f"{tmp_dir}/brief.db")
        uid = 111
        set_autobrief(uid, True, 4)
        sub = get_autobrief(uid)
        if not sub or not sub["active"] or abs(sub["interval_h"] - 4) > 1e-9:
            errors.append("coordinator: подписка не создалась")
        if uid not in due_subscriptions():
            errors.append("coordinator: новая подписка не считается due")
        set_autobrief(uid, False)
        if uid in due_subscriptions():
            errors.append("coordinator: выключенная подписка осталась due")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if len(errors) == before:
        print(f"  {OK} цепочка end-to-end, FAIL отсеян, dict валиден, подписка ок")
        print("  ----- превью ответа -----")
        for line in format_brief(brief, auto=True).splitlines():
            print(f"  {line}")
        print("  --------------------------")


def _check_charts(errors: list[str]) -> None:
    """Графики на мок-свечах: PNG создаётся, размер > 0, подписи нейтральны."""
    print("\n— Проверяю графики (мок-свечи) …")
    before = len(errors)

    # Синтетические свечи в формате Binance klines.
    rows = []
    base = 80000.0
    for i in range(120):
        o = base + i * 10
        c = o + (50 if i % 2 == 0 else -40)
        h = max(o, c) + 30
        low = min(o, c) - 30
        vol = 100 + (i % 10) * 5
        t = 1_000_000_000_000 + i * 3_600_000
        rows.append([t, o, h, low, c, vol, t + 3_600_000, 0, 0, 0, 0, 0])

    # Нет запрещённых слов в статических подписях.
    for label in CHART_LABELS:
        for word in FORBIDDEN_WORDS:
            if word in label.lower():
                errors.append(f"charts: запрещённое слово «{word}» в подписи «{label}»")

    paths = []
    try:
        p1 = render_analysis("BTCUSDT", "1h", rows)
        paths.append(p1)
        if not (os.path.isfile(p1) and os.path.getsize(p1) > 0):
            errors.append("charts: plot_analysis не создал непустой PNG")

        p2 = render_backtest("BTCUSDT", "1h", rows, 80500, 79800, [81500, 82200])
        paths.append(p2)
        if not (os.path.isfile(p2) and os.path.getsize(p2) > 0):
            errors.append("charts: plot_backtest не создал непустой PNG")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"charts: рендер упал — {exc!r}")
    finally:
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass

    if len(errors) == before:
        print(f"  {OK} оба PNG созданы (размер > 0), подписи нейтральны")


async def _check_miniapp(errors: list[str]) -> None:
    """Mini App: сервер стартует, все /api/* отвечают 200 с валидным JSON (мок-данные)."""
    print("\n— Проверяю Mini App (сервер + /api/*) …")
    from aiohttp.test_utils import TestClient, TestServer

    before = len(errors)

    async def fake_ticker(symbol):
        return {"lastPrice": "80000", "priceChangePercent": "3.5"}

    async def fake_klines(symbol, tf, limit=30):
        return [[i, 80000 + i, 80100 + i, 79900 + i, 80000 + i, 100, i, 0, 0, 0, 0, 0]
                for i in range(limit)]

    async def fake_brief(top=3):
        return {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "timeframe": "1h", "market_summary": "наблюдается 1 ситуаций",
            "news": [], "ideas": [{
                "symbol": "BTCUSDT", "side": "long", "score": 60,
                "entry": 80000, "stop": 78500, "take1": 83000, "take2": 85000,
                "skeptic_passed": 5, "skeptic_total": 5, "weak_points": [],
            }], "filtered_out": [],
        }

    async def fake_whales(symbol):
        return {"symbol": symbol, "pressure": "смешанное", "whale_count": 0, "top": []}

    def fake_stats(user_id):
        return {"total": 1, "wins": 1, "win_rate": 1.0, "avg_r": 1.0,
                "expectancy": 1.0, "total_pnl_usdt": 10.0, "best_r": 1.0, "worst_r": 1.0}

    def fake_portfolio(user_id):
        return {"balance": 10000.0, "positions": []}

    saved = {
        "get_ticker_24h": miniapp_server.get_ticker_24h,
        "get_klines": miniapp_server.get_klines,
        "build_brief": miniapp_server.build_brief,
        "get_whales": miniapp_server.get_whales,
        "journal_stats": miniapp_server.journal_stats,
        "paper_portfolio": miniapp_server.paper_portfolio,
    }
    miniapp_server.get_ticker_24h = fake_ticker
    miniapp_server.get_klines = fake_klines
    miniapp_server.build_brief = fake_brief
    miniapp_server.get_whales = fake_whales
    miniapp_server.journal_stats = fake_stats
    miniapp_server.paper_portfolio = fake_portfolio

    client = None
    try:
        client = TestClient(TestServer(miniapp_server.create_app()))
        await client.start_server()

        r = await client.get("/")
        if r.status != 200:
            errors.append(f"miniapp: / вернул {r.status}")

        endpoints = ["/api/market", "/api/brief", "/api/whales",
                     "/api/journal/1", "/api/paper/1"]
        for ep in endpoints:
            resp = await client.get(ep)
            if resp.status != 200:
                errors.append(f"miniapp: {ep} вернул {resp.status}")
                continue
            try:
                data = await resp.json()
            except Exception:
                errors.append(f"miniapp: {ep} вернул не JSON")
                continue
            if not isinstance(data, dict) or not data.get("ok"):
                errors.append(f"miniapp: {ep} ok!=true ({data})")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"miniapp: сервер упал — {exc!r}")
    finally:
        if client is not None:
            await client.close()
        for name, fn in saved.items():
            setattr(miniapp_server, name, fn)

    if len(errors) == before:
        print(f"  {OK} сервер стартует, все /api/* отвечают 200 валидным JSON")


async def _check_deploy(errors: list[str]) -> None:
    """Деплой: /healthz отвечает 200 {ok:true}; файлы Docker/Caddy валидны."""
    print("\n— Проверяю деплой (healthz + Docker/Caddy) …")
    from aiohttp.test_utils import TestClient, TestServer

    before = len(errors)
    root = os.path.dirname(os.path.abspath(__file__))

    # 1) /healthz 200 {ok:true}.
    client = None
    try:
        client = TestClient(TestServer(miniapp_server.create_app()))
        await client.start_server()
        resp = await client.get("/healthz")
        if resp.status != 200:
            errors.append(f"deploy: /healthz вернул {resp.status}")
        else:
            data = await resp.json()
            if not (isinstance(data, dict) and data.get("ok") is True):
                errors.append(f"deploy: /healthz ok!=true ({data})")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"deploy: healthz упал — {exc!r}")
    finally:
        if client is not None:
            await client.close()

    # 2) Наличие и базовая валидность деплой-файлов.
    def _read(name):
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            errors.append(f"deploy: нет файла {name}")
            return ""
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    dockerfile = _read("Dockerfile")
    if "python:3.11-slim" not in dockerfile or "bot.main" not in dockerfile:
        errors.append("deploy: Dockerfile без python:3.11-slim или запуска bot.main")

    compose = _read("docker-compose.yml")
    for marker in ("services:", "bot:", "caddy:", "build:", "image: caddy",
                   "./data:/app/data", "env_file"):
        if marker not in compose:
            errors.append(f"deploy: docker-compose.yml без '{marker}'")

    caddy = _read("Caddyfile")
    if "reverse_proxy bot:8080" not in caddy or "{$DOMAIN}" not in caddy:
        errors.append("deploy: Caddyfile без reverse_proxy bot:8080 или {$DOMAIN}")

    env_example = _read(".env.example")
    if "BOT_TOKEN=" not in env_example or "DOMAIN=" not in env_example:
        errors.append("deploy: .env.example без BOT_TOKEN/DOMAIN")

    if not os.path.isfile(os.path.join(root, "DEPLOY.md")):
        errors.append("deploy: нет DEPLOY.md")

    # 3) docker compose config, если docker установлен (иначе структурная проверка выше).
    docker = shutil.which("docker")
    compose_checked = "структурно (docker не найден)"
    if docker:
        import subprocess
        try:
            res = subprocess.run(
                [docker, "compose", "-f", os.path.join(root, "docker-compose.yml"), "config"],
                capture_output=True, text=True, timeout=60,
            )
            if res.returncode != 0:
                errors.append(f"deploy: docker compose config невалиден: {res.stderr.strip()[:200]}")
            else:
                compose_checked = "docker compose config OK"
        except Exception as exc:  # noqa: BLE001
            compose_checked = f"docker недоступен ({exc!r})"

    if len(errors) == before:
        print(f"  {OK} /healthz=200 {{ok:true}}; деплой-файлы валидны; compose — {compose_checked}")


async def _check_broker(errors: list[str]) -> None:
    """Брокер на моках биржи: paper как раньше, подпись, live-гейты, killswitch."""
    print("\n— Проверяю Брокер (моки биржи) …")
    import hashlib
    import hmac as _hmac

    before = len(errors)
    tmp_dir = tempfile.mkdtemp(prefix="broker_selftest_")
    uid = 111

    # Снимем возможные env от прошлых прогонов.
    for k in ("BROKER_MODE", "BINANCE_API_KEY", "BINANCE_API_SECRET",
              "BROKER_MAX_DAILY_LOSS_USDT"):
        os.environ.pop(k, None)

    saved_price = broker.get_ticker_24h

    async def fake_ticker(symbol):
        return {"lastPrice": "80000"}

    try:
        broker.set_db_path(f"{tmp_dir}/broker.db")
        set_paper_db(f"{tmp_dir}/broker.db")
        broker.get_ticker_24h = fake_ticker

        # 1) paper как раньше: ордер открывает виртуальную позицию, баланс падает.
        broker.set_mode("paper")
        res = await broker.place_order(uid, "BTCUSDT", "buy", 0.01)
        if res["status"] != "executed" or res["mode"] != "paper":
            errors.append(f"broker: paper-ордер не исполнился ({res})")
        if abs(get_balance(uid) - 9200.0) > 1e-9:
            errors.append(f"broker: paper-баланс не уменьшился ({get_balance(uid)})")

        # 2) Подпись testnet без сети: HMAC SHA256 совпадает с эталоном.
        sig = broker._sign("symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.01", "secret123")
        expected = _hmac.new(b"secret123",
                             b"symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.01",
                             hashlib.sha256).hexdigest()
        if sig != expected:
            errors.append("broker: подпись HMAC неверна")

        # 3) live недоступен без BROKER_MODE=live.
        if broker.current_mode() == "live":
            errors.append("broker: live активен без BROKER_MODE=live")
        try:
            broker.set_mode("live")
            errors.append("broker: set_mode('live') не отклонён")
        except Exception:
            pass

        # 4) live без тапа подтверждения — ордер НЕ создаётся (нет сети/исполнения).
        os.environ["BROKER_MODE"] = "live"
        os.environ["BINANCE_API_KEY"] = "k"
        os.environ["BINANCE_API_SECRET"] = "s"
        res_live = await broker.place_order(uid, "BTCUSDT", "buy", 0.01, 79000, 82000)
        if res_live["status"] != "needs_confirmation":
            errors.append(f"broker: live-ордер ушёл без подтверждения ({res_live})")

        # риск-гейт: live без стопа → BLOCK.
        res_ns = await broker.place_order(uid, "BTCUSDT", "buy", 0.01)
        if res_ns["status"] != "blocked" or "стоп" not in res_ns.get("reason", ""):
            errors.append(f"broker: live без стопа не заблокирован ({res_ns})")

        # 5) killswitch блокирует.
        broker.engage_killswitch()
        res_ks = await broker.place_order(uid, "BTCUSDT", "buy", 0.01, 79000, 82000)
        if res_ks["status"] != "blocked" or "killswitch" not in res_ks.get("reason", ""):
            errors.append(f"broker: killswitch не заблокировал ({res_ks})")
        # paper при killswitch всё ещё работает.
        broker_mode_env = os.environ.pop("BROKER_MODE", None)
        broker.set_mode("paper")
        res_paper2 = await broker.place_order(uid, "BTCUSDT", "buy", 0.01)
        if res_paper2["status"] != "executed":
            errors.append("broker: paper заблокирован killswitch'ем (не должен)")
    finally:
        broker.get_ticker_24h = saved_price
        for k in ("BROKER_MODE", "BINANCE_API_KEY", "BINANCE_API_SECRET"):
            os.environ.pop(k, None)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if len(errors) == before:
        print(f"  {OK} paper ок, подпись верна, live без тапа/стопа не уходит, killswitch блокирует")


async def _check_limit_orders(errors: list[str]) -> None:
    """Лимитные ордера: paper исполняется при касании, отмена; live-гейты и валидация."""
    print("\n— Проверяю лимитные ордера (моки) …")
    before = len(errors)
    tmp_dir = tempfile.mkdtemp(prefix="limit_selftest_")
    uid = 111

    for k in ("BROKER_MODE", "BINANCE_API_KEY", "BINANCE_API_SECRET"):
        os.environ.pop(k, None)
    saved_price = broker.get_ticker_24h
    saved_validate = broker._validate_lot

    async def fake_ticker(symbol):
        return {"lastPrice": "80000"}

    async def fake_validate(mode, symbol, qty):  # без сети
        return None

    async def price_at(val):
        async def f(symbol):
            return val
        return f

    try:
        broker.set_db_path(f"{tmp_dir}/limit.db")
        set_paper_db(f"{tmp_dir}/limit.db")
        broker.get_ticker_24h = fake_ticker
        broker._validate_lot = fake_validate
        broker.set_mode("paper")

        # Позитив: лимитная покупка ниже рынка.
        res = await broker.place_limit_order(uid, "BTCUSDT", "buy", 0.01, 78000, 76000, 82000)
        if res["status"] != "placed":
            errors.append(f"limit: ордер не выставлен ({res})")
        oid = res.get("orderId")

        # Цена выше лимита — не исполняется.
        ev = await run_limit_check(fetcher=await price_at(79000.0))
        if ev:
            errors.append("limit: ордер исполнился выше лимитной цены")

        # Цена коснулась лимита — исполняется, появляется позиция, баланс падает.
        ev = await run_limit_check(fetcher=await price_at(77000.0))
        if not any(e["orderId"] == oid for e in ev):
            errors.append("limit: ордер не исполнился при касании")
        if not portfolio(uid)["positions"]:
            errors.append("limit: после исполнения нет позиции")
        if get_balance(uid) >= 10000.0:
            errors.append("limit: баланс не изменился после исполнения")

        # Отмена до исполнения.
        res2 = await broker.place_limit_order(uid, "BTCUSDT", "buy", 0.01, 70000, 68000)
        await broker.cancel_order(uid, res2["orderId"])
        ev2 = await run_limit_check(fetcher=await price_at(69000.0))
        if any(e["orderId"] == res2["orderId"] for e in ev2):
            errors.append("limit: отменённый ордер всё равно исполнился")

        # Негатив: невалидные цена/кол-во.
        for bad in [(0, 78000), (0.01, 0)]:
            try:
                await broker.place_limit_order(uid, "BTCUSDT", "buy", bad[0], bad[1], 1)
                errors.append(f"limit: принят невалидный ордер {bad}")
            except Exception:
                pass

        # Негатив live: без стопа → BLOCK; без тапа → needs_confirmation (не выставлен).
        os.environ["BROKER_MODE"] = "live"
        os.environ["BINANCE_API_KEY"] = "k"
        os.environ["BINANCE_API_SECRET"] = "s"
        res_ns = await broker.place_limit_order(uid, "BTCUSDT", "buy", 0.01, 78000)
        if res_ns["status"] != "blocked" or "стоп" not in res_ns.get("reason", ""):
            errors.append(f"limit live: без стопа не заблокирован ({res_ns})")
        res_live = await broker.place_limit_order(uid, "BTCUSDT", "buy", 0.01, 78000, 76000, 82000)
        if res_live["status"] != "needs_confirmation":
            errors.append(f"limit live: ордер выставлен без подтверждения ({res_live})")
    finally:
        broker.get_ticker_24h = saved_price
        broker._validate_lot = saved_validate
        for k in ("BROKER_MODE", "BINANCE_API_KEY", "BINANCE_API_SECRET"):
            os.environ.pop(k, None)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if len(errors) == before:
        print(f"  {OK} paper: касание исполняет, отмена работает; live без стопа/тапа не уходит")


def _check_market_index_math(errors: list[str]) -> None:
    """Индекс рынка: compute даёт value 0–100 и три компонента."""
    print("\n— Проверяю индекс рынка (формула) …")
    before = len(errors)
    metrics = [
        {"price": 110, "ema50": 100, "rsi": 60, "vol_24h": 150, "avg_vol_20": 100},
        {"price": 90, "ema50": 100, "rsi": 45, "vol_24h": 80, "avg_vol_20": 100},
        {"price": 120, "ema50": 100, "rsi": 70, "vol_24h": 200, "avg_vol_20": 100},
    ]
    res = mi_compute(metrics)
    if not (isinstance(res["value"], int) and 0 <= res["value"] <= 100):
        errors.append(f"market_index: value вне [0,100] ({res['value']})")
    comp = res.get("components", {})
    for key in ("breadth", "momentum", "volume"):
        if key not in comp:
            errors.append(f"market_index: нет компонента '{key}'")
    if not str(res.get("label", "")).strip():
        errors.append("market_index: нет метки режима")
    if len(errors) == before:
        print(f"  {OK} value={res['value']} ({res['label']}); компоненты {comp}")


def _check_scan_scoring(errors: list[str]) -> None:
    """Скоринг сетапа: score находки равен сумме factors."""
    print("\n— Проверяю скоринг сканера …")
    before = len(errors)
    m = {"side": "long", "price": 100, "ema20": 99, "ema50": 97, "ema200": 90,
         "vol_ratio": 1.6, "rsi": 55, "atr": 2.0, "support": 96,
         "resistance": 108, "structure": "bull flag"}
    score, factors = score_setup(m)
    if score != sum(f["points"] for f in factors):
        errors.append(f"scan: score ({score}) != сумме factors")
    if not (0 <= score <= 100):
        errors.append(f"scan: score вне [0,100] ({score})")
    for f in factors:
        if f["points"] > f["max"]:
            errors.append(f"scan: фактор {f['name']} превысил максимум")
    if len(errors) == before:
        print(f"  {OK} score={score} = сумме factors ({[f['points'] for f in factors]})")


async def _check_new_apis(errors: list[str]) -> None:
    """Эндпоинты /api/market_index и /api/scan отвечают 200 с валидным JSON (моки)."""
    print("\n— Проверяю /api/market_index и /api/scan …")
    from aiohttp.test_utils import TestClient, TestServer
    before = len(errors)

    async def fake_index(top_n=30):
        return {"value": 60, "label": "Уверенность",
                "components": {"breadth": 24.0, "momentum": 18.0, "volume": 18.0},
                "updated_at": "2026-01-01T00:00:00+00:00", "pairs": 30}

    async def fake_scan(timeframe="1h", top_n=30):
        return [{"symbol": "BTCUSDT", "side": "long", "score": 70,
                 "factors": [{"name": "Тренд", "points": 30, "max": 30}],
                 "patterns": ["x"], "structure": "bull flag", "change_24h": 3.0,
                 "price": 80000, "rsi": 55}]

    saved = (miniapp_server.market_index, miniapp_server.scan)
    miniapp_server.market_index = fake_index
    miniapp_server.scan = fake_scan
    client = None
    try:
        client = TestClient(TestServer(miniapp_server.create_app()))
        await client.start_server()
        r = await client.get("/api/market_index")
        d = await r.json()
        if r.status != 200 or not d.get("ok"):
            errors.append(f"api: /api/market_index не 200/ok ({r.status})")
        else:
            idx = d["index"]
            if not (0 <= idx["value"] <= 100):
                errors.append("api: market_index value вне [0,100]")
            if any(k not in idx["components"] for k in ("breadth", "momentum", "volume")):
                errors.append("api: market_index без трёх компонентов")
        r2 = await client.get("/api/scan")
        d2 = await r2.json()
        if r2.status != 200 or not d2.get("ok"):
            errors.append(f"api: /api/scan не 200/ok ({r2.status})")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"api: сервер упал — {exc!r}")
    finally:
        if client is not None:
            await client.close()
        miniapp_server.market_index, miniapp_server.scan = saved
    if len(errors) == before:
        print(f"  {OK} оба эндпоинта 200, value в [0,100], три компонента")


def _check_index_html(errors: list[str]) -> None:
    """index.html: 15 экранов и нет захардкоженных цен из мокапов."""
    print("\n— Проверяю index.html (15 экранов, без хардкода цен) …")
    before = len(errors)
    root = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(root, "miniapp", "index.html"), encoding="utf-8") as fh:
        html = fh.read()

    screens = html.count('class="screen')
    if screens != 15:
        errors.append(f"index.html: экранов {screens}, ожидалось 15")

    forbidden_nums = ["67,428", "67428", "2,654", "164.82", "164,82",
                      "67,000", "67000", "66,912", "65,873", "62,418"]
    for num in forbidden_nums:
        if num in html:
            errors.append(f"index.html: захардкоженное число из мокапа «{num}»")

    if "Не финансовый совет. Наблюдения, не рекомендации" not in html:
        errors.append("index.html: нет футер-дисклеймера")
    if len(errors) == before:
        print(f"  {OK} экранов 15, захардкоженных цен нет, дисклеймер на месте")


async def _check_paper_limit_no_network(errors: list[str]) -> None:
    """Регресс: paper-лимитки выставляются/исполняются БЕЗ обращения к exchangeInfo."""
    print("\n— Проверяю paper-лимитки без сети (регресс LOT_SIZE) …")
    before = len(errors)
    tmp_dir = tempfile.mkdtemp(prefix="papernet_selftest_")
    uid = 111
    for k in ("BROKER_MODE", "BINANCE_API_KEY", "BINANCE_API_SECRET"):
        os.environ.pop(k, None)

    saved_ticker = broker.get_ticker_24h
    saved_lot = broker._get_lot_size
    saved_validate = broker._validate_lot

    async def boom(*a, **k):
        raise AssertionError("СЕТЕВОЙ ВЫЗОВ exchangeInfo В PAPER!")

    async def fake_ticker(symbol):
        return {"lastPrice": "80000"}

    async def price_at(val):
        async def f(symbol):
            return val
        return f

    try:
        broker.set_db_path(f"{tmp_dir}/pn.db")
        set_paper_db(f"{tmp_dir}/pn.db")
        broker.get_ticker_24h = fake_ticker
        # Любой заход в exchangeInfo/LOT_SIZE в paper → провал теста.
        broker._get_lot_size = boom
        broker._validate_lot = boom
        broker.set_mode("paper")

        # buylimit paper — должен выставиться без сети.
        rb = await broker.place_limit_order(uid, "BTCUSDT", "buy", 0.01, 78000, 76000, 82000)
        if rb["status"] != "placed":
            errors.append(f"paper buylimit не выставлен ({rb})")

        # открыть лонг (маркет), чтобы было что продать selllimit'ом.
        await broker.place_order(uid, "BTCUSDT", "buy", 0.02)
        # selllimit paper — тоже без сети.
        rs = await broker.place_limit_order(uid, "BTCUSDT", "sell", 0.01, 85000, 86000, 84000)
        if rs["status"] != "placed":
            errors.append(f"paper selllimit не выставлен ({rs})")

        # исполнение по касанию (цена ≤ лимита buy 78000).
        ev = await run_limit_check(fetcher=await price_at(77000.0))
        if not any(e["orderId"] == rb["orderId"] for e in ev):
            errors.append("paper buylimit не исполнился по касанию")
        if not portfolio(uid)["positions"]:
            errors.append("paper: после исполнения нет позиции")
    except AssertionError as exc:
        errors.append(f"paper: {exc}")
    finally:
        broker.get_ticker_24h = saved_ticker
        broker._get_lot_size = saved_lot
        broker._validate_lot = saved_validate
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if len(errors) == before:
        print(f"  {OK} buylimit/selllimit в paper без сети; исполнение по касанию работает")


def _check_autopush(errors: list[str]) -> None:
    """Автопуш: сильная идея пушится; score<75/риск-гейт FAIL/дубль/тихий час — нет."""
    print("\n— Проверяю автопуш наблюдений …")
    before = len(errors)
    tmp_dir = tempfile.mkdtemp(prefix="push_selftest_")
    uid = 111
    # 15:00 UTC = 10:00 NY (не тихий час); 08:00 UTC = 03:00 NY (тихий час).
    day = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
    night = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)

    brief = {
        "ideas": [
            {"symbol": "BTCUSDT", "side": "long", "setup_score": 82,
             "setup_factors": [{"name": "Тренд", "points": 30},
                               {"name": "Объём", "points": 25},
                               {"name": "RSI-зона", "points": 20}],
             "entry": 67400, "stop": 65258, "take1": 70618, "take2": 72264,
             "skeptic_passed": 5, "skeptic_total": 5, "weak_points": []},
            {"symbol": "ETHUSDT", "side": "long", "setup_score": 50,  # score < 75
             "setup_factors": [], "entry": 2600, "stop": 2500, "take1": 2800,
             "take2": 2900, "skeptic_passed": 5, "skeptic_total": 5, "weak_points": []},
            {"symbol": "SOLUSDT", "side": "long", "setup_score": 90,  # скептик FAIL
             "setup_factors": [], "entry": 160, "stop": 150, "take1": 175,
             "take2": 180, "skeptic_passed": 3, "skeptic_total": 5, "weak_points": ["a", "b"]},
        ],
        "filtered_out": [{"symbol": "XRPUSDT", "reason": "риск-гейт: стоп/RSI"}],
    }
    try:
        autopush.set_db_path(f"{tmp_dir}/push.db")

        # Позитив + негативы (score<75, скептик FAIL, риск-гейт FAIL) днём.
        pushes = evaluate_push(brief, uid, day)
        syms = [p["symbol"] for p in pushes]
        if syms != ["BTCUSDT"]:
            errors.append(f"autopush: ожидался пуш только BTCUSDT, получено {syms}")
        if "ETHUSDT" in syms:
            errors.append("autopush: пуш при score<75")
        if "SOLUSDT" in syms:
            errors.append("autopush: пуш при скептик FAIL")
        if "XRPUSDT" in syms:
            errors.append("autopush: пуш при риск-гейт FAIL (из filtered_out)")

        # Негатив: дубль пара+сторона в окне 12ч — второго пуша нет.
        again = evaluate_push(brief, uid, day)
        if any(p["symbol"] == "BTCUSDT" for p in again):
            errors.append("autopush: дубль пары в окне 12ч не отфильтрован")

        # Негатив: тихий час (03:00 NY) — пушей нет (другой пользователь, чтобы не мешал дубль).
        if evaluate_push(brief, 222, night):
            errors.append("autopush: пуш в тихий час 00:00–07:00 NY")

        # Текст пуша нейтральный, без запрещённых слов.
        msg = format_push(brief["ideas"][0])
        _check_forbidden(msg, "autopush", errors)
        if "Наблюдение, не рекомендация" not in msg or "Сила сетапа 82/100" not in msg:
            errors.append("autopush: формат пуша неполный")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if len(errors) == before:
        print(f"  {OK} сильная идея пушится; score<75/скептик FAIL/риск-гейт/дубль/тихий час — нет")
        print("  ----- превью пуша -----")
        for line in format_push(brief["ideas"][0]).splitlines():
            print(f"  {line}")
        print("  -----------------------")


async def _run() -> int:
    pairs = [("BTCUSDT", "1h"), ("ETHUSDT", "1h")]
    errors: list[str] = []

    for symbol, timeframe in pairs:
        print(f"\n— Проверяю {symbol} ({timeframe}) …")
        try:
            result = await analyze(symbol, timeframe)
        except DataAgentError as exc:
            errors.append(f"{symbol}/{timeframe}: данные недоступны — {exc}")
            print(f"  {FAIL} не удалось получить данные: {exc}")
            continue

        before = len(errors)
        _check_result(result, errors)
        message = _check_message(result, errors)

        if len(errors) == before:
            print(f"  {OK} индикаторы посчитаны, сообщение сформировано")
            print("  ----- превью ответа -----")
            for line in message.splitlines():
                print(f"  {line}")
            print("  --------------------------")

    await _check_backtest(errors)
    await _check_scan(errors)
    await _check_news(errors)
    await _check_entry(errors)
    await _check_exit(errors)
    _check_size(errors)
    _check_risk(errors)
    _check_journal(errors)
    await _check_alerts(errors)
    await _check_paper(errors)
    _check_whales(errors)
    _check_skeptic(errors)
    await _check_coordinator(errors)
    _check_charts(errors)
    await _check_miniapp(errors)
    await _check_deploy(errors)
    await _check_broker(errors)
    await _check_limit_orders(errors)
    await _check_paper_limit_no_network(errors)
    _check_market_index_math(errors)
    _check_scan_scoring(errors)
    await _check_new_apis(errors)
    _check_index_html(errors)
    _check_autopush(errors)

    print()
    if errors:
        print(f"{FAIL} Проверки НЕ пройдены ({len(errors)}):")
        for err in errors:
            print(f"   - {err}")
        return 1

    print(f"{OK} Все проверки зелёные.")
    return 0


def main() -> None:
    try:
        code = asyncio.run(_run())
    except KeyboardInterrupt:
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
