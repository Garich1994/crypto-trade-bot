"""Mini App v1 — локальный aiohttp-сервер дашборда.

Отдаёт index.html и /api/* (JSON). API вызывает функции агентов напрямую, без
дублирования их логики. Запускается рядом с ботом (порт 8080).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from aiohttp import web

# Имена агентов на уровне модуля — чтобы подменять в тестах.
from agents.coordinator_agent import build_brief
from agents.data_agent import get_klines, get_ticker_24h
from agents.journal_agent import stats as journal_stats
from agents.market_index import market_index
from agents.paper_agent import portfolio as paper_portfolio
from agents.scanner_agent import scan
from agents.whale_agent import get_whales

_INDEX = Path(__file__).resolve().parent / "index.html"

# Хост/порт берём из окружения (удобно для Docker).
HOST = os.environ.get("MINIAPP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MINIAPP_PORT", "8080"))
# Если задан DOMAIN (прод за HTTPS) — ссылка на дашборд идёт на него, иначе локально.
_DOMAIN = os.environ.get("DOMAIN", "").strip()
DASHBOARD_URL = f"https://{_DOMAIN}" if _DOMAIN else f"http://localhost:{PORT}"

#: Монеты рынка на дашборде.
_MARKET_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def _resp(data, status: int = 200) -> web.Response:
    return web.json_response(
        data,
        status=status,
        dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str),
    )


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(_INDEX)


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
    try:
        return _resp({"ok": True, "index": await market_index()})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_scan(request: web.Request) -> web.Response:
    try:
        tf = request.query.get("tf", "1h")
        findings = await scan(timeframe=tf, top_n=30)
        return _resp({"ok": True, "findings": findings})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


async def api_brief(request: web.Request) -> web.Response:
    try:
        brief = await build_brief(top=3)
        return _resp({"ok": True, "brief": brief})
    except Exception as exc:  # noqa: BLE001
        return _resp({"ok": False, "error": str(exc)})


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
    app.router.add_get("/api/market", api_market)
    app.router.add_get("/api/market_index", api_market_index)
    app.router.add_get("/api/scan", api_scan)
    app.router.add_get("/api/brief", api_brief)
    app.router.add_get("/api/whales", api_whales)
    app.router.add_get("/api/journal/{user_id}", api_journal)
    app.router.add_get("/api/paper/{user_id}", api_paper)
    return app


async def start_miniapp(host: str = HOST, port: int = PORT) -> web.AppRunner:
    """Запустить сервер дашборда, вернуть runner (для остановки)."""
    runner = web.AppRunner(create_app())
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
