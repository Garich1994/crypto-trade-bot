"""Coordinator Agent — автономный пайплайн (дайджест).

Координатор только дирижирует: вызывает функции готовых агентов и собирает
структурированный результat. Своей торговой логики не содержит.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Имена импортируются на уровень модуля, чтобы их можно было подменять в тестах.
from agents.entry_agent import score_entry
from agents.exit_agent import plan_exit
from agents.news_agent import get_crypto_news
from agents.scanner_agent import scan
from agents.skeptic_agent import skeptic
from agents.ta_agent import analyze

DEFAULT_TIMEFRAME = "1h"
DEFAULT_TOP = 5


def _now():
    return datetime.now(timezone.utc)


async def build_brief(
    symbols: list[str] | None = None,
    top: int = DEFAULT_TOP,
    timeframe: str = DEFAULT_TIMEFRAME,
) -> dict:
    """Собрать структурированный дайджест (dict, не текст).

    Цепочка на каждую пару: ta-анализ + entry-скоринг + exit-план + skeptic.
    Риск-гейт отсеивает идеи с FAIL по стопу или RSI.
    """
    try:
        news = await get_crypto_news(3)
    except Exception:  # noqa: BLE001 — новости не должны валить бриф
        news = []

    if symbols:
        candidates = [{"symbol": s.upper()} for s in symbols]
        scanned = len(candidates)
    else:
        findings = await scan(timeframe, 30)
        candidates = findings[:top]
        scanned = len(findings)

    ideas: list[dict] = []
    filtered: list[dict] = []

    for cand in candidates[:top]:
        symbol = str(cand["symbol"]).upper()
        try:
            a = await analyze(symbol, timeframe)
            price = float(a["price"])
            ema200 = float(a["ema"]["ema200"])
            side = "long" if price >= ema200 else "short"
            sc = await score_entry(symbol, timeframe, side)
            ex = await plan_exit(symbol, timeframe, side, price)
            sk = await skeptic(symbol, side, price, ex["stop"], ex["take1"])
        except Exception:  # noqa: BLE001 — сбой по паре не валит весь бриф
            continue

        statuses = {ch["name"]: ch["status"] for ch in sk["checks"]}
        gate_fail = (
            statuses.get("RSI против направления") == "FAIL"
            or statuses.get("Стоп vs волатильность") == "FAIL"
        )

        record = {
            "symbol": symbol,
            "side": side,
            "score": sc["score"],
            "entry": price,
            "stop": ex["stop"],
            "take1": ex["take1"],
            "take2": ex["take2"],
            "change_24h": a.get("change_24h"),
            "trend": a.get("trend"),
            "rsi": a.get("rsi"),
            "skeptic_passed": sk["passed"],
            "skeptic_total": sk["total"],
            "weak_points": sk["weak_points"],
        }

        if gate_fail:
            filtered.append(
                {
                    "symbol": symbol,
                    "reason": "; ".join(sk["weak_points"]) or "риск-гейт: стоп/RSI",
                }
            )
        else:
            ideas.append(record)

    ideas.sort(key=lambda r: r["score"], reverse=True)

    return {
        "generated_at": _now().isoformat(),
        "timeframe": timeframe,
        "scanned": scanned,
        "market_summary": f"наблюдается {scanned} ситуаций по топ-парам",
        "news": news,
        "ideas": ideas,
        "filtered_out": filtered,
    }
