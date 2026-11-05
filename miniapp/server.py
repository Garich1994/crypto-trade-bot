"""Mini App v1 — локальный aiohttp-сервер дашборда.

Отдаёт index.html и /api/* (JSON). API вызывает функции агентов напрямую, без
дублирования их логики. Запускается рядом с ботом (порт 8080).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

# Имена агентов на уровне модуля — чтобы подменять в тестах.
from agents.alerts_agent import list_alerts
from agents.budget_agent import calc_position
from agents.coordinator_agent import build_brief
from agents.data_agent import get_klines, get_ticker_24h
from agents.entry_agent import score_entry
from agents.exit_agent import plan_exit
from agents.journal_agent import stats as journal_stats
from agents.market_index import market_index
from agents.paper_agent import portfolio as paper_portfolio
from agents.scanner_agent import scan
from agents.skeptic_agent import skeptic
from agents.ta_agent import analyze
from agents.whale_agent import get_whales

_INDEX = Path(__file__).resolve().parent / "index.html"

# Хост/порт берём из окружения (удобно для Docker).
HOST = os.environ.get("MINIAPP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MINIAPP_PORT", "8080"))
# Если задан DOMAIN (прод за HTTPS) — ссылка на дашборд идёт на него, иначе локально.
_DOMAIN = os.environ.get("DOMAIN", "").strip()
DASHBOARD_URL = f"https://{_DOMAIN}" if _DOMAIN else f"http://localhost:{PORT}"
# Имя бота для Telegram Login Widget на сайте (вне Telegram). Пусто — гостевой режим.
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").strip().lstrip("@")
# Версия сборки — для подписи и обхода кэша Telegram (?v= / no-cache).
APP_VERSION = (os.environ.get("APP_VERSION", "").strip()
               or datetime.now(timezone.utc).strftime("%Y%m%d%H%M"))

#: Монеты рынка на дашборде.
_MARKET_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

# Тяжёлые расчёты (индекс рынка и скан) считаются в фоне и кэшируются — обработчики
# API отдают последний результат мгновенно, без тяжёлых вычислений внутри.
_REFRESH_INTERVAL = 300  # секунд (5 минут)
AUTO_REFRESH = True       # в тестах выключается, чтобы управлять кэшем вручную
_INDEX_CACHE: dict = {"data": None, "updated_at": None}
_SCAN_CACHE: dict = {"data": None, "updated_at": None}
_BRIEF_CACHE: dict = {"data": None, "updated_at": None}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _refresh_index() -> None:
    try:
        data = await market_index()
        _INDEX_CACHE["data"] = data
        _INDEX_CACHE["updated_at"] = data.get("updated_at") or _now_iso()
    except Exception:  # noqa: BLE001 — оставляем прошлый кэш, не роняем цикл
        logger.exception("Не удалось обновить индекс рынка")


async def _refresh_scan() -> None:
    try:
        findings = await scan(timeframe="1h", top_n=30)
        _SCAN_CACHE["data"] = findings
        _SCAN_CACHE["updated_at"] = _now_iso()
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось обновить скан")


async def _refresh_brief() -> None:
    try:
        brief = await build_brief(top=3)
        _BRIEF_CACHE["data"] = brief
        _BRIEF_CACHE["updated_at"] = _now_iso()
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось обновить сводку (brief)")


async def refresh_now() -> None:
    """Пересчитать индекс, скан и сводку сейчас (используется циклом и тестами)."""
    await asyncio.gather(_refresh_index(), _refresh_scan(), _refresh_brief())


async def _refresh_loop() -> None:
    """Фоновый пересчёт каждые 5 минут. Первый расчёт — сразу при старте."""
    while True:
        await refresh_now()
        await asyncio.sleep(_REFRESH_INTERVAL)


async def _on_startup(app: web.Application) -> None:
    if AUTO_REFRESH:
        app["refresh_task"] = asyncio.create_task(_refresh_loop())


async def _on_cleanup(app: web.Application) -> None:
    task = app.get("refresh_task")
    if task is not None:
        task.cancel()


def _resp(data, status: int = 200) -> web.Response:
    return web.json_response(
        data,
        status=status,
        dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str),
    )


async def index(request: web.Request) -> web.Response:
    # no-cache: Telegram WebApp агрессивно кэширует страницу — отдаём всегда свежую.
    return web.FileResponse(_INDEX, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


async def api_config(request: web.Request) -> web.Response:
    """Конфиг для фронта: имя бота для входа и версия сборки."""
    return _resp({"ok": True, "bot_username": BOT_USERNAME, "version": APP_VERSION})


async def healthz(request: web.Request) -> web.Response:
    """Проверка живости для хостинга/мониторинга."""
    return _resp({"ok": True})


async def api_market(request: web.Request) -> web.Response:
    try:
        coins = []
        for sym in _MARKET_SYMBOLS:
            ticker = await get_ticker_24h(sym)
            kl = await get_klines(sym, "1h", 30)
            coins.append(
                {
                    "symbol": sym,
                    "price": float(ticker["lastPrice"]),
                    "change24h": float(ticker["priceChangePercent"]),
                    "spark": [float(r[4]) for r in kl],
                }
            )
        return _resp({"ok": True, "coins": coins})
    except Exception as exc:  # noqa: BLE001 — дашборд деградирует мягко
        return _resp({"ok": False, "error": str(exc)})


async def api_market_index(request: web.Request) -> web.Response:
    # Только отдача кэша — без тяжёлых вычислений в обработчике.
    if _INDEX_CACHE["data"] is None:
        return _resp({"ok": True, "warming": True})
    return _resp({
        "ok": True,
        "index": _INDEX_CACHE["data"],
        "updated_at": _INDEX_CACHE["updated_at"],
    })


async def api_scan(request: web.Request) -> web.Response:
    if _SCAN_CACHE["data"] is None:
        return _resp({"ok": True, "warming": True})
    return _resp({
        "ok": True,
        "findings": _SCAN_CACHE["data"],
        "updated_at": _SCAN_CACHE["updated_at"],
    })


async def _entry_price(symbol: str) -> float:
    ticker = await get_ticker_24h(symbol)
    return float(ticker["lastPrice"])


async def api_entry(request: web.Request) -> web.Response:
    try:
        symbol = request.query.get("symbol", "BTCUSDT").upper()
        tf = request.query.get("tf", "1h")
        side = request.query.get("side", "long")
        return _resp({"ok": True, "entry": await score_entry(symbol, tf, side)})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_exit(request: web.Request) -> web.Response:
    try:
        symbol = request.query.get("symbol", "BTCUSDT").upper()
        tf = request.query.get("tf", "1h")
        side = request.query.get("side", "long")
        entry = request.query.get("entry")
        entry = float(entry) if entry else await _entry_price(symbol)
        # Пресеты выхода задают множитель ATR (стоп/тейки). По умолчанию 1.5 (базовая).
        mult = float(request.query.get("mult", "1.5"))
        if mult <= 0:
            mult = 1.5
        return _resp({"ok": True, "exit": await plan_exit(symbol, tf, side, entry, mult)})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_ta(request: web.Request) -> web.Response:
    """Технический анализ пары для выбранного таймфрейма (все индикаторы заново)."""
    try:
        symbol = request.query.get("symbol", "BTCUSDT").upper()
        tf = request.query.get("tf", "1h")
        return _resp({"ok": True, "ta": await analyze(symbol, tf)})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_budget(request: web.Request) -> web.Response:
    try:
        balance = float(request.query.get("balance", "10000"))
        risk = float(request.query.get("risk", "1"))
        entry = float(request.query["entry"])
        stop = float(request.query["stop"])
        res = calc_position(balance, risk, entry, stop)
        return _resp({"ok": True, "budget": res})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_skeptic(request: web.Request) -> web.Response:
    try:
        symbol = request.query.get("symbol", "BTCUSDT").upper()
        tf = request.query.get("tf", "1h")
        side = request.query.get("side", "long")
        entry = request.query.get("entry")
        entry = float(entry) if entry else await _entry_price(symbol)
        ex = await plan_exit(symbol, tf, side, entry)
        sk = await skeptic(symbol, side, entry, ex["stop"], ex["take1"])
        return _resp({"ok": True, "skeptic": sk, "exit": ex})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_risk(request: web.Request) -> web.Response:
    try:
        symbol = request.query.get("symbol", "BTCUSDT").upper()
        tf = request.query.get("tf", "1h")
        side = request.query.get("side", "long")
        entry = request.query.get("entry")
        entry = float(entry) if entry else await _entry_price(symbol)
        ex = await plan_exit(symbol, tf, side, entry)
        sk = await skeptic(symbol, side, entry, ex["stop"], ex["take1"])
        st = {c["name"]: c["status"] for c in sk["checks"]}
        rr = ex["rr1"]
        checks = [
            {"name": "Стоп указан", "status": "PASS"},
            {"name": "R:R ≥ 1.5", "status": "PASS" if rr >= 1.5 else "FAIL"},
            {"name": "Тренд по EMA", "status": st.get("Тренд против", "PASS")},
            {"name": "RSI в зоне", "status": st.get("RSI против направления", "PASS")},
            {"name": "Волатильность", "status": st.get("Стоп vs волатильность", "PASS")},
            {"name": "Конфликты уровней", "status":
                "PASS" if st.get("Уровни против") == "PASS" else "FAIL"},
        ]
        verdict = "PASS" if all(c["status"] == "PASS" for c in checks) else "ВНИМАНИЕ"
        return _resp({"ok": True, "risk": {
            "verdict": verdict, "rr": rr, "checks": checks,
            "entry": entry, "stop": ex["stop"], "take1": ex["take1"]}})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_alerts(request: web.Request) -> web.Response:
    try:
        user_id = int(request.match_info["user_id"])
        return _resp({"ok": True, "alerts": list_alerts(user_id)})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_klines(request: web.Request) -> web.Response:
    try:
        symbol = request.query.get("symbol", "BTCUSDT").upper()
        tf = request.query.get("tf", "1h")
        limit = min(int(request.query.get("limit", "60")), 120)
        raw = await get_klines(symbol, tf, limit=limit)
        candles = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])]
                   for r in raw]
        return _resp({"ok": True, "symbol": symbol, "tf": tf, "candles": candles})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_brief(request: web.Request) -> web.Response:
    # Только отдача фонового кэша — без тяжёлой сборки сводки в обработчике.
    if _BRIEF_CACHE["data"] is None:
        return _resp({"ok": True, "warming": True})
    return _resp({
        "ok": True,
        "brief": _BRIEF_CACHE["data"],
        "updated_at": _BRIEF_CACHE["updated_at"],
    })


async def api_whales(request: web.Request) -> web.Response:
    symbol = request.query.get("symbol", "BTCUSDT").upper()
    try:
        data = await get_whales(symbol)
        return _resp({"ok": True, "whales": data})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_journal(request: web.Request) -> web.Response:
    try:
        user_id = int(request.match_info["user_id"])
        return _resp({"ok": True, "stats": journal_stats(user_id)})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_paper(request: web.Request) -> web.Response:
    try:
        user_id = int(request.match_info["user_id"])
        return _resp({"ok": True, "portfolio": paper_portfolio(user_id)})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/ta", api_ta)
    app.router.add_get("/api/market", api_market)
    app.router.add_get("/api/market_index", api_market_index)
    app.router.add_get("/api/scan", api_scan)
    app.router.add_get("/api/brief", api_brief)
    app.router.add_get("/api/whales", api_whales)
    app.router.add_get("/api/journal/{user_id}", api_journal)
    app.router.add_get("/api/paper/{user_id}", api_paper)
    app.router.add_get("/api/entry", api_entry)
    app.router.add_get("/api/exit", api_exit)
    app.router.add_get("/api/budget", api_budget)
    app.router.add_get("/api/skeptic", api_skeptic)
    app.router.add_get("/api/risk", api_risk)
    app.router.add_get("/api/alerts/{user_id}", api_alerts)
    app.router.add_get("/api/klines", api_klines)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def start_miniapp(host: str = HOST, port: int = PORT) -> web.AppRunner:
    """Запустить сервер дашборда, вернуть runner (для остановки)."""
    runner = web.AppRunner(create_app())
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
