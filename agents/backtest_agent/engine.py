"""Backtest Agent — исторический replay уровней сделки.

Честный механический прогон по прошлым свечам, без предсказаний. Комиссии и
проскальзывание не моделируются (упрощение). Никаких торговых рекомендаций.
"""
from __future__ import annotations

from agents.data_agent import DataAgentError, get_klines_history

#: Макс. число свечей удержания позиции до закрытия по рынку.
MAX_CANDLES_IN_TRADE = 100
DEFAULT_MAX_CANDLES = 5000
_ALLOWED_SIDES = {"long", "short"}
_SIDE_ALIASES = {"лонг": "long", "шорт": "short"}


class BacktestError(Exception):
    """Понятная наружу ошибка бэктеста (валидация, нет касаний входа и т.п.)."""


def normalize_side(side: str) -> str:
    """Привести сторону к 'long'/'short' или бросить BacktestError."""
    s = side.strip().lower()
    s = _SIDE_ALIASES.get(s, s)
    if s not in _ALLOWED_SIDES:
        raise BacktestError("Сторона должна быть long или short.")
    return s


def _validate_levels(side: str, entry: float, stop: float, take: float) -> None:
    if side == "long":
        if not (stop < entry < take):
            raise BacktestError(
                "Для лонга уровни должны идти так: стоп < вход < тейк."
            )
    else:  # short
        if not (take < entry < stop):
            raise BacktestError(
                "Для шорта уровни должны идти так: тейк < вход < стоп."
            )


def _trade_r(side: str, entry: float, exit_price: float, risk: float) -> float:
    direction = 1.0 if side == "long" else -1.0
    return (exit_price - entry) * direction / risk


def _replay(
    candles: list[tuple[float, float, float]],
    side: str,
    entry: float,
    stop: float,
    take: float,
    risk: float,
) -> list[float]:
    """Пройти по свечам и вернуть список R закрытых сделок."""
    results: list[float] = []
    in_position = False
    held = 0

    for high, low, close in candles:
        if not in_position:
            if low <= entry <= high:
                in_position = True
                held = 0
            continue

        held += 1
        exit_price: float | None = None

        if side == "long":
            if low <= stop:          # стоп проверяем первым (консервативно)
                exit_price = stop
            elif high >= take:
                exit_price = take
        else:  # short
            if high >= stop:
                exit_price = stop
            elif low <= take:
                exit_price = take

        if exit_price is None and held >= MAX_CANDLES_IN_TRADE:
            exit_price = close  # закрытие по рынку

        if exit_price is not None:
            results.append(_trade_r(side, entry, exit_price, risk))
            in_position = False

    return results


def _aggregate(results: list[float]) -> dict:
    trades = len(results)
    wins = sum(1 for r in results if r > 0)
    losses = sum(1 for r in results if r < 0)

    gross_profit = sum(r for r in results if r > 0)
    gross_loss = -sum(r for r in results if r < 0)  # положительное число

    win_rate = wins / trades if trades else 0.0
    avg_r = sum(results) / trades if trades else 0.0
    expectancy = avg_r  # ожидаемый R на сделку = средний R

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = None  # убыточных сделок не было → бесконечность

    # Максимальная просадка по кривой эквити в R.
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in results:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "avg_R": avg_r,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
    }


async def backtest(
    symbol: str,
    timeframe: str,
    side: str,
    entry_price: float,
    stop_price: float,
    take_price: float,
    max_candles: int = DEFAULT_MAX_CANDLES,
) -> dict:
    """Прогнать уровни сделки по истории и вернуть агрегированную статистику.

    Бросает BacktestError (валидация / нет касаний входа) или DataAgentError
    (API недоступен). Оба показываются пользователю без трейсбека.
    """
    symbol = symbol.upper().strip()
    side = normalize_side(side)

    entry = float(entry_price)
    stop = float(stop_price)
    take = float(take_price)
    _validate_levels(side, entry, stop, take)

    risk = abs(entry - stop)
    if risk <= 0:
        raise BacktestError("Стоп не может совпадать со входом.")

    limit = max(1, min(int(max_candles), DEFAULT_MAX_CANDLES))
    raw = await get_klines_history(symbol, timeframe, limit)

    # (high, low, close) в хронологическом порядке.
    candles: list[tuple[float, float, float]] = []
    for c in raw:
        try:
            candles.append((float(c[2]), float(c[3]), float(c[4])))
        except (TypeError, ValueError, IndexError):
            continue
    if not candles:
        raise DataAgentError("Не удалось разобрать свечи от Binance.")

    results = _replay(candles, side, entry, stop, take, risk)
    if not results:
        raise BacktestError(
            "В истории не было ни одного касания цены входа — "
            "нечего считать. Попробуй другой уровень входа или таймфрейм."
        )

    reward = abs(take - entry)
    stats = _aggregate(results)
    stats.update(
        {
            "symbol": symbol,
            "timeframe": timeframe.lower(),
            "side": side,
            "entry": entry,
            "stop": stop,
            "take": take,
            "risk": risk,
            "reward": reward,
            "rr": reward / risk,
            "candles_analyzed": len(candles),
        }
    )
    return stats
