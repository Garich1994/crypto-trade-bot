"""Data Agent — сбор рыночных данных с публичного API Binance (без ключей).

Кэш в памяти с TTL 60 секунд, таймаут и 1 повтор при сетевой ошибке.
Наружу отдаётся понятное исключение DataAgentError, без трейсбека.

Для рыночных данных используется публичный mirror data-api.binance.vision
(тот же формат, без ключей, доступен из регионов, где основной api.binance.com
закрыт по гео-ограничению 451). Основной хост остаётся запасным.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Sequence

import aiohttp

from core.config import HTTP_TIMEOUT

# Хосты спотовых рыночных данных (пробуются по порядку).
SPOT_HOSTS = (
    "https://data-api.binance.vision",  # публичный mirror рыночных данных
    "https://api.binance.com",          # основной API (может быть закрыт по региону)
)
# Хосты фьючерсов USDT-M (для funding rate).
FUTURES_HOSTS = ("https://fapi.binance.com",)

#: Разрешённые таймфреймы свечей.
ALLOWED_INTERVALS = ("15m", "1h", "4h", "1d")

_CACHE_TTL = 60  # секунд
_RETRIES = 1     # один повтор после первой сетевой неудачи

# Кэш в памяти: ключ -> (expires_at_monotonic, data). Ключ не зависит от хоста.
_cache: dict[tuple, tuple[float, Any]] = {}


class DataAgentError(Exception):
    """Понятная наружу ошибка сбора данных (без трейсбека для пользователя)."""


def _cache_key(path: str, params: dict[str, Any]) -> tuple:
    return (path, tuple(sorted(params.items())))


def _extract_msg(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("msg", "")).strip()
    return ""


async def _request(hosts: Sequence[str], path: str, params: dict[str, Any]) -> Any:
    """GET с кэшем (TTL 60с), таймаутом, одним повтором и перебором хостов.

    - 200 -> данные кэшируются и возвращаются.
    - 400 -> немедленно DataAgentError (напр. неизвестный тикер), без перебора.
    - иной статус (451/403/429/5xx) или сетевая ошибка -> пробуем следующий хост.
    """
    key = _cache_key(path, params)
    now = time.monotonic()

    cached = _cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    last_error: DataAgentError | None = None

    for base in hosts:
        url = f"{base}{path}"
        for attempt in range(_RETRIES + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, params=params) as resp:
                        body = await resp.json(content_type=None)
                        if resp.status == 200:
                            _cache[key] = (now + _CACHE_TTL, body)
                            return body
                        msg = _extract_msg(body)
                        if resp.status == 400:
                            # Ошибка запроса (неизвестный тикер и т.п.) — перебор не поможет.
                            raise DataAgentError(
                                msg or "Неверный запрос (возможно, неизвестный тикер)."
                            )
                        # Гео-ограничение / лимит / сбой — пробуем следующий хост.
                        last_error = DataAgentError(
                            msg or f"Binance вернул статус {resp.status}."
                        )
                        break
            except DataAgentError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = DataAgentError(
                    "Не удалось получить данные от Binance (сеть недоступна или таймаут)."
                )
                last_error.__cause__ = exc
                if attempt < _RETRIES:
                    await asyncio.sleep(1)
                    continue
                break  # повторы исчерпаны — к следующему хосту

    raise last_error or DataAgentError("Не удалось получить данные от Binance.")


async def get_klines(symbol: str, interval: str, limit: int = 300) -> list[list]:
    """Свечи OHLCV. Возвращает список массивов в формате Binance."""
    interval = interval.lower()
    if interval not in ALLOWED_INTERVALS:
        raise DataAgentError(
            f"Неподдерживаемый таймфрейм: {interval}. "
            f"Доступны: {', '.join(ALLOWED_INTERVALS)}."
        )
    params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
    data = await _request(SPOT_HOSTS, "/api/v3/klines", params)
    if not isinstance(data, list) or not data:
        raise DataAgentError("Binance вернул пустой набор свечей.")
    return data


async def get_klines_history(
    symbol: str,
    interval: str,
    total: int,
    batch: int = 1000,
) -> list[list]:
    """Исторические свечи глубиной до `total` штук с пагинацией.

    Binance отдаёт максимум 1000 свечей за запрос, поэтому для большей глубины
    делаем несколько запросов назад по времени (через endTime). Возвращает список
    в хронологическом порядке (от старых к новым), не длиннее `total`.
    """
    interval = interval.lower()
    if interval not in ALLOWED_INTERVALS:
        raise DataAgentError(
            f"Неподдерживаемый таймфрейм: {interval}. "
            f"Доступны: {', '.join(ALLOWED_INTERVALS)}."
        )
    symbol = symbol.upper()
    per_req = max(1, min(batch, 1000))

    collected: list[list] = []
    end_time: int | None = None

    while len(collected) < total:
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": per_req,
        }
        if end_time is not None:
            params["endTime"] = end_time

        data = await _request(SPOT_HOSTS, "/api/v3/klines", params)
        if not isinstance(data, list) or not data:
            break

        collected = data + collected  # более старые свечи идут в начало
        earliest_open = int(data[0][0])
        next_end = earliest_open - 1
        if end_time is not None and next_end >= end_time:
            break  # нет прогресса — выходим, чтобы не зациклиться
        end_time = next_end
        if len(data) < per_req:
            break  # история закончилась

    if not collected:
        raise DataAgentError("Binance вернул пустой набор свечей.")
    return collected[-total:]


async def get_ticker_24h(symbol: str) -> dict:
    """24-часовая статистика по паре."""
    params = {"symbol": symbol.upper()}
    data = await _request(SPOT_HOSTS, "/api/v3/ticker/24hr", params)
    if not isinstance(data, dict):
        raise DataAgentError("Неожиданный ответ тикера от Binance.")
    return data


async def get_all_tickers_24h() -> list[dict]:
    """24-часовая статистика по всем парам (для выбора вселенной сканером)."""
    data = await _request(SPOT_HOSTS, "/api/v3/ticker/24hr", {})
    if not isinstance(data, list):
        raise DataAgentError("Неожиданный ответ списка тикеров от Binance.")
    return data


async def get_funding_rate(symbol: str) -> float | None:
    """Текущий funding rate (фьючерсы USDT-M), в долях.

    Если фьючерсов для пары нет или рынок недоступен — возвращает None (не ошибка).
    """
    params = {"symbol": symbol.upper()}
    try:
        data = await _request(FUTURES_HOSTS, "/fapi/v1/premiumIndex", params)
    except DataAgentError:
        return None
    if not isinstance(data, dict) or "lastFundingRate" not in data:
        return None
    try:
        return float(data["lastFundingRate"])
    except (TypeError, ValueError):
        return None
