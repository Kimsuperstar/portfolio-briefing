from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import yfinance as yf

from .config import KST

def parse_published_kst(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).astimezone(KST)
        except Exception:
            return None

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KST)
        except ValueError:
            return None

    return None


def parse_news_item(raw: dict[str, Any], ticker: str) -> dict[str, Any]:
    content = raw.get("content") if isinstance(raw.get("content"), dict) else raw

    title = content.get("title") or raw.get("title") or ""

    provider_obj = content.get("provider")
    provider = (
        provider_obj.get("displayName")
        if isinstance(provider_obj, dict)
        else content.get("publisher") or raw.get("publisher") or ""
    )

    url_obj = (
        content.get("canonicalUrl")
        or content.get("clickThroughUrl")
        or raw.get("link")
    )
    url = url_obj.get("url") if isinstance(url_obj, dict) else (url_obj or "")

    published_raw = (
        content.get("pubDate")
        or content.get("displayTime")
        or raw.get("providerPublishTime")
    )
    published = parse_published_kst(published_raw)

    return {
        "ticker": ticker,
        "title": title,
        "provider": provider,
        "published_kst": published,
        "url": url,
    }


def latest_news_for_ticker(
    ticker: str,
    target_date: date,
    limit: int,
) -> dict[str, Any]:
    raw_news = yf.Ticker(ticker).news or []

    items = []
    seen = set()
    for raw in raw_news:
        item = parse_news_item(raw, ticker)
        if not item["published_kst"]:
            continue
        key = (item["title"], item["url"])
        if key in seen:
            continue
        seen.add(key)
        items.append(item)

    items.sort(key=lambda x: x["published_kst"], reverse=True)

    today_items = [
        x for x in items
        if x["published_kst"].date() == target_date
    ]

    used_date: date | None = target_date if today_items else None
    selected = today_items

    # 오늘자 없으면 실제 최신 발행일로 fallback
    if not selected and items:
        used_date = items[0]["published_kst"].date()
        selected = [
            x for x in items
            if x["published_kst"].date() == used_date
        ]

    output = []
    for item in selected[:limit]:
        output.append(
            {
                "ticker": ticker,
                "title": item["title"],
                "provider": item["provider"],
                "published_kst": item["published_kst"].strftime("%Y-%m-%d %H:%M:%S KST"),
                "url": item["url"],
            }
        )

    return {
        "ticker": ticker,
        "requested_kst_date": target_date.isoformat(),
        "used_news_date": used_date.isoformat() if used_date else "",
        "fallback_to_latest_date": bool(used_date and used_date != target_date),
        "items": output,
    }


