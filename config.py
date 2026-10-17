# -*- coding: utf-8 -*-
"""대시보드 설정. 종목·등급·데이터 출처를 바꾸려면 이 파일만 고치면 됩니다."""

START_DATE = "2024-01-01"

# 신용등급별 회사채 스프레드 지수 (ICE BofA 옵션조정 스프레드, FRED 무료 데이터, 단위 % -> bp로 변환)
# 스프레드 = 같은 등급 회사채 수익률 - 같은 만기 미국채 수익률
SPREAD_SERIES = {
    "AAA": "BAMLC0A1CAAA",
    "AA": "BAMLC0A2CAA",
    "A": "BAMLC0A3CA",
    "BBB": "BAMLC0A4CBBB",
    "BB": "BAMLH0A1HYBB",
    "B": "BAMLH0A2HYB",
}

# rating: S&P 장기 신용등급(참고용, 2026-10 기준). "무등급" = 회사 전체 신용등급이 없음 → 하이일드 B 지수를 참고로 사용
# bucket: 위 스프레드 지수 중 어느 것을 쓸지. currency: 주가 통화
# 구성: CDS(부도 보험료)가 실제로 거래·보도되는 AI 부채 우려 기업만
# (네비우스·아이렌·어플라이드 디지털·테라울프·사이퍼는 CDS 수치가 없어 2026-10-17 제외)
TICKERS = [
    {"ticker": "CRWV", "name": "코어위브", "rating": "B+", "bucket": "B", "group": "네오클라우드"},
    {"ticker": "ORCL", "name": "오라클", "rating": "BBB-", "bucket": "BBB", "group": "AI 클라우드"},
    {"ticker": "9984.T", "name": "소프트뱅크그룹", "rating": "BB+", "bucket": "BB", "group": "AI 투자", "currency": "¥"},
    {"ticker": "NVDA", "name": "엔비디아", "rating": "AA-", "bucket": "AA", "group": "AI 칩"},
    {"ticker": "AVGO", "name": "브로드컴", "rating": "BBB+", "bucket": "BBB", "group": "AI 칩"},
]

# GPU 임대가격 (Price of Compute 무료 API, 공급사별 시간당 가격의 일별 중앙값)
GPU_SKUS = [
    {"sku": "b300", "name": "B300 (블랙웰 울트라)"},
    {"sku": "b200", "name": "B200 (블랙웰)"},
    {"sku": "h200-sxm", "name": "H200 SXM"},
    {"sku": "h100-sxm", "name": "H100 SXM"},
]

# 국민연금 국내주식 리밸런싱 허용범위(목표비중 ± %p). 2026년 5월 '한시 확대' 후 상·하단이 공개되지 않아 비워 둠.
# 공개되면 예: {"국내주식": 3.0} 처럼 적으면 차트에 허용범위가 표시됩니다.
NPS_BAND = {}

# 하이일드 스프레드(%) '박스권 위쪽' 기준선 — 증권사 메모 차트의 점선(약 3.2%)을 따름
HY_BOX_TOP = 3.2

# 위험 색깔 기준 (README에 설명)
RISK = {
    "cds_red": 400, "cds_yellow": 150,          # 최근 CDS(bp)
    "spread_red": 500, "spread_yellow": 200,    # 등급 스프레드(bp)
    "m1_red": -25.0, "m1_yellow": -10.0,        # 1개월 주가 변화(%)
    "cds_max_age_days": 90,                     # 이보다 오래된 CDS는 색 판정에서 제외
}
