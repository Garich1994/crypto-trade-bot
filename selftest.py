"""Самопроверка: прогон analyze на BTCUSDT и ETHUSDT (таймфрейм 1h).

Проверяет, что все индикаторы посчитались и сообщение сформировалось.
Зелёные галочки = всё ок. Запуск:  python selftest.py
"""
from __future__ import annotations

import asyncio
import math
import shutil
import sys
import tempfile

# На Windows консоль по умолчанию cp1252 и не печатает кириллицу — переключаем в UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from agents.backtest_agent import BacktestError, backtest
from agents.budget_agent import calc_position
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
from agents.news_agent import get_crypto_news
from agents.risk_agent import check_risk
from agents.scanner_agent import scan
from agents.ta_agent import analyze
from bot.formatting import (
    DISCLAIMER,
    DISCLAIMER_BACKTEST,
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
