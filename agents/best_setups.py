"""Best Setups — автономный поиск лучших сетапов («Топ-идеи»).

Координатор ничего нового не считает: гоняет готовые функции агентов (сканер, TA,
entry-скоринг, exit-план, skeptic) по топ-30 USDT-парам, прогоняет каждую пару
через фильтр качества и кэширует топ-10. Фоновый пересчёт каждые 30 минут.

Фильтр отбора (строгий и честный):
  • скоринг входа ≥ 70 (качество сетапа по правилам, НЕ вероятность успеха);
  • скептик не нашёл слабых мест (passed == total);
  • риск-гейт PASS (как на экране /api/risk: R:R ≥ 1.5 и ключевые проверки PASS).

Нет сетапов, прошедших фильтр, — честно пустой список, слабые не натягиваются.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

# Имена агентов на уровне модуля — чтобы подменять их в тестах.
from agents.entry_agent import score_entry
from agents.exit_agent import plan_exit
from agents.scanner_agent import scan
from agents.skeptic_agent import skeptic

logger = logging.getLogger(__name__)

TIMEFRAME = "1h"
SCAN_TOP = 30          # сколько пар берём у сканера
MIN_SCORE = int(os.environ.get("BEST_MIN_SCORE", "70"))  # порог качества сетапа
TOP_N = 10             # сколько идей отдаём
INTERVAL = 1800        # пересчёт раз в 30 минут
EMPTY_TEXT = "Сильных сетапов по правилам сейчас нет"

AUTO_REFRESH = True    # в тестах выключается
_CACHE: dict = {"data": None, "updated_at": None}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def risk_gate(sk: dict, rr1: float) -> tuple[str, list[dict]]:
    """Риск-гейт по проверкам скептика + R:R — та же логика, что на экране риска."""
    st = {c["name"]: c["status"] for c in sk.get("checks", [])}
    checks = [
        {"name": "Стоп указан", "status": "PASS"},
        {"name": "R:R ≥ 1.5", "status": "PASS" if rr1 >= 1.5 else "FAIL"},
        {"name": "Тренд по EMA", "status": st.get("Тренд против", "PASS")},
        {"name": "RSI в зоне", "status": st.get("RSI против направления", "PASS")},
        {"name": "Волатильность", "status": st.get("Стоп vs волатильность", "PASS")},
        {"name": "Конфликты уровней",
         "status": "PASS" if st.get("Уровни против") == "PASS" else "FAIL"},
    ]
    verdict = "PASS" if all(c["status"] == "PASS" for c in checks) else "BLOCK"
    return verdict, checks


async def _evaluate_pair(finding: dict, timeframe: str, min_score: int) -> list[dict]:
    """Прогнать пару по обеим сторонам, вернуть прошедшие фильтр сетапы."""
    symbol = str(finding["symbol"]).upper()
    price = float(finding["price"])
    rsi = finding.get("rsi")
    pattern = finding.get("structure") or (finding.get("patterns") or [None])[0]
    out: list[dict] = []

    for side in ("long", "short"):
        try:
            sc = await score_entry(symbol, timeframe, side)
        except Exception:  # noqa: BLE001 — сбой по стороне не валит прогон
            continue
        if sc["score"] < min_score:
            continue
        try:
            ex = await plan_exit(symbol, timeframe, side, price)
            sk = await skeptic(symbol, side, price, ex["stop"], ex["take1"])
        except Exception:  # noqa: BLE001
            continue

        skeptic_ok = sk["passed"] == sk["total"]
        gate, _ = risk_gate(sk, ex["rr1"])
        if not skeptic_ok or gate != "PASS":
            continue

        out.append({
            "pair": symbol,
            "side": side,
            "score": sc["score"],
            "entry": ex["entry"],
            "stop": ex["stop"],
            "take1": ex["take1"],
            "take2": ex["take2"],
            "rr1": ex["rr1"],
            "rr2": ex["rr2"],
            "skeptic": {"passed": sk["passed"], "total": sk["total"],
                        "verdict": sk.get("summary", "")},
            "risk_gate": gate,
            "rsi": rsi,
            "pattern": pattern,
        })
    return out


async def compute_best(
    timeframe: str = TIMEFRAME,
    scan_top: int = SCAN_TOP,
    min_score: int = MIN_SCORE,
    top_n: int = TOP_N,
) -> dict:
    """Собрать топ-идеи. Возвращает dict с ideas/scanned/passed/updated_at."""
    findings = await scan(timeframe, scan_top)
    scanned = len(findings)
    ideas: list[dict] = []
    for f in findings:
        try:
            ideas.extend(await _evaluate_pair(f, timeframe, min_score))
        except Exception:  # noqa: BLE001
            continue
    ideas.sort(key=lambda r: r["score"], reverse=True)
    passed = len(ideas)
    return {
        "ideas": ideas[:top_n],
        "scanned": scanned,
        "passed": passed,
        "updated_at": _now_iso(),
    }


async def refresh_now() -> dict:
    """Пересчитать сейчас и положить в кэш (используется циклом, ботом и тестами)."""
    try:
        data = await compute_best()
        _CACHE["data"] = data
        _CACHE["updated_at"] = data["updated_at"]
        logger.info("Топ-идеи: просканировано %s пар, прошло фильтр %s, в топе %s",
                    data["scanned"], data["passed"], len(data["ideas"]))
        return data
    except Exception:  # noqa: BLE001 — оставляем прошлый кэш, не роняем цикл
        logger.exception("Топ-идеи: прогон не удался")
        return _CACHE["data"] or {"ideas": [], "scanned": 0, "passed": 0,
                                  "updated_at": _now_iso()}


def get_cached() -> dict | None:
    """Последний результат из кэша (None, если ещё не считали)."""
    return _CACHE["data"]


async def best_ideas(top_n: int = TOP_N) -> list[dict]:
    """Список топ-идей из кэша (для бота/автопуша). Пустой — если считаем или пусто."""
    data = _CACHE["data"]
    if data is None:
        return []
    return data["ideas"][:top_n]


async def _loop() -> None:
    """Фоновый пересчёт каждые 30 минут. Первый прогон — сразу при старте."""
    while True:
        await refresh_now()
        await asyncio.sleep(INTERVAL)
