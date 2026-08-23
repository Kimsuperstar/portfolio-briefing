#!/usr/bin/env python3
"""
portfolio_briefing_pipeline.py

목표
----
1) 미국 정규장 종가/등락률: yfinance history()의 일봉 Close 사용
2) ETF 구성비: 운용사 공식 자료 우선, 실패하면 yfinance top_holdings로 명시적 fallback
3) ETF 기여도: 구성비 * 구성종목 수익률
4) 옵션/ELN ETF(QQQI, JEPQ): 주식 기여도와 설명되지 않는 잔차를 분리
5) 뉴스: yfinance.Ticker.news -> KST 발행일 기준 오늘자, 없으면 최신 발행일로 fallback
6) Gemini 입력용 JSON 생성
7) GEMINI_API_KEY가 있으면 Gemini 브리핑 생성(선택)

주의
----
- "기여도"는 시장가격 수익률에 대한 근사치다.
- holdings 날짜와 수익률 날짜가 다르면 정확도가 낮아질 수 있다.
- QQQI/JEPQ는 옵션/ELN 등 파생상품이 있어 주식 기여도만으로 전체 수익률을 설명할 수 없다.
- 공식 전체 holdings를 못 구하고 top holdings만 쓴 경우 residual을 옵션 효과로 단정하지 않는다.

설치
----
python -m pip install -r requirements.txt

실행
----
python portfolio_briefing_pipeline.py
python portfolio_briefing_pipeline.py --no-gemini
python portfolio_briefing_pipeline.py --news-limit 3 --top-contributors 5

Gemini까지 실행하려면
--------------------
환경변수 GEMINI_API_KEY 설정 후 실행:
  Windows PowerShell:
    $env:GEMINI_API_KEY="YOUR_KEY"
    python portfolio_briefing_pipeline.py

출력
----
briefing_input.json
briefing.md          # GEMINI_API_KEY가 있고 --no-gemini가 아닐 때
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import pdfplumber
from playwright.sync_api import sync_playwright
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

import os
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


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isnan(value):
            return None
        return float(value)

    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text.lower() in {"nan", "n/a", "none", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_weight(
    value: Any,
    column_name: str = "",
    source_hint: str = "",
) -> float | None:
    """
    ETF 비중을 0~1 단위로 통일.

    예:
    8.14% -> 0.0814
    0.15% -> 0.0015
    yfinance 0.0814 -> 0.0814
    """
    number = safe_float(value)

    if number is None:
        return None

    col = column_name.lower()
    source = source_hint.lower()
    text = str(value)

    # State Street XLSX는 Weight 열이
    # 8.14, 0.15 같은 '퍼센트 숫자' 형태
    if "state street" in source:
        return number / 100.0

    # 열 이름 자체에 (%)가 명시된 공식 자료
    if "%" in col or "%" in text:
        return number / 100.0

    # yfinance Holding Percent는 이미
    # 0.0814 == 8.14% 형태
    if "holding percent" in col:
        return number if number <= 1 else number / 100.0

    # 그 외
    return number / 100.0 if number > 1 else number


def yahoo_symbol(symbol: str) -> str:
    """BRK.B -> BRK-B 등 Yahoo Finance 형식으로 최소 보정."""
    s = str(symbol).strip().upper()
    s = s.replace("/", "-")
    if re.fullmatch(r"[A-Z]{1,5}\.[A-Z]", s):
        s = s.replace(".", "-")
    return s


def looks_like_equity_ticker(symbol: str) -> bool:
    """
    일반 미국 주식 ticker만 최대한 통과시킨다.

    제외:
    - 현금
    - MMF
    - 선물
    - 옵션/파생상품
    """
    s = yahoo_symbol(symbol)

    if not s:
        return False

    banned = {
        "USD",
        "CASH",
        "CASHUSD",
        "RECPAY",
        "N/A",
        "NA",
        "USDF",
        "MMF",
        "SWEEP",
    }

    if s in banned:
        return False

    # 선물 코드 제외
    # 예: ESU6, NQU6
    #
    # F G H J K M N Q U V X Z
    # = 선물 월(month) 코드
    futures_pattern = r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d{1,2}$"

    if re.fullmatch(futures_pattern, s):
        return False

    # 일반 미국주식 ticker 형태
    return bool(
        re.fullmatch(
            r"[A-Z][A-Z0-9\-]{0,6}",
            s,
        )
    )


# ---------------------------------------------------------------------------
# 1. 가격
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 2. ETF holdings
# ---------------------------------------------------------------------------

def request_bytes(url: str) -> bytes:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.content


def request_text(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def detect_as_of_from_text(text: str) -> str:
    patterns = [
        r"(?:Data\s+as\s+of|as\s+of)\s+(\d{1,2}/\d{1,2}/\d{4})",
        r"(?:Data\s+as\s+of|as\s+of)\s+([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1)
    return ""

def detect_holdings_as_of(text: str) -> str:
    """
    Top Holdings 섹션 안의 Data as of 날짜만 찾는다.
    다른 성과/배당/예정일 날짜를 잡지 않는다.
    """

    # Top Holdings 이후 영역만 잘라낸다.
    match = re.search(
        r"Top\s+Holdings(.*?)(?:Holdings\s+are\s+subject\s+to\s+change|$)",
        text,
        flags=re.I | re.S,
    )

    if not match:
        return ""

    holdings_section = match.group(1)

    date_match = re.search(
        r"Data\s+as\s+of[:\s]*"
        r"(\d{1,2}/\d{1,2}/\d{4})",
        holdings_section,
        flags=re.I,
    )

    if date_match:
        return date_match.group(1)

    return ""

def validate_as_of_date(as_of: str) -> str:
    """
    holdings 날짜가 미래라면 잘못 파싱된 것으로 판단한다.
    """

    if not as_of:
        return ""

    try:
        parsed = datetime.strptime(
            as_of,
            "%m/%d/%Y"
        ).date()
    except ValueError:
        return as_of

    today_kst = datetime.now(KST).date()

    if parsed > today_kst:
        return ""

    return as_of

def _stringify_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [
            " ".join(str(x) for x in col if str(x) != "nan").strip()
            for col in out.columns
        ]
    else:
        out.columns = [str(c).strip() for c in out.columns]
    return out


def _find_header_row(raw: pd.DataFrame) -> pd.DataFrame:
    """
    XLSX가 상단 메타데이터 + 중간 헤더 구조일 때
    ticker/symbol/weight 키워드가 있는 행을 헤더로 재설정.
    """
    raw = raw.copy()
    max_scan = min(len(raw), 40)
    for i in range(max_scan):
        row_text = " | ".join(str(x) for x in raw.iloc[i].tolist()).lower()
        has_symbol = any(k in row_text for k in ["ticker", "identifier", "symbol"])
        has_weight = any(k in row_text for k in ["weight", "% of fund", "weighting"])
        if has_symbol and has_weight:
            header = [str(x).strip() for x in raw.iloc[i].tolist()]
            body = raw.iloc[i + 1 :].copy()
            body.columns = header
            return body
    return raw


def normalize_holdings_frame(
    df: pd.DataFrame,
    *,
    source_hint: str = "",
) -> list[Holding]:
    """
    운용사별 서로 다른 column 명을 최대한 일반화한다.
    """
    df = _stringify_columns(df)
    df = _find_header_row(df)
    df = _stringify_columns(df)

    cols = {c.lower(): c for c in df.columns}

    def choose_column(keywords: list[str]) -> str | None:
        # 정확 일치 우선
        for k in keywords:
            if k in cols:
                return cols[k]
        # 부분 일치
        for lower, original in cols.items():
            for k in keywords:
                if k in lower:
                    return original
        return None

    symbol_col = choose_column([
        "ticker",
        "symbol",
        "identifier",
        "security identifier",
    ])
    weight_col = choose_column([
        "weight",
        "weighting (%)",
        "weighting",
        "% of fund",
        "weight (%)",
        "weighting(%)",
    ])
    name_col = choose_column([
        "security name",
        "company",
        "name",
        "description",
    ])

    if not symbol_col or not weight_col:
        return []

    holdings: list[Holding] = []
    seen: set[str] = set()

    for _, row in df.iterrows():
        raw_symbol = row.get(symbol_col)
        raw_weight = row.get(weight_col)

        symbol = yahoo_symbol(raw_symbol)
        weight = normalize_weight(
            raw_weight,
            weight_col,
            source_hint,
        )

        if not looks_like_equity_ticker(symbol):
            continue
        if weight is None or weight <= 0 or weight > 1:
            continue
        if symbol in seen:
            continue

        name = str(row.get(name_col, "") if name_col else "").strip()
        holdings.append(Holding(symbol=symbol, name=name, weight=weight))
        seen.add(symbol)

    return holdings


def parse_html_holdings(url: str) -> tuple[list[Holding], str]:
    html = request_text(url)
    page_text = BeautifulSoup(
        html,
        "html.parser"
    ).get_text(" ", strip=True)

    as_of = validate_as_of_date(
        detect_holdings_as_of(page_text)
    )

    # pd.read_html가 HTML 안의 표를 가져온다.
    tables = pd.read_html(io.StringIO(html))
    candidates: list[list[Holding]] = []

    for table in tables:
        parsed = normalize_holdings_frame(table)
        if parsed:
            candidates.append(parsed)

    if not candidates:
        return [], as_of

    # 가장 많은 holdings를 포함한 표를 채택
    best = max(candidates, key=len)
    return best, as_of


def fetch_spym_official() -> HoldingsResult:
    cfg = ETF_CONFIG["SPYM"]
    url = cfg["holdings_xlsx"]
    content = request_bytes(url)

    # State Street 파일은 첫 부분에 설명행이 있을 수 있어 header=None으로 읽고 탐색
    raw = pd.read_excel(io.BytesIO(content), header=None)
    holdings = normalize_holdings_frame(raw, source_hint="State Street")

    # workbook 전체에서 as-of 텍스트 탐색
    as_of = ""
    try:
        all_text = " ".join(str(x) for x in raw.head(30).fillna("").to_numpy().ravel())
        as_of = detect_as_of_from_text(all_text)
    except Exception:
        pass

    if not holdings:
        raise RuntimeError("State Street Daily Holdings XLSX를 파싱하지 못했습니다.")

    return HoldingsResult(
        etf="SPYM",
        holdings=holdings,
        source="State Street official Daily Holdings XLSX",
        source_url=url,
        as_of=as_of,
        is_full_holdings=len(holdings) >= 400,
    )

def fetch_qqqi_official() -> HoldingsResult:
    """
    NEOS 공식 QQQI 페이지에서
    'Download Full Holdings' 버튼을 실제로 클릭하여
    전체 holdings 파일을 다운로드한다.
    """

    cfg = ETF_CONFIG["QQQI"]
    url = cfg["official_page"]

    print(
        "[QQQI] NEOS official Full Holdings download..."
    )

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            user_agent=HEADERS["User-Agent"],
            accept_downloads=True,
        )

        try:
            # -------------------------
            # 1. QQQI 페이지 접속
            # -------------------------
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            # 페이지의 JS가 holdings 영역을 렌더링할 시간
            page.wait_for_timeout(5000)

            # 페이지 텍스트
            body_text = page.locator(
                "body"
            ).inner_text()

            # -------------------------
            # 2. Holdings 기준 날짜
            # -------------------------
            as_of = ""

            date_match = re.search(
                r"Top\s+Holdings.*?"
                r"Data\s+as\s+of[:\s]*"
                r"(\d{1,2}/\d{1,2}/\d{4})",
                body_text,
                flags=re.I | re.S,
            )

            if date_match:
                as_of = date_match.group(1)

            # -------------------------
            # 3. Download Full Holdings
            # 버튼 찾기
            # -------------------------
            # NEOS 페이지의 다운로드 함수가
            # JavaScript로 로드될 때까지 기다린다.
            page.wait_for_function(
                "typeof downloadHoldingsCSV === 'function'",
                timeout=30000,
            )

            print(
                "[QQQI] downloadHoldingsCSV function found"
            )

            # 버튼을 실제 클릭하지 않고
            # 버튼의 onclick 함수를 직접 실행한다.
            #
            # NEOS HTML:
            # onclick="downloadHoldingsCSV('QQQI');"
            with page.expect_download(
                timeout=30000
            ) as download_info:

                page.evaluate(
                    "downloadHoldingsCSV('QQQI')"
                )

            download = download_info.value

            # -------------------------
            # 4. 실제 다운로드
            # -------------------------

            filename = (
                download.suggested_filename
                or "qqqi_holdings"
            )

            temp_path = download.path()

            if temp_path is None:
                raise RuntimeError(
                    "QQQI Full Holdings 파일 다운로드 경로를 얻지 못했습니다."
                )

            print(
                f"[QQQI] downloaded: {filename}"
            )

            # -------------------------
            # 5. CSV / Excel 판별
            # -------------------------
            filename_lower = filename.lower()

            if filename_lower.endswith(".csv"):

                raw = pd.read_csv(
                    temp_path,
                )
                # Cash & Other 제외
                raw = raw[
                    ~raw["StockTicker"]
                    .astype(str)
                    .str.upper()
                    .isin({
                        "CASH&OTHER",
                        "CASH",
                    })
                ]

                raw = raw[
                    raw["MoneyMarketFlag"]
                    .fillna("")
                    .astype(str)
                    .str.upper()
                    != "Y"
                ]

            elif (
                filename_lower.endswith(".xlsx")
                or filename_lower.endswith(".xls")
            ):

                raw = pd.read_excel(
                    temp_path,
                    header=None,
                )

            else:
                # 확장자가 이상하면
                # CSV → Excel 순서로 시도
                try:
                    raw = pd.read_csv(
                        temp_path,
                        header=None,
                    )

                except Exception:

                    raw = pd.read_excel(
                        temp_path,
                        header=None,
                    )

            # -------------------------
            # CSV에 적힌 실제 Holdings 기준일 사용
            # -------------------------
            if "Date" in raw.columns:

                dates = (
                    raw["Date"]
                    .dropna()
                    .astype(str)
                    .unique()
                )

                if len(dates) > 0:
                    as_of = dates[0]

            print(
                f"[QQQI] CSV holdings as_of={as_of or 'unknown'}"
            )

        finally:
            browser.close()

    # NEOS Full Holdings의 Cash & Other 행 제거
    if "StockTicker" in raw.columns:

        raw = raw[
            ~raw["StockTicker"]
            .astype(str)
            .str.upper()
            .isin({
                "CASH&OTHER",
                "CASH",
            })
        ]

    if "MoneyMarketFlag" in raw.columns:

        raw = raw[
            raw["MoneyMarketFlag"]
            .fillna("")
            .astype(str)
            .str.upper()
            != "Y"
        ]
    # -------------------------
    # 6. 기존 공통 parser 사용
    # -------------------------
    holdings = normalize_holdings_frame(
        raw,
        source_hint="NEOS",
    )

    if not holdings:
        raise RuntimeError(
            "QQQI Full Holdings 파일은 다운로드했지만 "
            "구성종목을 파싱하지 못했습니다."
        )

    # -------------------------
    # 7. 비중 계산
    # -------------------------
    total_weight = sum(
        holding.weight
        for holding in holdings
    )

    coverage = total_weight * 100

    print(
        f"[QQQI] holdings={len(holdings)}, "
        f"equity weight={coverage:.2f}%, "
        f"as_of={as_of or 'unknown'}"
    )

    # QQQI는 Nasdaq-100 주식 +
    # 옵션 전략이므로 100% 주식일 필요는 없음
    is_full = (
        len(holdings) >= 80
        and coverage >= 80
    )

    return HoldingsResult(
        etf="QQQI",

        holdings=holdings,

        source=(
            "NEOS official Full Holdings download"
        ),

        source_url=url,

        as_of=as_of,

        is_full_holdings=is_full,

        warning=(
            ""
            if is_full
            else (
                f"NEOS Full Holdings를 받았지만 "
                f"주식 {len(holdings)}개, "
                f"{coverage:.2f}%만 계산 가능합니다. "
                f"옵션/현금 등 비주식 자산이 포함될 수 있습니다."
            )
        ),
    )

def fetch_qqqm_official() -> HoldingsResult:
    """
    Invesco 공식 QQQM Holdings API를 직접 사용한다.

    percentageOfTotalNetAssets:
        8.427906 == 8.427906%
    따라서 Python 내부에서는 0.08427906으로 변환한다.
    """

    url = (
        "https://dng-api.invesco.com/cache/v1/accounts/"
        "en_US/shareclasses/46138G649/holdings/fund"
        "?idType=cusip"
        "&productType=ETF"
    )

    print(
        "[QQQM] Invesco official Holdings API fetch..."
    )

    response = requests.get(
        url,
        timeout=30,
    )

    response.raise_for_status()

    # Content-Type은 text/plain이지만
    # 실제 본문은 JSON이다.
    data = response.json()

    raw_holdings = data.get(
        "holdings",
        []
    )

    if not raw_holdings:
        raise RuntimeError(
            "Invesco QQQM API에서 holdings가 없습니다."
        )

    holdings: list[Holding] = []

    skipped = []

    for item in raw_holdings:

        symbol = yahoo_symbol(
            item.get("ticker", "")
        )

        name = (
            item.get("issuerName")
            or ""
        )

        security_type = (
            item.get("securityTypeName")
            or ""
        )

        security_code = (
            item.get("securityTypeCode")
            or ""
        )

        raw_weight = item.get(
            "percentageOfTotalNetAssets"
        )

        weight_pct = safe_float(
            raw_weight
        )

        if weight_pct is None:
            continue

        # Invesco 값:
        # 8.427906 = 8.427906%
        #
        # Python 내부:
        # 0.08427906
        weight = (
            weight_pct / 100.0
        )

        # 주식 종목만 기여도 계산
        is_equity = (
            security_code.upper() == "COM"
            or "COMMON STOCK" in security_type.upper()
        )

        if not is_equity:
            skipped.append(
                {
                    "ticker": symbol,
                    "type": security_type,
                    "weight_pct": weight_pct,
                }
            )
            continue

        if not looks_like_equity_ticker(
            symbol
        ):
            continue

        holdings.append(
            Holding(
                symbol=symbol,
                name=name,
                weight=weight,
                instrument_type="equity",
            )
        )

    if not holdings:
        raise RuntimeError(
            "Invesco QQQM API에서 "
            "주식 holdings를 파싱하지 못했습니다."
        )

    total_weight = sum(
        holding.weight
        for holding in holdings
    )

    coverage = (
        total_weight * 100
    )

    as_of = (
        data.get("effectiveBusinessDate")
        or data.get("effectiveDate")
        or ""
    )

    total_api_holdings = data.get(
        "totalNumberOfHoldings"
    )

    print(
        f"[QQQM] "
        f"API holdings={total_api_holdings}, "
        f"equities={len(holdings)}, "
        f"equity weight={coverage:.2f}%, "
        f"as_of={as_of or 'unknown'}"
    )

    if skipped:
        print(
            f"[QQQM] "
            f"non-equity holdings skipped="
            f"{len(skipped)}"
        )

    # 패시브 ETF이므로
    # 약 90% 이상 주식 coverage 확보 시
    # 충분한 품질로 판단
    is_full = (
        len(holdings) >= 90
        and coverage >= 90
    )

    return HoldingsResult(
        etf="QQQM",
        holdings=holdings,

        source=(
            "Invesco official QQQM Holdings API"
        ),

        source_url=url,

        as_of=as_of,

        is_full_holdings=is_full,

        warning=(
            ""
            if is_full
            else (
                f"Invesco 공식 API에서 "
                f"주식 {len(holdings)}개, "
                f"{coverage:.2f}%를 확보했습니다."
            )
        ),
    )

def fetch_jepq_official() -> HoldingsResult:
    """
    J.P. Morgan 공식 Daily ETF Holdings PDF 사용.
    주식만 contribution 계산에 포함하고
    ELN/현금 등은 제외한다.
    """

    cfg = ETF_CONFIG["JEPQ"]
    url = cfg["holdings_page"]

    print(
        "[JEPQ] J.P. Morgan official Daily Holdings PDF fetch..."
    )

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30,
    )

    response.raise_for_status()

    # 실제 PDF인지 확인
    content_type = response.headers.get(
        "Content-Type",
        ""
    ).lower()

    if "pdf" not in content_type:
        raise RuntimeError(
            f"JEPQ holdings 응답이 PDF가 아닙니다: {content_type}"
        )

    text_parts = []

    with pdfplumber.open(
        io.BytesIO(response.content)
    ) as pdf:

        for page in pdf.pages:
            text = page.extract_text()

            if text:
                text_parts.append(text)

    full_text = "\n".join(text_parts)

    # ---------------------------
    # 기준 날짜
    # ---------------------------

    as_of = ""

    date_match = re.search(
        r"As\s+of\s+Date:\s*"
        r"(\d{1,2}/\d{1,2}/\d{4})",
        full_text,
        flags=re.I,
    )

    if date_match:
        as_of = date_match.group(1)

    # ---------------------------
    # holdings 파싱
    # ---------------------------

    lines = [
        line.strip()
        for line in full_text.splitlines()
        if line.strip()
    ]

    holdings: list[Holding] = []

    # CUSIP + ticker로 시작하는 새로운 holding 탐색
    #
    # 예:
    # 67066G104 NVDA NVIDIA CORP COMMON
    #
    start_pattern = re.compile(
        r"^([A-Z0-9]{8,10})\s+"
        r"([A-Z][A-Z0-9\-.]{0,8})\s+"
        r"(.+)$"
    )

    current_block = None
    blocks = []

    for line in lines:

        match = start_pattern.match(line)

        if match:

            if current_block:
                blocks.append(current_block)

            current_block = {
                "security_id": match.group(1),
                "ticker": match.group(2),
                "text": line,
            }

        elif current_block:
            current_block["text"] += " " + line

    if current_block:
        blocks.append(current_block)

    # ---------------------------
    # 주식만 분리
    # ---------------------------

    for block in blocks:

        symbol = yahoo_symbol(
            block["ticker"]
        )

        text = block["text"]

        upper = text.upper()

        # ELN 제외
        if "EQUITY LINKED NOTES" in upper:
            continue

        # 현금/MMF 제외
        if symbol in {
            "CASH",
            "JIMXX",
        }:
            continue

        # JEPQ 공식 PDF에서:
        #
        # 일반 주식/ADR/REIT → Physical
        # ELN              → Synthetic
        #
        # 따라서 Physical 자산 중
        # Money Market/현금 등을 제외하는 방식이
        # PDF 텍스트 추출에 더 안정적이다.

        if "SYNTHETIC" in upper:
            # Equity Linked Note
            continue

        if "EQUITY LINKED NOTES" in upper:
            continue

        if "MONEY MARKET" in upper:
            continue

        if symbol in {
            "CASH",
            "JIMXX",
        }:
            continue

        # 일반 주식/ADR/REIT 등은 대부분 Physical
        if "PHYSICAL" not in upper:
            continue

        if not looks_like_equity_ticker(
            symbol
        ):
            continue

        # 블록 마지막 부분의 % 숫자들 추출
        #
        # 예:
        # ... 7.45% 7.4%
        #
        percentages = re.findall(
            r"(\d+(?:\.\d+)?)%",
            text,
        )

        if len(percentages) < 2:
            continue

        # 마지막 값 = % of Net Assets
        net_assets_pct = float(
            percentages[-1]
        )

        weight = (
            net_assets_pct / 100.0
        )

        holdings.append(
            Holding(
                symbol=symbol,
                name="",
                weight=weight,
                instrument_type="equity",
            )
        )

    if not holdings:
        raise RuntimeError(
            "JEPQ PDF에서 주식 holdings를 파싱하지 못했습니다."
        )

    print(
        f"[JEPQ] parsed equity symbols: "
        f"{[h.symbol for h in holdings[:15]]}"
    )

    total_weight = sum(
        h.weight
        for h in holdings
    )

    coverage = total_weight * 100

    print(
        f"[JEPQ] equities={len(holdings)}, "
        f"equity weight={coverage:.2f}%, "
        f"as_of={as_of or 'unknown'}"
    )

    # JEPQ는 ELN이 있으므로
    # 주식 coverage가 100%일 필요 없음
    is_full = (
        len(holdings) >= 50
        and coverage >= 60
    )

    return HoldingsResult(
        etf="JEPQ",
        holdings=holdings,
        source=(
            "J.P. Morgan official "
            "Daily ETF Holdings PDF"
        ),
        source_url=url,
        as_of=as_of,
        is_full_holdings=is_full,
        warning=(
            "JEPQ는 ELN/현금 등을 별도로 보유하므로 "
            "여기 표시되는 coverage는 주식 포트폴리오 비중입니다."
        ),
    )

def fetch_yfinance_top_holdings(etf: str, reason: str) -> HoldingsResult:
    """
    공식 자료 자동 수집 실패 시 명시적인 fallback.
    top_holdings만 사용하므로 전체 기여도라고 부르지 않는다.
    """
    df = yf.Ticker(etf).funds_data.top_holdings.reset_index()
    holdings = normalize_holdings_frame(df)

    if not holdings:
        # yfinance 표 구조가 normalize_holdings_frame과 달라질 경우 직접 처리
        holdings = []
        for _, row in df.iterrows():
            symbol = yahoo_symbol(row.get("Symbol", row.iloc[0] if len(row) else ""))
            weight = safe_float(row.get("Holding Percent"))
            if looks_like_equity_ticker(symbol) and weight and weight > 0:
                holdings.append(
                    Holding(
                        symbol=symbol,
                        name=str(row.get("Name", "")),
                        weight=float(weight),
                    )
                )

    if not holdings:
        raise RuntimeError(f"{etf}: yfinance top_holdings도 가져오지 못했습니다.")

    return HoldingsResult(
        etf=etf,
        holdings=holdings,
        source="yfinance funds_data.top_holdings (fallback, partial)",
        source_url="https://finance.yahoo.com/",
        as_of="",
        is_full_holdings=False,
        warning=f"공식 holdings 자동 수집 실패: {reason}. 상위 보유종목만 사용합니다.",
    )


def get_holdings(etf: str) -> HoldingsResult:
    fetchers = {
        "SPYM": fetch_spym_official,
        "QQQI": fetch_qqqi_official,
        "QQQM": fetch_qqqm_official,
        "JEPQ": fetch_jepq_official,
    }
    try:
        return fetchers[etf]()

    except Exception as exc:
        print(
            f"[{etf}] official holdings failed: "
            f"{type(exc).__name__}: {exc}"
        )

        return fetch_yfinance_top_holdings(
            etf,
            str(exc),
        )

# ---------------------------------------------------------------------------
# 3. 기여도
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 4. 뉴스
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 5. Gemini
# ---------------------------------------------------------------------------

def build_gemini_prompt(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, indent=2)

    return f"""
