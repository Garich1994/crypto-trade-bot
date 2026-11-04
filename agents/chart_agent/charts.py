"""Chart Agent — PNG-графики через matplotlib (тёмная премиальная тема).

Backend Agg (без GUI). Функции возвращают путь к временному PNG; удаляет файл
вызывающая сторона после отправки.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")  # без GUI, до импорта pyplot

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from agents.data_agent import get_klines  # noqa: E402
from agents.ta_agent import indicators as ind  # noqa: E402

DEFAULT_TIMEFRAME = "1h"
_FETCH = 250     # сколько свечей тянем (для корректных EMA/RSI)
_SHOW = 150      # сколько показываем

# Нейтральные подписи — без «покупай/продавай».
CHART_LABELS = (
    "EMA 20", "EMA 50", "Объём", "RSI 14", "Цена", "Вход", "Стоп", "Тейк",
)

# Палитра в духе Binance dark.
_BG = "#0b0e11"
_GRID = "#2b3139"
_TEXT = "#eaecef"
_UP = "#0ecb81"
_DOWN = "#f6465d"
_EMA20 = "#f0b90b"
_EMA50 = "#848e9c"
_RSI = "#f0b90b"
_ENTRY = "#ffffff"
_STOP = "#f6465d"
_TAKE = "#0ecb81"


def _temp_png() -> str:
    fd, path = tempfile.mkstemp(suffix=".png", prefix="chart_")
    os.close(fd)
    return path


def _to_arrays(rows: list[list]):
    o = np.array([float(r[1]) for r in rows])
    h = np.array([float(r[2]) for r in rows])
    low = np.array([float(r[3]) for r in rows])
    c = np.array([float(r[4]) for r in rows])
    v = np.array([float(r[5]) for r in rows])
    t = np.array([int(r[0]) for r in rows])
    return o, h, low, c, v, t


def _style_axis(ax):
    ax.set_facecolor(_BG)
    ax.grid(True, color=_GRID, linewidth=0.5, alpha=0.6)
    ax.tick_params(colors=_TEXT, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(_GRID)


def _render(
    symbol: str,
    timeframe: str,
    rows: list[list],
    levels: dict | None = None,
) -> str:
    """Общий рендер: свечи + EMA + объём + RSI. Возвращает путь к PNG."""
    import pandas as pd

    o, h, low, c, v, t = _to_arrays(rows)
    close_s = pd.Series(c)
    ema20 = ind.ema(close_s, 20).to_numpy()
    ema50 = ind.ema(close_s, 50).to_numpy()
    rsi = ind.rsi(close_s, 14).to_numpy()

    n = len(c)
    show = min(_SHOW, n)
    s = n - show  # срез для отображения
    x = np.arange(show)
    up = c[s:] >= o[s:]
    colors = np.where(up, _UP, _DOWN)

    fig = plt.figure(figsize=(12, 7), dpi=100, facecolor=_BG)
    gs = fig.add_gridspec(3, 1, height_ratios=[3, 1, 1], hspace=0.08)
    ax_price = fig.add_subplot(gs[0])
    ax_vol = fig.add_subplot(gs[1], sharex=ax_price)
    ax_rsi = fig.add_subplot(gs[2], sharex=ax_price)

    for ax in (ax_price, ax_vol, ax_rsi):
        _style_axis(ax)

    # Свечи: фитили + тела.
    ax_price.vlines(x, low[s:], h[s:], color=colors, linewidth=0.8)
    body_bottom = np.minimum(o[s:], c[s:])
    body_height = np.abs(c[s:] - o[s:])
    body_height = np.where(body_height == 0, (h[s:] - low[s:]) * 0.001 + 1e-9, body_height)
    ax_price.bar(x, body_height, bottom=body_bottom, width=0.7, color=colors)

    # EMA.
    ax_price.plot(x, ema20[s:], color=_EMA20, linewidth=1.2, label=CHART_LABELS[0])
    ax_price.plot(x, ema50[s:], color=_EMA50, linewidth=1.2, label=CHART_LABELS[1])

    # Уровни (для бэктеста).
    if levels:
        if levels.get("entry") is not None:
            ax_price.axhline(levels["entry"], color=_ENTRY, linewidth=1.0,
                             linestyle="--", label=CHART_LABELS[5])
        if levels.get("stop") is not None:
            ax_price.axhline(levels["stop"], color=_STOP, linewidth=1.0,
                             linestyle="--", label=CHART_LABELS[6])
        for tk in levels.get("takes", []):
            ax_price.axhline(tk, color=_TAKE, linewidth=1.0, linestyle="--")

    leg = ax_price.legend(loc="upper left", fontsize=8, facecolor=_BG,
                          edgecolor=_GRID, labelcolor=_TEXT)
    if leg:
        leg.get_frame().set_alpha(0.6)
    ax_price.set_title(f"{symbol} · {timeframe}", color=_TEXT, fontsize=13, loc="left")
    ax_price.set_ylabel(CHART_LABELS[4], color=_TEXT, fontsize=9)

    # Объём.
    ax_vol.bar(x, v[s:], width=0.7, color=colors, alpha=0.5)
    ax_vol.set_ylabel(CHART_LABELS[2], color=_TEXT, fontsize=9)

    # RSI.
    ax_rsi.plot(x, rsi[s:], color=_RSI, linewidth=1.2)
    ax_rsi.axhline(70, color=_DOWN, linewidth=0.8, linestyle="--", alpha=0.7)
    ax_rsi.axhline(30, color=_UP, linewidth=0.8, linestyle="--", alpha=0.7)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel(CHART_LABELS[3], color=_TEXT, fontsize=9)

    # Метки времени по оси X (несколько).
    ticks = np.linspace(0, show - 1, min(6, show)).astype(int)
    labels = []
    for i in ticks:
        dt = datetime.fromtimestamp(t[s + i] / 1000, tz=timezone.utc)
        labels.append(dt.strftime("%m-%d %H:%M"))
    ax_rsi.set_xticks(ticks)
    ax_rsi.set_xticklabels(labels, rotation=0)
    for ax in (ax_price, ax_vol):
        plt.setp(ax.get_xticklabels(), visible=False)

    path = _temp_png()
    fig.savefig(path, facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    return path


def render_analysis(symbol: str, timeframe: str, rows: list[list]) -> str:
    """Отрисовать аналитический график по готовым свечам (без сети)."""
    return _render(symbol.upper(), timeframe, rows, levels=None)


def render_backtest(
    symbol: str,
    timeframe: str,
    rows: list[list],
    entry: float,
    stop: float,
    take,
) -> str:
    """График с горизонтальными линиями входа/стопа/тейков (без сети)."""
    takes = take if isinstance(take, (list, tuple)) else [take]
    levels = {"entry": float(entry), "stop": float(stop),
              "takes": [float(x) for x in takes]}
    return _render(symbol.upper(), timeframe, rows, levels=levels)


async def plot_analysis(symbol: str, timeframe: str = DEFAULT_TIMEFRAME) -> str:
    """Скачать свечи и построить аналитический график. Возвращает путь к PNG."""
    rows = await get_klines(symbol, timeframe, limit=_FETCH)
    return render_analysis(symbol, timeframe, rows)


async def plot_backtest(
    symbol: str,
    entry: float,
    stop: float,
    take,
    timeframe: str = DEFAULT_TIMEFRAME,
) -> str:
    """Скачать свечи и построить график с уровнями сделки. Возвращает путь к PNG."""
    rows = await get_klines(symbol, timeframe, limit=_FETCH)
    return render_backtest(symbol, timeframe, rows, entry, stop, take)
