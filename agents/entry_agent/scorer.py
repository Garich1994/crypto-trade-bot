"""Entry Timer Agent — скоринг условий входа (0–100), без призывов к действию."""
from __future__ import annotations

import pandas as pd

from agents.data_agent import DataAgentError, get_klines
from agents.ta_agent import indicators as ind

DEFAULT_TIMEFRAME = "1h"
_CANDLES = 300
_SIDES = {"long", "short"}
_SIDE_ALIASES = {"лонг": "long", "шорт": "short"}
_SIDE_RU = {"long": "лонг", "short": "шорт"}


class EntryError(Exception):
    """Понятная наружу ошибка (валидация стороны и т.п.)."""


def _normalize_side(side: str) -> str:
    s = side.strip().lower()
    s = _SIDE_ALIASES.get(s, s)
    if s not in _SIDES:
        raise EntryError("Сторона должна быть long или short.")
    return s


def _dir(side: str, s: float) -> float:
    """Для short переворачиваем знак силы фактора."""
    return s if side == "long" else -s


def _rsi_factor(side: str, rsi: float) -> tuple[float, str]:
    if rsi < 30:
        s, state = 1.0, "перепродан"
    elif rsi < 45:
        s, state = 0.5, "ниже середины"
    elif rsi <= 55:
        s, state = 0.0, "нейтрально"
    elif rsi <= 70:
        s, state = -0.5, "выше середины"
    else:
        s, state = -1.0, "перекуплен"
    return _dir(side, s) * 10.0, f"RSI {rsi:.0f} — {state}"


def _macd_factor(side: str, line: float, avg: float, hist: float) -> tuple[float, str]:
    if line > avg and hist > 0:
        s, direction = 1.0, "импульс вверх"
    elif line > avg:
        s, direction = 0.5, "скорее вверх"
    elif line < avg and hist < 0:
        s, direction = -1.0, "импульс вниз"
    else:
        s, direction = -0.5, "скорее вниз"
    return _dir(side, s) * 7.5, f"MACD — {direction}"


def _ema_factor(side: str, price: float, e20: float, e50: float, e200: float) -> tuple[float, str]:
    above = sum(1 for e in (e20, e50, e200) if price > e)
    s = (above - 1.5) / 1.5  # 3→+1, 0→-1
    return _dir(side, s) * 10.0, f"Цена vs EMA20/50/200 — выше {above}/3"


def _sr_factor(
    side: str, price: float, support: float | None, resistance: float | None
) -> tuple[float, str]:
    if support is None or resistance is None or resistance <= support:
        return 0.0, "Положение между S/R — мало уровней"
    pos = (price - support) / (resistance - support)
    pos = min(1.0, max(0.0, pos))
    s = (0.5 - pos) * 2.0  # у поддержки +1, у сопротивления -1
    return _dir(side, s) * 7.5, f"Положение между S/R — {pos * 100:.0f}% до сопротивления"


def _atr_factor(atr_pct: float) -> tuple[float, str]:
    # Высокая волатильность хуже для входа — независимо от стороны.
    if atr_pct < 1:
        s = 1.0
    elif atr_pct < 2:
        s = 0.3
    elif atr_pct < 3:
        s = -0.3
    else:
        s = -1.0
    return s * 7.5, f"Волатильность ATR {atr_pct:.2f}%"


def _volume_factor(ratio: float) -> tuple[float, str]:
    if ratio >= 2:
        s = 1.0
    elif ratio >= 1.2:
        s = 0.5
    elif ratio >= 0.8:
        s = 0.0
    elif ratio >= 0.5:
        s = -0.5
    else:
        s = -1.0
    return s * 7.5, f"Объём x{ratio:.1f} к среднему"


def _make_summary(side: str, score: int, factors: list[dict]) -> str:
    side_ru = _SIDE_RU[side]
    if score >= 70:
        head = f"Условия для оценки {side_ru}-входа сейчас выглядят относительно благоприятными."
    elif score >= 55:
        head = f"Условия для оценки {side_ru}-входа скорее нейтральные с небольшим перевесом в плюс."
    elif score >= 45:
        head = f"Условия для оценки {side_ru}-входа нейтральные, явного перевеса нет."
    elif score >= 30:
        head = f"Условия для оценки {side_ru}-входа выглядят слабыми."
    else:
        head = f"Условия для оценки {side_ru}-входа выглядят неблагоприятными."

    worst = min(factors, key=lambda f: f["points"])
    best = max(factors, key=lambda f: f["points"])
    if worst["points"] <= -5:
        tail = f" Главный минус: {worst['label'].lower()}."
    elif best["points"] >= 5:
        tail = f" Главный плюс: {best['label'].lower()}."
    else:
        tail = ""
    return head + tail


async def score_entry(symbol: str, timeframe: str = DEFAULT_TIMEFRAME, side: str = "long") -> dict:
    """Оценить условия входа. Возвращает {score, factors, summary}.

    Бросает EntryError (валидация) или DataAgentError (API недоступен).
    """
    symbol = symbol.upper().strip()
    side = _normalize_side(side)

    raw = await get_klines(symbol, timeframe, limit=_CANDLES)
    close = pd.to_numeric(pd.Series([c[4] for c in raw]), errors="coerce")
    high = pd.to_numeric(pd.Series([c[2] for c in raw]), errors="coerce")
    low = pd.to_numeric(pd.Series([c[3] for c in raw]), errors="coerce")
    volume = pd.to_numeric(pd.Series([c[5] for c in raw]), errors="coerce")
    if close.dropna().shape[0] < 30:
        raise DataAgentError("Недостаточно свечей для оценки.")

    price = float(close.iloc[-1])
    rsi = float(ind.rsi(close, 14).iloc[-1])
    macd_line, macd_avg, macd_hist = ind.macd(close, 12, 26, 9)
    e20 = float(ind.ema(close, 20).iloc[-1])
    e50 = float(ind.ema(close, 50).iloc[-1])
    e200 = float(ind.ema(close, 200).iloc[-1])
    atr_val = float(ind.atr(high, low, close, 14).iloc[-1])
    atr_pct = (atr_val / price * 100) if price else 0.0

    swing_highs, swing_lows = ind.swing_levels(high, low, window=5)
    support, resistance = ind.nearest_support_resistance(swing_highs, swing_lows, price)

    avg_vol = float(volume.tail(20).mean())
    vol_ratio = (float(volume.iloc[-1]) / avg_vol) if avg_vol > 0 else 0.0

    parts = [
        _rsi_factor(side, rsi),
        _macd_factor(side, float(macd_line.iloc[-1]), float(macd_avg.iloc[-1]), float(macd_hist.iloc[-1])),
        _ema_factor(side, price, e20, e50, e200),
        _sr_factor(side, price, support, resistance),
        _atr_factor(atr_pct),
        _volume_factor(vol_ratio),
    ]
    factors = [{"label": label, "points": round(points)} for points, label in parts]

    raw_score = 50.0 + sum(points for points, _ in parts)
    score = int(round(min(100.0, max(0.0, raw_score))))

    return {
        "symbol": symbol,
        "timeframe": timeframe.lower(),
        "side": side,
        "score": score,
        "factors": factors,
        "summary": _make_summary(side, score, factors),
    }
