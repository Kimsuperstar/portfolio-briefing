from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import pdfplumber
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .config import ETF_CONFIG, HEADERS, KST
from .models import Holding, HoldingsResult
from .utils import looks_like_equity_ticker, normalize_weight, safe_float, yahoo_symbol

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

