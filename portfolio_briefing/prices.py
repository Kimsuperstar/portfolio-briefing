from __future__ import annotations

import pandas as pd
import yfinance as yf

from .models import PriceInfo
from .utils import looks_like_equity_ticker, yahoo_symbol

def get_regular_close(ticker: str) -> PriceInfo:
    """
    정규장 일봉 Close를 기준으로 사용한다.
    fast_info/currentPrice를 종가로 사용하지 않는다.
    """
    hist = yf.Ticker(ticker).history(
        period="15d",
        interval="1d",
        auto_adjust=False,
        actions=True,
        prepost=False,
    )
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 2:
        return PriceInfo(ticker, "", None, None, None, 0.0, None)

    latest = hist.iloc[-1]
    previous = hist.iloc[-2]

    close = float(latest["Close"])
    previous_close = float(previous["Close"])
    price_return_pct = (close / previous_close - 1.0) * 100.0

    dividend = 0.0
    if "Dividends" in hist.columns:
        try:
            dividend = float(latest.get("Dividends", 0.0) or 0.0)
        except Exception:
            dividend = 0.0

    # 배당락일 확인용 총수익 근사치
    total_return_pct = ((close + dividend) / previous_close - 1.0) * 100.0

    return PriceInfo(
        ticker=ticker,
        close_date=str(hist.index[-1].date()),
        close=round(close, 6),
        previous_close=round(previous_close, 6),
        price_return_pct=round(price_return_pct, 6),
        dividend=round(dividend, 6),
        total_return_pct=round(total_return_pct, 6),
    )


def bulk_daily_returns(symbols: list[str]) -> dict[str, float | None]:
    """
    구성종목을 개별 호출하지 않고 batch 다운로드해 Yahoo rate-limit을 줄인다.
    반환값은 % 단위 수익률.
    """
    cleaned = sorted({yahoo_symbol(s) for s in symbols if looks_like_equity_ticker(s)})
    result: dict[str, float | None] = {s: None for s in cleaned}
    if not cleaned:
        return result

    batch_size = 100

    for start in range(0, len(cleaned), batch_size):
        batch = cleaned[start : start + batch_size]
        try:
            data = yf.download(
                tickers=batch,
                period="10d",
                interval="1d",
                auto_adjust=False,
                actions=False,
                prepost=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )
        except Exception:
            continue

        for symbol in batch:
            try:
                if len(batch) == 1 and not isinstance(data.columns, pd.MultiIndex):
                    close = data["Close"].dropna()
                else:
                    close = data[symbol]["Close"].dropna()

                if len(close) >= 2:
                    result[symbol] = float((close.iloc[-1] / close.iloc[-2] - 1.0) * 100.0)
            except Exception:
                result[symbol] = None

    return result


