from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .attribution import build_attribution
from .config import ETFS, KST, PORTFOLIO
from .gemini import maybe_run_gemini
from .holdings import get_holdings
from .news import latest_news_for_ticker
from .prices import get_regular_close

def collect_payload(
    news_limit: int,
    top_contributors: int,
) -> dict[str, Any]:
    generated = datetime.now(KST)

    prices = {}
    for ticker in PORTFOLIO:
        try:
            prices[ticker] = asdict(get_regular_close(ticker))
        except Exception as exc:
            prices[ticker] = {
                "ticker": ticker,
                "error": str(exc),
            }

    attributions = {}
    top_news_symbols: list[str] = []

    for etf in ETFS:
        try:
            p = get_regular_close(etf)
            holdings = get_holdings(etf)
            attribution = build_attribution(
                etf,
                p,
                holdings,
                top_n=top_contributors,
            )
            attributions[etf] = attribution

            # 뉴스는 실제 기여도가 큰 종목 위주로 추가
            for row in attribution["top_positive"][:3] + attribution["top_negative"][:3]:
                symbol = row["symbol"]
                if symbol not in top_news_symbols:
                    top_news_symbols.append(symbol)

        except Exception as exc:
            attributions[etf] = {
                "etf": etf,
                "error": str(exc),
                "quality": "FAILED",
            }

    # 보유 종목 + ETF 기여 상위 구성종목 뉴스를 조회
    news_symbols = []
    for symbol in [*PORTFOLIO, *top_news_symbols]:
        if symbol not in news_symbols:
            news_symbols.append(symbol)

    target_date = generated.date()
    news = {}
    for symbol in news_symbols:
        try:
            news[symbol] = latest_news_for_ticker(
                symbol,
                target_date=target_date,
                limit=news_limit,
            )
        except Exception as exc:
            news[symbol] = {
                "ticker": symbol,
                "error": str(exc),
                "items": [],
            }

    return {
        "generated_kst": generated.strftime("%Y-%m-%d %H:%M:%S KST"),
        "portfolio": PORTFOLIO,
        "methodology": {
            "price": "yfinance regular-session daily Close, auto_adjust=False, prepost=False",
            "attribution_formula": "contribution_pp = holding_weight * stock_return_pct",
            "holdings_priority": "official issuer holdings -> yfinance top_holdings fallback",
            "news": "yfinance.Ticker.news, KST today first; if none use latest actual publish date",
        },
        "prices": prices,
        "etf_attribution": attributions,
        "news": news,
    }


def print_console_summary(payload: dict[str, Any]) -> None:
    print("# Portfolio Data Check")
    print(f"- Generated: {payload['generated_kst']}")

    print("\n## Prices")
    for ticker, p in payload["prices"].items():
        if "error" in p:
            print(f"- {ticker}: ERROR - {p['error']}")
            continue
        print(
            f"- {ticker}: ${p['close']:.2f} | "
            f"{p['price_return_pct']:+.2f}% | "
            f"{p['close_date']}"
        )

    print("\n## ETF Attribution Quality")
    for etf, a in payload["etf_attribution"].items():
        if "error" in a:
            print(f"- {etf}: FAILED - {a['error']}")
            continue
        print(
            f"- {etf}: {a['quality']} | "
            f"coverage={a['calculable_weight_coverage_pct']:.2f}% | "
            f"source={a['holdings_source']} | "
            f"as_of={a['holdings_as_of'] or 'unknown'}"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Build a data-grounded US portfolio briefing input."
    )
    parser.add_argument("--news-limit", type=int, default=3)
    parser.add_argument("--top-contributors", type=int, default=5)
    parser.add_argument("--output", default="briefing_input.json")
    parser.add_argument("--briefing-output", default="briefing.md")
    parser.add_argument("--gemini-model", default="gemini-2.5-flash")
    parser.add_argument(
        "--no-gemini",
        action="store_true",
        help="GEMINI_API_KEY가 있어도 Gemini 분석을 실행하지 않음.",
    )
    args = parser.parse_args(argv)

    payload = collect_payload(
        news_limit=args.news_limit,
        top_contributors=args.top_contributors,
    )

    output_path = Path(args.output)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print_console_summary(payload)
    print(f"\nSaved: {output_path}")

    if not args.no_gemini:
        try:
            briefing = maybe_run_gemini(payload, model=args.gemini_model)
            if briefing:
                briefing_path = Path(args.briefing_output)
                briefing_path.write_text(briefing, encoding="utf-8")
                print(f"Saved Gemini briefing: {briefing_path}")
            else:
                print(
                    "Gemini skipped: GEMINI_API_KEY 환경변수가 없습니다. "
                    "데이터 JSON 생성은 완료되었습니다."
                )
        except Exception as exc:
            print(f"Gemini error: {exc}", file=sys.stderr)
            return 2

    return 0

