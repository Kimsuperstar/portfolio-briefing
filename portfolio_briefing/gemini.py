from __future__ import annotations

import json
import os
from typing import Any

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