너는 미국 주식/ETF 일일 브리핑 분석가다.

아래 JSON은 Python이 계산한 사실 데이터다.
숫자를 새로 추측하거나 임의로 보정하지 말고 JSON에 있는 값만 사용한다.

중요 규칙:
1. 가격은 미국 정규장 Close 기준이다.
2. ETF 기여도 contribution_pp는 Python이 '구성비 × 종목 수익률'로 계산했다.
3. holdings_source, holdings_as_of, calculable_weight_coverage_pct, quality를 반드시 확인한다.
4. quality가 PARTIAL이면 전체 ETF 기여도라고 단정하지 말고 '확인 가능한 보유종목 기준'이라고 표현한다.
5. QQQI/JEPQ의 unexplained_residual_pp를 옵션/ELN 효과라고 단정하지 않는다.
   누락 holdings, 옵션/ELN, 현금, 운용비용, NAV-시장가격 괴리 등이 함께 포함될 수 있다.
6. 영어 뉴스 제목은 한국어로 번역하고 핵심 내용을 한국어로 요약한다.
7. 뉴스와 가격의 인과관계가 확실하지 않으면 '관련 가능성이 있다', '영향을 준 것으로 보인다'라고 표현한다.
8. 뉴스 실제 발행일을 숨기지 않는다.
9. 데이터가 부족하면 부족하다고 명시한다.
10. 투자 매수/매도 지시를 하지 않는다.
11. 브리핑의 기준 날짜는 KST 오늘 날짜다.
12. 가격 데이터는 가장 최근 종료된 미국 정규장의 Close를 사용한다.
    특히 월요일 KST 아침 브리핑에서는 가장 최근 미국 정규장이 금요일이므로 금요일 종가를 사용한다.
