"""Exit Planner Agent — расчёт стопа и тейков от ATR и уровней S/R.

Это арифметика по данным, а не рекомендация. Комиссии и проскальзывание не
моделируются.
"""
from __future__ import annotations

import pandas as pd

from agents.data_agent import DataAgentError, get_klines
from agents.ta_agent import indicators as ind

DEFAULT_TIMEFRAME = "1h"
_CANDLES = 300
_SIDES = {"long", "short"}
_SIDE_ALIASES = {"лонг": "long", "шорт": "short"}
_SNAP_FACTOR = 0.75  # «рядом» для тейков = в пределах 0.75 * ATR
_SNAP_STOP = 0.5     # для стопа окно уже, чтобы пресеты ×1/×1.5/×2 давали разные стопы
_RR1 = 1.5
_RR2 = 2.0


class ExitError(Exception):
    """Понятная наружу ошибка (валидация стороны/цены)."""


def _normalize_side(side: str) -> str:
    s = side.strip().lower()
    s = _SIDE_ALIASES.get(s, s)
    if s not in _SIDES:
        raise ExitError("Сторона должна быть long или short.")
    return s


def _nearest(target: float, candidates: list[float], max_dist: float) -> float | None:
    """Ближайший уровень к target в пределах max_dist, иначе None."""
    near = [c for c in candidates if abs(c - target) <= max_dist]
    if not near:
        return None
    return min(near, key=lambda c: abs(c - target))


async def plan_exit(
    symbol: str,
    timeframe: str,
    side: str,
    entry_price: float,
    atr_mult: float = 1.5,
) -> dict:
    """Рассчитать уровни выхода. Возвращает stop/take1/take2/rr1/rr2 и основания.

    Бросает ExitError (валидация) или DataAgentError (API недоступен).
    """
    symbol = symbol.upper().strip()
    side = _normalize_side(side)
    entry = float(entry_price)
    if entry <= 0:
        raise ExitError("Цена входа должна быть положительной.")
    if atr_mult <= 0:
        raise ExitError("Множитель ATR должен быть положительным.")

    raw = await get_klines(symbol, timeframe, limit=_CANDLES)
    close = pd.to_numeric(pd.Series([c[4] for c in raw]), errors="coerce")
    high = pd.to_numeric(pd.Series([c[2] for c in raw]), errors="coerce")
    low = pd.to_numeric(pd.Series([c[3] for c in raw]), errors="coerce")
    if close.dropna().shape[0] < 30:
        raise DataAgentError("Недостаточно свечей для расчёта.")

    atr_val = float(ind.atr(high, low, close, 14).iloc[-1])
    if atr_val <= 0:
        raise DataAgentError("Не удалось рассчитать ATR.")

    swing_highs, swing_lows = ind.swing_levels(high, low, window=5)
    supports_below = [s for s in swing_lows if s < entry]
    resistances_above = [r for r in swing_highs if r > entry]
    snap_dist = _SNAP_FACTOR * atr_val

    direction = 1.0 if side == "long" else -1.0
    base_risk = atr_mult * atr_val

    # Начальные уровни.
    stop = entry - direction * base_risk
    take1 = entry + direction * _RR1 * base_risk
    take2 = entry + direction * _RR2 * base_risk

    snapped: list[str] = []

    # Прижатие стопа к уровню — только если уровень ВНУТРИ ATR-диапазона и ГЛУБЖЕ
    # базового стопа (не ближе ко входу): стоп не затягивается выше уровня, а разные
    # множители ATR дают разные стопы. Нет подходящего уровня — стоп строго по ATR.
    stop_snap = _SNAP_STOP * atr_val
    if side == "long":
        far_levels = [s for s in supports_below if s <= stop]   # глубже базового стопа
    else:
        far_levels = [r for r in resistances_above if r >= stop]
    snapped_stop = _nearest(stop, far_levels, stop_snap)
    if snapped_stop is not None:
        stop = snapped_stop
        snapped.append("стоп")

    risk = abs(entry - stop)
    if risk <= 0:
        raise ExitError("Стоп совпал со входом — задай другой множитель ATR.")

    # Подтяжка тейков к уровням в сторону движения.
    take_candidates = resistances_above if side == "long" else supports_below

    snapped_t1 = _nearest(take1, take_candidates, snap_dist)
    if snapped_t1 is not None:
        ok = (snapped_t1 - entry) * direction > 0  # тейк в сторону движения
        if ok:
            take1 = snapped_t1
            snapped.append("тейк1")

    snapped_t2 = _nearest(take2, take_candidates, snap_dist)
    if snapped_t2 is not None:
        ok = (snapped_t2 - entry) * direction > (take1 - entry) * direction  # дальше тейк1
        if ok:
            take2 = snapped_t2
            snapped.append("тейк2")

    # Гарантируем корректный порядок уровней.
    if (take2 - entry) * direction <= (take1 - entry) * direction:
        take2 = entry + direction * _RR2 * base_risk

    rr1 = abs(take1 - entry) / risk
    rr2 = abs(take2 - entry) / risk

    return {
        "symbol": symbol,
        "timeframe": timeframe.lower(),
        "side": side,
        "entry": entry,
        "atr": atr_val,
        "atr_mult": atr_mult,
        "risk": risk,
        "stop": stop,
        "take1": take1,
        "take2": take2,
        "rr1": rr1,
        "rr2": rr2,
        "snapped": snapped,
    }
