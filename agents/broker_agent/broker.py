"""Broker Agent — исполнение спот-ордеров: paper → testnet → live.

Безопасность (железно):
- API-ключи ТОЛЬКО из окружения (BINANCE_API_KEY/BINANCE_API_SECRET); никогда не
  логируются, не печатаются, не возвращаются наружу.
- live включается только явной строкой BROKER_MODE=live в .env; командой — нельзя.
- Каждый live-ордер: риск-гейт PASS + подтверждение пользователя тапом кнопки.
  Без тапа ордер не уходит. Полной автоторговли НЕТ.
- Дневной лимит убытка: при превышении live блокируется до следующего дня.
- killswitch мгновенно блокирует live и testnet; разблокировка — вручную в .env.
- Маркет-ордера. Лимитные — следующим шагом.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

# Функции других агентов — на уровень модуля (патчатся в тестах).
from agents.data_agent import get_ticker_24h
from agents.journal_agent import add_trade as journal_add_trade
from agents.paper_agent import (
    add_limit_order as paper_add_limit,
    cancel_limit_order as paper_cancel_limit,
    open_limit_orders as paper_open_limit_orders,
    open_position as paper_open,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB = _PROJECT_ROOT / "data" / "journal.db"
_db_path: Path = _DEFAULT_DB

TESTNET_BASE = "https://testnet.binance.vision"
_RUNTIME_MODES = ("paper", "testnet")  # через команду доступны только эти
_PER_TRADE_RR_MIN = 1.5

# Ожидающие подтверждения live-ордера (в памяти процесса).
_pending: dict[str, dict] = {}
_PENDING_TTL = 300  # секунд


class BrokerError(Exception):
    """Понятная наружу ошибка брокера."""


def set_db_path(path) -> None:
    global _db_path
    _db_path = Path(path)


def _connect() -> sqlite3.Connection:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_db_path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS broker_state (key TEXT PRIMARY KEY, value TEXT)"
    )
    return con


def _get_state(key: str, default: str | None = None) -> str | None:
    with _connect() as con:
        row = con.execute(
            "SELECT value FROM broker_state WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else default


def _set_state(key: str, value: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO broker_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------- режимы

def _live_enabled() -> bool:
    """live активен только если в окружении явно BROKER_MODE=live."""
    return os.environ.get("BROKER_MODE", "paper").strip().lower() == "live"


def current_mode() -> str:
    if _live_enabled():
        return "live"
    stored = _get_state("mode", "paper")
    return stored if stored in _RUNTIME_MODES else "paper"


def set_mode(mode: str) -> str:
    """Переключить режим. live командой недоступен — только через .env."""
    m = str(mode).strip().lower()
    if m == "live":
        raise BrokerError("Режим live включается только строкой BROKER_MODE=live в .env.")
    if m not in _RUNTIME_MODES:
        raise BrokerError("Доступные режимы: paper, testnet (live — через .env).")
    _set_state("mode", m)
    return m


# ---------------------------------------------------------------- killswitch

def engage_killswitch() -> None:
    _set_state("killswitch", "on")


def is_killed() -> bool:
    return _get_state("killswitch", "off") == "on"


def reset_killswitch_if_env() -> bool:
    """Снять killswitch, если в .env явно задано BROKER_KILLSWITCH_RESET=1.

    Вызывается при старте бота. Возвращает True, если killswitch был снят.
    """
    if os.environ.get("BROKER_KILLSWITCH_RESET", "").strip() in ("1", "yes", "true"):
        if is_killed():
            _set_state("killswitch", "off")
            return True
    return False


# ---------------------------------------------------------------- дневной лимит

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record_live_loss(user_id: int, amount_usdt: float) -> None:
    """Учесть реализованный убыток за день (для дневного лимита live)."""
    key = f"loss:{int(user_id)}:{_today()}"
    cur = float(_get_state(key, "0") or "0")
    _set_state(key, str(cur + max(0.0, float(amount_usdt))))


def _daily_loss_exceeded(user_id: int) -> bool:
    limit = float(os.environ.get("BROKER_MAX_DAILY_LOSS_USDT", "0") or "0")
    if limit <= 0:
        return False  # лимит выключен, если не задан
    loss = float(_get_state(f"loss:{int(user_id)}:{_today()}", "0") or "0")
    return loss >= limit


# ---------------------------------------------------------------- ключи/подпись

def _keys_present() -> bool:
    return bool(os.environ.get("BINANCE_API_KEY")) and bool(
        os.environ.get("BINANCE_API_SECRET")
    )


def _sign(query: str, secret: str) -> str:
    """HMAC SHA256 подпись строки запроса (как требует Binance)."""
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def _base_url(mode: str) -> str:
    if mode == "testnet":
        return TESTNET_BASE
    # live: биржа настраивается (US: Binance.US или другая), по умолчанию — глобальная.
    return os.environ.get("BROKER_BASE_URL", "https://api.binance.com").rstrip("/")


async def _place_signed(mode: str, symbol: str, side: str, qty: float) -> dict:
    """Реальный подписанный маркет-ордер (testnet/live). Ключи только из env."""
    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_API_SECRET", "")
    ts = int(time.time() * 1000)
    query = (
        f"symbol={symbol.upper()}&side={side.upper()}&type=MARKET"
        f"&quantity={qty}&timestamp={ts}"
    )
    signature = _sign(query, secret)
    url = f"{_base_url(mode)}/api/v3/order?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": key}  # ключ только в заголовке, не логируем
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    msg = data.get("msg") if isinstance(data, dict) else None
                    raise BrokerError(msg or f"Биржа вернула статус {resp.status}.")
                return data
    except aiohttp.ClientError as exc:
        raise BrokerError("Сеть биржи недоступна.") from exc


def _fill_price(order: dict, fallback: float) -> float:
    try:
        fills = order.get("fills") or []
        if fills:
            return float(fills[0].get("price") or fallback)
        q = float(order.get("cummulativeQuoteQty") or 0)
        e = float(order.get("executedQty") or 0)
        if e > 0:
            return q / e
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return fallback


# ---------------------------------------------------------------- риск-гейт

def _risk_gate(side: str, entry: float, stop, take) -> tuple[bool, str]:
    if stop is None:
        return False, "стоп обязателен"
    if side == "buy" and not stop < entry:
        return False, "для покупки стоп должен быть ниже цены"
    if side == "sell" and not stop > entry:
        return False, "для продажи стоп должен быть выше цены"
    if take is not None:
        risk = abs(entry - stop)
        if risk > 0:
            rr = abs(take - entry) / risk
            if rr < _PER_TRADE_RR_MIN:
                return False, f"R:R {rr:.2f} < {_PER_TRADE_RR_MIN}"
    return True, "ok"


# ---------------------------------------------------------------- статус

def status() -> dict:
    """Текущий режим и статус (без ключей — только факт их наличия)."""
    mode = current_mode()
    return {
        "mode": mode,
        "killswitch": is_killed(),
        "keys_present": _keys_present(),  # bool, НЕ сами ключи
        "base_url": _base_url(mode) if mode in ("testnet", "live") else "—",
        "live_enabled_env": _live_enabled(),
    }


# ---------------------------------------------------------------- исполнение

async def _price(symbol: str) -> float:
    ticker = await get_ticker_24h(symbol)
    return float(ticker["lastPrice"])


async def place_order(
    user_id: int,
    symbol: str,
    side: str,
    qty: float,
    stop: float | None = None,
    take: float | None = None,
) -> dict:
    """Подготовить/исполнить ордер по текущему режиму.

    paper → виртуально (как раньше); testnet → подписанный ордер (фейк-деньги);
    live → требует риск-гейт PASS + подтверждение тапом (возвращает needs_confirmation).
    """
    symbol = symbol.upper().strip()
    side = side.strip().lower()
    if side not in ("buy", "sell"):
        raise BrokerError("Сторона должна быть buy или sell.")
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        raise BrokerError("Количество должно быть числом.")
    if qty <= 0:
        raise BrokerError("Количество должно быть больше нуля.")

    mode = current_mode()

    # --- paper: виртуальный портфель, без изменений ---
    if mode == "paper":
        price = await _price(symbol)
        pos_side = "long" if side == "buy" else "short"
        pos = paper_open(user_id, symbol, pos_side, qty, price, stop, take)
        return {"status": "executed", "mode": "paper", "symbol": symbol,
                "side": side, "qty": qty, "price": price, "position": pos}

    # --- testnet / live ---
    if is_killed():
        return {"status": "blocked",
                "reason": "killswitch активен — разблокировка вручную в .env "
                          "(BROKER_KILLSWITCH_RESET=1 и перезапуск)."}
    if not _keys_present():
        return {"status": "blocked",
                "reason": "нет ключей в .env (BINANCE_API_KEY/BINANCE_API_SECRET)."}

    if mode == "testnet":
        price = await _price(symbol)
        order = await _place_signed("testnet", symbol, side, qty)
        fill = _fill_price(order, price)
        _journal_executed(user_id, symbol, side, fill, stop, take, qty, "testnet")
        return {"status": "executed", "mode": "testnet", "symbol": symbol,
                "side": side, "qty": qty, "price": fill}

    # mode == live
    if _daily_loss_exceeded(user_id):
        return {"status": "blocked",
                "reason": "превышен дневной лимит убытка — live заблокирован до завтра."}
    price = await _price(symbol)
    ok, reason = _risk_gate(side, price, stop, take)
    if not ok:
        return {"status": "blocked", "reason": f"риск-гейт BLOCK: {reason}."}

    # Подтверждение тапом обязательно — создаём ожидающий ордер.
    token = secrets.token_urlsafe(8)
    _pending[token] = {
        "kind": "market",
        "user_id": int(user_id), "symbol": symbol, "side": side, "qty": qty,
        "stop": stop, "take": take, "price": price, "ts": time.time(),
    }
    return {
        "status": "needs_confirmation",
        "mode": "live",
        "token": token,
        "symbol": symbol, "side": side, "qty": qty, "price": price,
        "warning": ("ВНИМАНИЕ: реальный ордер на бирже (live). "
                    "Деньги настоящие. Без подтверждения ордер не уйдёт."),
    }


async def confirm_order(user_id: int, token: str) -> dict:
    """Исполнить ранее подготовленный live-ордер по токену подтверждения."""
    pend = _pending.get(token)
    if pend is None or pend["user_id"] != int(user_id):
        return {"status": "blocked", "reason": "подтверждение не найдено или устарело."}
    if time.time() - pend["ts"] > _PENDING_TTL:
        _pending.pop(token, None)
        return {"status": "blocked", "reason": "подтверждение истекло, повтори /buy."}

    # Повторные проверки безопасности непосредственно перед отправкой.
    if not _live_enabled():
        _pending.pop(token, None)
        return {"status": "blocked", "reason": "live выключен (BROKER_MODE не live)."}
    if is_killed():
        _pending.pop(token, None)
        return {"status": "blocked", "reason": "killswitch активен."}

    _pending.pop(token, None)

    if pend.get("kind") == "limit":
        order = await _place_limit_signed(
            "live", pend["symbol"], pend["side"], pend["qty"], pend["limit_price"]
        )
        order_id = order.get("orderId") if isinstance(order, dict) else None
        _journal_executed(user_id, pend["symbol"], pend["side"], pend["limit_price"],
                          pend["stop"], pend["take"], pend["qty"], "live")
        return {"status": "executed", "mode": "live", "order_type": "limit",
                "symbol": pend["symbol"], "side": pend["side"], "qty": pend["qty"],
                "limit_price": pend["limit_price"], "orderId": order_id}

    order = await _place_signed("live", pend["symbol"], pend["side"], pend["qty"])
    fill = _fill_price(order, pend["price"])
    _journal_executed(user_id, pend["symbol"], pend["side"], fill,
                      pend["stop"], pend["take"], pend["qty"], "live")
    return {"status": "executed", "mode": "live", "order_type": "market",
            "symbol": pend["symbol"], "side": pend["side"], "qty": pend["qty"],
            "price": fill}


async def _signed_request(method: str, mode: str, path: str, params: str) -> dict:
    """Подписанный REST-запрос к бирже (GET/POST/DELETE). Ключи только из env."""
    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_API_SECRET", "")
    ts = int(time.time() * 1000)
    query = f"{params}&timestamp={ts}" if params else f"timestamp={ts}"
    signature = _sign(query, secret)
    url = f"{_base_url(mode)}{path}?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": key}
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    msg = data.get("msg") if isinstance(data, dict) else None
                    raise BrokerError(msg or f"Биржа вернула статус {resp.status}.")
                return data
    except aiohttp.ClientError as exc:
        raise BrokerError("Сеть биржи недоступна.") from exc


async def _place_limit_signed(mode, symbol, side, qty, price) -> dict:
    params = (
        f"symbol={symbol.upper()}&side={side.upper()}&type=LIMIT"
        f"&timeInForce=GTC&quantity={qty}&price={price}"
    )
    return await _signed_request("POST", mode, "/api/v3/order", params)


async def _get_lot_size(mode: str, symbol: str) -> tuple[float, float]:
    """(minQty, stepSize) из exchangeInfo. Нет данных — честная ошибка."""
    base = _base_url(mode)
    url = f"{base}/api/v3/exchangeInfo?symbol={symbol.upper()}"
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise BrokerError("Не удалось получить параметры пары (LOT_SIZE).")
                data = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise BrokerError("Не удалось получить параметры пары (LOT_SIZE).") from exc
    for sym in data.get("symbols", []):
        if sym.get("symbol") == symbol.upper():
            for f in sym.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    return float(f["minQty"]), float(f["stepSize"])
    raise BrokerError("Параметры пары (LOT_SIZE) не найдены — ордер не выставлен.")


async def _validate_lot(mode: str, symbol: str, qty: float) -> None:
    min_qty, step = await _get_lot_size(mode, symbol)
    if qty < min_qty:
        raise BrokerError(f"Количество меньше минимального {min_qty}.")
    if step > 0:
        steps = round((qty - min_qty) / step)
        if abs(min_qty + steps * step - qty) > step * 1e-6:
            raise BrokerError(f"Количество не кратно шагу {step}.")


async def place_limit_order(
    user_id: int,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    stop: float | None = None,
    take: float | None = None,
) -> dict:
    """Выставить лимитный ордер по текущему режиму (type=LIMIT, GTC)."""
    symbol = symbol.upper().strip()
    side = side.strip().lower()
    if side not in ("buy", "sell"):
        raise BrokerError("Сторона должна быть buy или sell.")
    try:
        qty = float(qty)
        price = float(price)
    except (TypeError, ValueError):
        raise BrokerError("Количество и цена должны быть числами.")
    if qty <= 0 or price <= 0:
        raise BrokerError("Количество и цена должны быть больше нуля.")

    mode = current_mode()

    if mode == "paper":
        order = paper_add_limit(user_id, symbol, side, qty, price, stop, take)
        return {"status": "placed", "mode": "paper", "order_type": "limit",
                "symbol": symbol, "side": side, "qty": qty, "limit_price": price,
                "amount": qty * price, "orderId": order["orderId"]}

    # testnet / live
    if is_killed():
        return {"status": "blocked",
                "reason": "killswitch активен — разблокировка вручную в .env."}
    if not _keys_present():
        return {"status": "blocked",
                "reason": "нет ключей в .env (BINANCE_API_KEY/BINANCE_API_SECRET)."}
    await _validate_lot(mode, symbol, qty)

    if mode == "testnet":
        order = await _place_limit_signed("testnet", symbol, side, qty, price)
        _journal_executed(user_id, symbol, side, price, stop, take, qty, "testnet")
        return {"status": "placed", "mode": "testnet", "order_type": "limit",
                "symbol": symbol, "side": side, "qty": qty, "limit_price": price,
                "amount": qty * price, "orderId": order.get("orderId")}

    # live
    if _daily_loss_exceeded(user_id):
        return {"status": "blocked",
                "reason": "превышен дневной лимит убытка — live заблокирован до завтра."}
    ok, reason = _risk_gate(side, price, stop, take)
    if not ok:
        return {"status": "blocked", "reason": f"риск-гейт BLOCK: {reason}."}

    token = secrets.token_urlsafe(8)
    _pending[token] = {
        "kind": "limit",
        "user_id": int(user_id), "symbol": symbol, "side": side, "qty": qty,
        "limit_price": price, "stop": stop, "take": take, "ts": time.time(),
    }
    return {
        "status": "needs_confirmation", "mode": "live", "order_type": "limit",
        "token": token, "symbol": symbol, "side": side, "qty": qty,
        "price": price, "amount": qty * price,
        "warning": ("ВНИМАНИЕ: реальный лимитный ордер на бирже (live). "
                    "Деньги настоящие. Без подтверждения ордер не выставляется."),
    }


async def list_orders(user_id: int) -> dict:
    """Открытые лимитные ордера по текущему режиму."""
    mode = current_mode()
    if mode == "paper":
        rows = paper_open_limit_orders(user_id)
        orders = [{"orderId": r["id"], "symbol": r["symbol"], "side": r["side"],
                   "qty": r["qty"], "price": r["limit_price"]} for r in rows]
        return {"mode": "paper", "orders": orders}
    if is_killed():
        raise BrokerError("killswitch активен — биржевые режимы заблокированы.")
    if not _keys_present():
        raise BrokerError("нет ключей в .env.")
    data = await _signed_request("GET", mode, "/api/v3/openOrders", "")
    orders = [{"orderId": o.get("orderId"), "symbol": o.get("symbol"),
               "side": str(o.get("side", "")).lower(), "qty": o.get("origQty"),
               "price": o.get("price")} for o in (data or [])]
    return {"mode": mode, "orders": orders}


async def cancel_order(user_id: int, order_id, symbol: str | None = None) -> dict:
    """Отменить лимитный ордер по текущему режиму."""
    mode = current_mode()
    if mode == "paper":
        paper_cancel_limit(user_id, int(order_id))
        return {"status": "cancelled", "mode": "paper", "orderId": int(order_id)}
    if is_killed():
        raise BrokerError("killswitch активен — биржевые режимы заблокированы.")
    if not _keys_present():
        raise BrokerError("нет ключей в .env.")
    if not symbol:
        raise BrokerError("Для отмены на бирже укажи пару: /cancelorder <id> <ПАРА>.")
    params = f"symbol={symbol.upper()}&orderId={int(order_id)}"
    await _signed_request("DELETE", mode, "/api/v3/order", params)
    return {"status": "cancelled", "mode": mode, "orderId": int(order_id)}


def _journal_executed(user_id, symbol, side, price, stop, take, qty, source) -> None:
    """Записать исполненный ордер в журнал с пометкой источника."""
    pos_side = "long" if side == "buy" else "short"
    safe_stop = stop if stop is not None else price * (0.99 if pos_side == "long" else 1.01)
    try:
        journal_add_trade(user_id, symbol, pos_side, price, safe_stop, take, qty,
                          source=source)
    except Exception:  # noqa: BLE001 — запись в журнал не должна ронять ответ
        pass
