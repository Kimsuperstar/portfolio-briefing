from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import ETF_CONFIG
from .models import HoldingsResult, PriceInfo
from .prices import bulk_daily_returns
from .utils import yahoo_symbol

def parse_data_date(value: str):
    """
    holdings/가격 날짜 문자열을 date 객체로 변환한다.

    지원:
    2026-08-21
    08/21/2026
    """

    if not value:
        return None

    text = str(value).strip()

    if text.lower() in {
        "unknown",
        "none",
        "nan",
        "",
    }:
        return None

    formats = [
        "%Y-%m-%d",
        "%m/%d/%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(
                text,
                fmt,
            ).date()

        except ValueError:
            continue

    return None

def build_attribution(
    etf: str,
    price_info: PriceInfo,
    holdings_result: HoldingsResult,
    top_n: int,
) -> dict[str, Any]:

    # ------------------------------------------------
    # Holdings 날짜 검증
    # ------------------------------------------------

    price_date = parse_data_date(
        price_info.close_date
    )

    holdings_date = parse_data_date(
        holdings_result.as_of
    )

    future_holdings = False

    if (
        price_date is not None
        and holdings_date is not None
        and holdings_date > price_date
    ):
        future_holdings = True

        print(
            f"[{etf}] WARNING: "
            f"holdings date {holdings_result.as_of} "
            f"is later than price date "
            f"{price_info.close_date}. "
            f"Attribution disabled."
        )

    # 미래 holdings면 기여도 계산에 사용하지 않는다.
    if future_holdings:
        usable_holdings = []
    else:
        usable_holdings = (
            holdings_result.holdings
        )

    returns = bulk_daily_returns(
        [
            h.symbol
            for h in usable_holdings
        ]
    )

    rows = []
    observed_weight = 0.0
    contribution_sum_pp = 0.0

    for h in usable_holdings:
        symbol = yahoo_symbol(h.symbol)
        ret = returns.get(symbol)
        contribution = None

        if ret is not None:
            contribution = h.weight * ret  # weight(0~1) * 수익률(%), 결과는 %p
            observed_weight += h.weight
            contribution_sum_pp += contribution

        rows.append(
            {
                "symbol": symbol,
                "name": h.name,
                "weight_pct": round(h.weight * 100.0, 6),
                "stock_return_pct": None if ret is None else round(ret, 6),
                "contribution_pp": None if contribution is None else round(contribution, 6),
            }
        )

    calculable = [r for r in rows if r["contribution_pp"] is not None]
    positives = sorted(calculable, key=lambda x: x["contribution_pp"], reverse=True)
    negatives = sorted(calculable, key=lambda x: x["contribution_pp"])

    etf_return = price_info.price_return_pct
    unexplained = (
        None
        if etf_return is None
        else etf_return - contribution_sum_pp
    )

    coverage_pct = observed_weight * 100.0

    derivative_sensitive = bool(ETF_CONFIG[etf]["derivative_sensitive"])

    notes = []

    if future_holdings:
        notes.append(
            f"Holdings 기준일 {holdings_result.as_of}이 "
            f"가격 분석일 {price_info.close_date}보다 미래이므로 "
            f"look-ahead bias 방지를 위해 종목 기여도 계산에서 제외했습니다."
        )

    if not holdings_result.is_full_holdings:
        notes.append(
            "전체 holdings가 아니라 일부/top holdings일 수 있어 잔차에는 누락 종목 효과가 포함됩니다."
        )
    if not holdings_result.is_full_holdings:
        notes.append(
            "전체 holdings가 아니라 일부/top holdings일 수 있어 잔차에는 누락 종목 효과가 포함됩니다."
        )
    if derivative_sensitive:
        notes.append(
            "옵션/ELN 전략 ETF이므로 잔차에는 파생상품 효과가 포함될 수 있습니다."
        )
    if price_info.dividend > 0:
        notes.append(
            f"해당 거래일 배당/분배금 {price_info.dividend:.4f}이 확인되어 가격수익률과 총수익률이 다릅니다."
        )
    notes.append(
        "시장가격 수익률과 NAV 수익률은 premium/discount 때문에 다를 수 있습니다."
    )

    quality = "HIGH"

    if derivative_sensitive:

        if (
            holdings_result.is_full_holdings
            and coverage_pct >= 60
        ):
            quality = "MEDIUM"

        else:
            quality = "PARTIAL"

    else:

        if (
            not holdings_result.is_full_holdings
            or coverage_pct < 90
        ):
            quality = "PARTIAL"


    # 미래 holdings는 무조건 분석 품질 PARTIAL
    if future_holdings:
        quality = "PARTIAL"

    return {
        "etf": etf,
        "strategy": ETF_CONFIG[etf]["strategy_note"],
        "price_return_pct": etf_return,
        "total_return_pct_including_distribution": price_info.total_return_pct,
        "holdings_source": holdings_result.source,
        "holdings_source_url": holdings_result.source_url,
        "holdings_as_of": holdings_result.as_of,
        "is_full_holdings": holdings_result.is_full_holdings,
        "calculable_weight_coverage_pct": round(coverage_pct, 4),
        "stock_contribution_sum_pp": round(contribution_sum_pp, 6),
        "unexplained_residual_pp": None if unexplained is None else round(unexplained, 6),
        "quality": quality,
        "warnings": [x for x in [holdings_result.warning, *notes] if x],
        "top_positive": positives[:top_n],
        "top_negative": negatives[:top_n],
    }


