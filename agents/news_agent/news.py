"""News Monitor Agent — свежие крипто-новости из публичных RSS (без ключей).

Данные не выдумываются: если лента недоступна, её новости просто не попадут
в результат. Сбой одной ленты не ломает другую.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import feedparser

from core.config import HTTP_TIMEOUT

logger = logging.getLogger(__name__)

# (название источника, URL RSS-ленты)
FEEDS: tuple[tuple[str, str], ...] = (
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
)

_FRESH_WINDOW = timedelta(hours=24)
_PER_FEED = 15
_USER_AGENT = "crypto-trade-bot/1.0 (+rss reader)"


async def _fetch_raw(url: str) -> bytes | None:
    """Скачать содержимое ленты. При любой ошибке — None (не падаем)."""
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    headers = {"User-Agent": _USER_AGENT}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("RSS %s вернул статус %s", url, resp.status)
                    return None
                return await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("RSS %s недоступен: %s", url, exc)
        return None


def _entry_time(entry) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_feed(source: str, raw: bytes, now: datetime) -> list[dict]:
    """Разобрать ленту и оставить свежие (<24ч) новости."""
    parsed = feedparser.parse(raw)
    items: list[dict] = []
    for entry in parsed.entries[:_PER_FEED]:
        published = _entry_time(entry)
        if published is None or now - published > _FRESH_WINDOW:
            continue
        title = (getattr(entry, "title", "") or "").strip()
        url = (getattr(entry, "link", "") or "").strip()
        if not title or not url:
            continue
        items.append(
            {"title": title, "source": source, "time": published, "url": url}
        )
    return items


async def get_crypto_news(limit: int = 5) -> list[dict]:
    """Вернуть до `limit` свежих новостей (новые сверху).

    Пустой список — допустимый результат (нет свежих или ленты недоступны).
    """
    now = datetime.now(timezone.utc)
    raws = await asyncio.gather(*(_fetch_raw(url) for _, url in FEEDS))

    collected: list[dict] = []
    for (source, _), raw in zip(FEEDS, raws):
        if not raw:
            continue
        try:
            collected.extend(await asyncio.to_thread(_parse_feed, source, raw, now))
        except Exception:  # noqa: BLE001 — парсер ленты не должен ронять агента
            logger.exception("Не удалось разобрать ленту %s", source)

    collected.sort(key=lambda it: it["time"], reverse=True)
    return collected[: max(0, limit)]
