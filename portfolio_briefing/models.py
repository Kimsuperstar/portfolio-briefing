from __future__ import annotations

from dataclasses import dataclass

@dataclass
class PriceInfo:
    ticker: str
    close_date: str
    close: float | None
    previous_close: float | None
    price_return_pct: float | None
    dividend: float
    total_return_pct: float | None
    source: str = "yfinance history(interval=1d, auto_adjust=False, prepost=False)"


@dataclass
class Holding:
    symbol: str
    name: str
    weight: float  # 0.08 == 8%
    instrument_type: str = "equity"


@dataclass
class HoldingsResult:
    etf: str
    holdings: list[Holding]
    source: str
    source_url: str
    as_of: str
    is_full_holdings: bool
    warning: str = ""