13. 뉴스 섹션은 KST 오늘 발행된 뉴스만 다룬다.
    오늘 뉴스가 없는 종목은 '오늘 관련 뉴스 없음'이라고 표시한다.
    과거 날짜 뉴스로 임의 fallback하지 않는다.
14. 오늘 발행된 뉴스가 최근 거래일 종가 이후에 나온 경우,
    해당 뉴스를 이미 끝난 주가 움직임의 원인으로 설명하지 않는다.
    '오늘 체크할 뉴스', '향후 영향 가능성'으로만 설명한다.
15. '오늘 내 포트폴리오가 움직인 이유'에서는
    가장 최근 거래일의 가격·ETF 기여도처럼 숫자로 확인된 사실을 우선 설명한다.
    뉴스의 발행시각이 해당 거래일보다 이후라면 원인 분석에 사용하지 않는다.
16. calculable_weight_coverage_pct가 0이면 unexplained_residual_pp를
    옵션/ELN 등 특정 요인 때문이라고 단정하지 않는다.
    '보유종목 기준일 문제로 종목별 기여도 분석이 불가능하다'고만 설명한다.
17. S&P 500, Nasdaq, Dow 등의 지수 데이터가 JSON에 없으면
    시장 전체가 상승·하락·혼조였다고 표현하지 않는다.
    포트폴리오 내 종목 흐름만 설명한다.

