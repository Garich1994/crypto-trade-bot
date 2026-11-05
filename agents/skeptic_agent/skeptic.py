"""Skeptic Agent — проверка идеи сделки на прочность (rule-based, без LLM).

Показывает слабые места фактами PASS/FAIL. Не выносит вердикт «входить или нет».
"""
from __future__ import annotations

import pandas as pd

from agents.data_agent import DataAgentError, get_klines
from agents.ta_agent import indicators as ind

DEFAULT_TIMEFRAME = "1h"
_CANDLES = 300
_SIDES = {"long", "short"}
_SIDE_ALIASES = {"лонг": "long", "шорт": "short"}


class SkepticError(Exception):
    """Понятная наружу ошибка (валидация входных данных)."""


def _normalize_side(side: str) -> str:
    s = str(side).strip().lower()
    s = _SIDE_ALIASES.get(s, s)
    if s not in _SIDES:
        raise SkepticError("Сторона должна быть long или short.")
    return s


def _chk_rsi(side: str, rsi: float) -> tuple[bool, str]:
    if side == "long" and rsi > 70:
        return False, f"RSI {rsi:.0f} — перекупленность против лонга"
    if side == "short" and rsi < 30:
        return False, f"RSI {rsi:.0f} — перепроданность против шорта"
    return True, f"RSI {rsi:.0f} — не против направления"


def _chk_stop_atr(entry: float, stop: float, atr: float) -> tuple[bool, str]:
    dist = abs(entry - stop)
    mult = dist / atr if atr > 0 else 0.0
    if mult < 1.0:
        return False, f"стоп {dist:.0f} = {mult:.2f}×ATR — уже ATR, выбьет шумом"
    return True, f"стоп {dist:.0f} = {mult:.2f}×ATR"


def _chk_levels(
    side: str, take: float, support: float | None, resistance: float | None
) -> tuple[bool, str]:
    if side == "long":
        if resistance is not None and resistance < take:
            return False, f"сопротивление {resistance:.0f} ближе тейка {take:.0f}"
        return True, "сопротивление не ближе тейка"
    # short
    if support is not None and support > take:
        return False, f"поддержка {support:.0f} ближе тейка {take:.0f}"
    return True, "поддержка не ближе тейка"


def _chk_volume(volume_ratio: float) -> tuple[bool, str]:
    if volume_ratio < 0.7:
        return False, f"объём x{volume_ratio:.2f} — движение на слабом участии"
    return True, f"объём x{volume_ratio:.2f} — участие достаточное"


def _chk_trend(side: str, price: float, ema200: float) -> tuple[bool, str]:
    if side == "long" and price < ema200:
        return False, f"цена {price:.0f} ниже EMA200 {ema200:.0f} — тренд против лонга"
    if side == "short" and price > ema200:
        return False, f"цена {price:.0f} выше EMA200 {ema200:.0f} — тренд против шорта"
    return True, f"цена со стороны EMA200 {ema200:.0f} в пользу направления"


def analyze_idea(
    side: str, entry: float, stop: float, take: float, metrics: dict
) -> dict:
    """Чистая проверка идеи по метрикам (тестируется на мок-данных)."""
    side = _normalize_side(side)
    names_checks = [
        ("RSI против направления", _chk_rsi(side, metrics["rsi"])),
        ("Стоп vs волатильность", _chk_stop_atr(entry, stop, metrics["atr"])),
        (
            "Уровни против",
            _chk_levels(side, take, metrics.get("support"), metrics.get("resistance")),
        ),
        ("Участие объёма", _chk_volume(metrics["volume_ratio"])),
        ("Тренд против", _chk_trend(side, metrics["price"], metrics["ema200"])),
    ]

    checks = []
    weak_points = []
    passed = 0
    for name, (ok, detail) in names_checks:
        checks.append({"name": name, "status": "PASS" if ok else "FAIL", "detail": detail})
        if ok:
            passed += 1
        else:
            weak_points.append(detail)

    total = len(checks)
    if passed == total:
        summary = f"Слабых мест не найдено ({passed}/{total})."
    else:
        summary = f"Прошла {passed}/{total}. Слабые места: " + "; ".join(weak_points)

    return {
        "side": side,
        "entry": entry,
        "stop": stop,
        "take": take,
        "checks": checks,
        "passed": passed,
        "total": total,
        "weak_points": weak_points,
        "summary": summary,
    }


async def skeptic(
    symbol: str,
    side: str,
    entry: float,
    stop: float,
    take: float,
    timeframe: str = DEFAULT_TIMEFRAME,
) -> dict:
    """Проверить идею сделки на прочность. Бросает SkepticError/DataAgentError."""
    symbol = symbol.upper().strip()
    side = _normalize_side(side)
    try:
        entry = float(entry)
        stop = float(stop)
        take = float(take)
    except (TypeError, ValueError):
        raise SkepticError("Вход, стоп и тейк должны быть числами.")
    if entry <= 0 or stop <= 0 or take <= 0:
        raise SkepticError("Вход, стоп и тейк должны быть больше нуля.")

    raw = await get_klines(symbol, timeframe, limit=_CANDLES)
    close = pd.to_numeric(pd.Series([c[4] for c in raw]), errors="coerce")
    high = pd.to_numeric(pd.Series([c[2] for c in raw]), errors="coerce")
    low = pd.to_numeric(pd.Series([c[3] for c in raw]), errors="coerce")
    volume = pd.to_numeric(pd.Series([c[5] for c in raw]), errors="coerce")
    if close.dropna().shape[0] < 30:
        raise DataAgentError("Недостаточно свечей для проверки.")

    price = float(close.iloc[-1])
    swing_highs, swing_lows = ind.swing_levels(high, low, window=5)
    # Уровни S/R — от последнего закрытия (как в Аналитике), единый источник на всех
    # экранах: на одном таймфрейме поддержка/сопротивление совпадают.
    support, resistance = ind.nearest_support_resistance(swing_highs, swing_lows, price)
    avg_vol = float(volume.tail(20).mean())

    metrics = {
        "price": price,
        "rsi": float(ind.rsi(close, 14).iloc[-1]),
        "atr": float(ind.atr(high, low, close, 14).iloc[-1]),
        "ema200": float(ind.ema(close, 200).iloc[-1]),
        "volume_ratio": (float(volume.iloc[-1]) / avg_vol) if avg_vol > 0 else 0.0,
        "support": support,
        "resistance": resistance,
    }

    result = analyze_idea(side, entry, stop, take, metrics)
    result["symbol"] = symbol
    result["timeframe"] = timeframe.lower()
    return result
