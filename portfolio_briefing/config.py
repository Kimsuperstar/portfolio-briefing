from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

KST = ZoneInfo("Asia/Seoul")

PORTFOLIO = ["GOOGL", "JEPQ", "QQQI", "SPYM", "QQQM"]
ETFS = ["JEPQ", "QQQI", "SPYM", "QQQM"]

# 운용사 공식 자료
ETF_CONFIG: dict[str, dict[str, Any]] = {
    "QQQM": {
        "issuer": "Invesco",
        "official_page": "https://www.invesco.com/us/en/financial-products/etfs/invesco-nasdaq-100-etf.html",
        # legacy holdings 페이지도 시도한다. 사이트 구조 변경 시 실패할 수 있음.
        "holdings_page": "https://www.invesco.com/us/financial-products/etfs/holdings?audienceType=Investor&ticker=QQQM",
        "derivative_sensitive": False,
        "strategy_note": "Nasdaq-100 추종 패시브 ETF",
    },
    "SPYM": {
        "issuer": "State Street",
        "official_page": "https://www.ssga.com/us/en/individual/etfs/state-street-spdr-portfolio-sp-500-etf-spym",
        # State Street가 제공하는 공식 Daily Holdings XLSX
        "holdings_xlsx": "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spym.xlsx",
        "derivative_sensitive": False,
        "strategy_note": "S&P 500 추종 패시브 ETF",
    },
    "QQQI": {
        "issuer": "NEOS",
        "official_page": "https://neosfunds.com/qqqi/",
        "holdings_page": "https://neosfunds.com/qqqi/",
        "derivative_sensitive": True,
        "strategy_note": "Nasdaq-100 주식 + NDX 옵션 전략",
    },
    "JEPQ": {
        "issuer": "J.P. Morgan",

        "official_page": (
            "https://am.jpmorgan.com/us/en/asset-management/adv/"
            "products/jpmorgan-nasdaq-equity-premium-income-etf-"
            "etf-shares-46654q203"
        ),

        "holdings_page": (
            "https://am.jpmorgan.com/FundsMarketingHandler/pdf"
            "?country=us"
            "&cusip=46654Q203"
            "&locale=en-US"
            "&role=adv"
            "&type=dailyETFHoldings"
        ),

        "derivative_sensitive": True,

        "strategy_note": (
            "액티브 주식 포트폴리오 + ELN/옵션 전략"
        ),
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150 Safari/537.36"
    )
}