다음 형식으로 한국어 브리핑을 작성한다.

# ① 시장 한줄 요약
- 포트폴리오의 가장 최근 거래일 흐름을 한 문장으로 요약.

# ② 내 종목 가격
| 종목 | 종가 | 등락률 | 분배금 | 가격 기준일 |

# ③ ETF 상승·하락 기여 요인
ETF별로:
- 계산 품질(HIGH/MEDIUM/PARTIAL)
- holdings 기준일/출처
- 계산 가능 비중
- 상승 기여 TOP 5
- 하락 기여 TOP 5
- 주식 기여도 합계
- ETF 실제 가격 등락률
- 설명되지 않는 잔차
- QQQI/JEPQ는 파생상품 구조 때문에 해석 한계를 명시

# ④ 내 종목 관련 핵심 뉴스
| 발행일시(KST) | 관련 종목 | 한글 제목 | 핵심 내용 | 가능한 영향 |
각 기사 URL도 함께 표시.

# ⑤ 오늘 내 포트폴리오가 움직인 이유
- 데이터와 뉴스를 연결해 3~5개 요인.
- 숫자로 확인 가능한 요인과 해석을 구분.

# ⑥ 오늘 체크할 것
- 데이터에서 직접 확인 가능한 위험요인/관찰포인트를 최대 3개.
- 일정 데이터가 JSON에 없으면 임의의 실적일/경제지표 일정을 만들어내지 않는다.

JSON:
{data}
""".strip()


def maybe_run_gemini(payload: dict[str, Any], model: str) -> str | None:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError(
            "Gemini 실행을 위해 google-genai가 필요합니다: pip install google-genai"
        ) from exc

    client = genai.Client(api_key=api_key)
    prompt = build_gemini_prompt(payload)

    response = client.models.generate_content(
        model=model,
        contents=prompt,
    )
    return response.text


# ---------------------------------------------------------------------------
# 6. 전체 파이프라인
# ---------------------------------------------------------------------------

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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
