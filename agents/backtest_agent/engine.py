"""Backtest Agent — исторический replay уровней сделки + Монте-Карло.

Честный механический прогон по прошлым свечам, без предсказаний. Комиссии и
проскальзывание не моделируются (упрощение). Никаких торговых рекомендаций.

Машина времени (time_machine): к историческому replay добавлены 10 000 симуляций
Монте-Карло из собственных 5-минутных баров пары за последние ~60 дней. Фиксированный
seed даёт повторяемость. Это симуляция из прошлых баров, а не прогноз будущего.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import numpy as np

from agents.data_agent import (
    DataAgentError,
    get_klines_history,
    get_klines_range,
)

#: Макс. число свечей удержания позиции до закрытия по рынку.
MAX_CANDLES_IN_TRADE = 100
DEFAULT_MAX_CANDLES = 5000
_ALLOWED_SIDES = {"long", "short"}
_SIDE_ALIASES = {"лонг": "long", "шорт": "short"}

# --- Монте-Карло (Машина времени) ---
MC_REPLAYS = 10_000       # число симуляций
MC_SEED = 19              # фиксированный seed → повторяемость
MC_HORIZON = 288          # баров вперёд (288 5-мин баров = 24 часа)
MC_WINDOW = 2             # окно по времени суток, ± баров
MC_DAYS = 60              # глубина пула (дней 5-мин баров)
_BARS_PER_DAY = 288       # 5-мин баров в сутках
_MC_MIN_BARS = 2000       # минимум баров в пуле для честного расчёта


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


# ---------------------------------------------------------------------------
# Монте-Карло: 10 000 симуляций «остатка» на собственных 5-мин барах пары.
# ---------------------------------------------------------------------------


def _build_pool(raw: list[list]) -> list[np.ndarray]:
    """Пул отношений high/low/close к закрытию предыдущего бара, по времени суток.

    Возвращает список из _BARS_PER_DAY массивов (N×3): для каждого индекса времени
    суток — набор баров-доноров (hr, lr, cr), собранных из последних ~60 дней.
    """
    buckets: list[list[tuple[float, float, float]]] = [[] for _ in range(_BARS_PER_DAY)]
    prev_close: float | None = None
    for c in raw:
        try:
            open_ms = int(c[0])
            high = float(c[2])
            low = float(c[3])
            close = float(c[4])
        except (TypeError, ValueError, IndexError):
            prev_close = None
            continue
        if prev_close is not None and prev_close > 0:
            dt = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
            idx = (dt.hour * 60 + dt.minute) // 5
            if 0 <= idx < _BARS_PER_DAY:
                buckets[idx].append((high / prev_close, low / prev_close,
                                     close / prev_close))
        prev_close = close

    # Кандидаты по каждому индексу: объединяем окно ±MC_WINDOW по времени суток.
    pool: list[np.ndarray] = []
    for idx in range(_BARS_PER_DAY):
        rows: list[tuple[float, float, float]] = []
        for d in range(-MC_WINDOW, MC_WINDOW + 1):
            rows.extend(buckets[(idx + d) % _BARS_PER_DAY])
        pool.append(np.asarray(rows, dtype=float) if rows else np.empty((0, 3)))
    return pool


def _simulate(
    pool: list[np.ndarray],
    side: str,
    entry: float,
    stop: float,
    target: float,
    replays: int,
    seed: int,
    horizon: int,
    start_idx: int,
) -> dict:
    """Векторизованная симуляция `replays` путей на `horizon` баров вперёд.

    По каждому бару проверяется сначала стоп, затем тейк (касание обоих = стоп).
    Цена идёт до конца окна всегда — так получаем честное распределение цены
    «на конец» и исход первого касания отдельно.
    """
    rng = np.random.default_rng(seed)
    all_rows = np.vstack([p for p in pool if len(p)]) if any(len(p) for p in pool) \
        else np.empty((0, 3))

    price = np.full(replays, float(entry))
    first = np.zeros(replays, dtype=np.int8)  # 0 нет, 1 тейк, 2 стоп
    risk = abs(entry - stop)

    for t in range(horizon):
        idx = (start_idx + t) % _BARS_PER_DAY
        cand = pool[idx]
        if len(cand) == 0:
            cand = all_rows
        if len(cand) == 0:
            break
        pick = rng.integers(0, len(cand), size=replays)
        hr = cand[pick, 0]
        lr = cand[pick, 1]
        cr = cand[pick, 2]
        high = price * hr
        low = price * lr
        close = price * cr

        open_mask = first == 0
        if side == "long":
            stop_hit = open_mask & (low <= stop)
            tgt_hit = open_mask & ~stop_hit & (high >= target)
        else:  # short
            stop_hit = open_mask & (high >= stop)
            tgt_hit = open_mask & ~stop_hit & (low <= target)
        first[stop_hit] = 2
        first[tgt_hit] = 1

        price = close

    terminal = price
    target_n = int((first == 1).sum())
    stop_n = int((first == 2).sum())
    neither_n = replays - target_n - stop_n

    if side == "long":
        above = terminal >= entry
    else:
        above = terminal <= entry
    green_n = int(((first == 1) | above).sum())

    # R: тейк +1, стоп -1, иначе — расстояние от входа на конец / риск (со знаком).
    direction = 1.0 if side == "long" else -1.0
    r = np.where(first == 1, 1.0,
                 np.where(first == 2, -1.0,
                          direction * (terminal - entry) / risk))
    avg_r = float(r.mean())

    return {
        "target_first_pct": round(100 * target_n / replays, 1),
        "stop_first_pct": round(100 * stop_n / replays, 1),
        "neither_pct": round(100 * neither_n / replays, 1),
        "green_pct": round(100 * green_n / replays, 1),
        "median_close": float(np.median(terminal)),
        "p5_close": float(np.percentile(terminal, 5)),
        "p95_close": float(np.percentile(terminal, 95)),
        "avg_r": round(avg_r, 3),
        "_target_n": target_n,
        "_stop_n": stop_n,
    }


def _mc_verdict(target_n: int, stop_n: int, rr: float) -> tuple[str, list[str]]:
    """Честный вердикт из долей (учитывает R тейка через точку безубытка 1/(1+R))."""
    sided = target_n + stop_n
    if sided == 0:
        return "Ни одна симуляция не дошла до тейка или стопа.", []
    share = target_n / sided
    breakeven = 1.0 / (1.0 + rr)
    diff = (share - breakeven) * 100  # в процентных пунктах
    if abs(diff) < 5:
        verdict = "Почти монетка" if abs(rr - 1.0) < 0.1 else "Близко к безубытку"
    elif abs(diff) < 15:
        verdict = "Склоняется к тейку" if diff > 0 else "Склоняется к стопу"
    else:
        verdict = "Сильно в сторону тейка" if diff > 0 else "Сильно в сторону стопа"
    detail = (f"{verdict}: из касаний тейк первым в {share * 100:.1f}% "
              f"(безубыток при R:R {rr:.2f} — {breakeven * 100:.1f}%).")
    return detail, []


async def monte_carlo(
    symbol: str,
    side: str,
    entry: float,
    stop: float,
    target: float,
    replays: int = MC_REPLAYS,
    seed: int = MC_SEED,
    horizon: int = MC_HORIZON,
    days: int = MC_DAYS,
) -> dict:
    """10 000 симуляций пути на 5-мин барах пары за последние ~`days` дней."""
    symbol = symbol.upper().strip()
    side = normalize_side(side)
    entry, stop, target = float(entry), float(stop), float(target)
    _validate_levels(side, entry, stop, target)
    risk = abs(entry - stop)
    if risk <= 0:
        raise BacktestError("Стоп не может совпадать со входом.")

    total = min(max(days, 1) * _BARS_PER_DAY, 18_000)
    raw = await get_klines_range(symbol, "5m", total)
    if len(raw) < _MC_MIN_BARS:
        raise BacktestError(
            "Слишком мало 5-минутных баров в истории пары для честной симуляции."
        )

    pool = _build_pool(raw)
    now = datetime.now(timezone.utc)
    start_idx = (now.hour * 60 + now.minute) // 5

    mc = await asyncio.to_thread(
        _simulate, pool, side, entry, stop, target,
        replays, seed, horizon, start_idx,
    )
    target_n = mc.pop("_target_n")
    stop_n = mc.pop("_stop_n")
    reward = abs(target - entry)
    rr = reward / risk
    verdict, _ = _mc_verdict(target_n, stop_n, rr)

    notes: list[str] = []
    if mc["neither_pct"] > 40:
        notes.append(
            f"{mc['neither_pct']}% симуляций не дошли ни до тейка, ни до стопа — "
            f"исход решает цена на конец окна."
        )
    share = target_n / (target_n + stop_n) if (target_n + stop_n) else 0.0
    lean = (share - 1.0 / (1.0 + rr))
    if (lean > 0 and mc["avg_r"] < 0) or (lean < 0 and mc["avg_r"] > 0):
        notes.append(
            f"Средний R ({mc['avg_r']:+.3f}) смотрит в другую сторону из-за "
            f"симуляций без касания."
        )

    mc.update({
        "symbol": symbol, "side": side, "entry": entry, "stop": stop,
        "target": target, "risk": risk, "reward": reward, "rr": round(rr, 2),
        "replays": replays, "seed": seed, "horizon_bars": horizon,
        "window_bars": MC_WINDOW, "days": days, "pool_bars": sum(len(p) for p in pool),
        "verdict": verdict, "notes": notes,
        "fees_note": "Комиссии и проскальзывание НЕ учтены — реальный исход хуже.",
    })
    return mc


async def time_machine(
    symbol: str,
    timeframe: str,
    side: str,
    entry: float,
    stop: float,
    target: float,
) -> dict:
    """Машина времени: Монте-Карло (5-мин бары) + исторический replay правила.

    Возвращает dict {mc, history, seed}. history — существующий backtest по
    выбранному таймфрейму (сколько раз цена входа касалась и чем закрывалось);
    если касаний не было — history = {"ok": False, "reason": ...}.
    """
    mc = await monte_carlo(symbol, side, entry, stop, target)
    history: dict
    try:
        bt = await backtest(symbol, timeframe, side, entry, stop, target)
        history = {
            "ok": True,
            "trades": bt["trades"],
            "wins": bt["wins"],
            "losses": bt["losses"],
            "neither": bt["trades"] - bt["wins"] - bt["losses"],
            "win_rate": bt["win_rate"],
            "avg_R": bt["avg_R"],
            "candles_analyzed": bt["candles_analyzed"],
        }
    except (BacktestError, DataAgentError) as exc:
        history = {"ok": False, "reason": str(exc)}

    return {"mc": mc, "history": history, "seed": MC_SEED}
