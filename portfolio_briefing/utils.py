from __future__ import annotations

import math
import re
from typing import Any

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

