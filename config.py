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

# rating: S&P 장기 신용등급(참고용, 2026-10 기준). bucket: 위 스프레드 지수 중 어느 것을 쓸지
TICKERS = [
    {"ticker": "CRWV", "name": "코어위브", "rating": "B+", "bucket": "B"},
    {"ticker": "ORCL", "name": "오라클", "rating": "BBB-", "bucket": "BBB"},
    {"ticker": "META", "name": "메타", "rating": "AA-", "bucket": "AA"},
    {"ticker": "GOOGL", "name": "구글", "rating": "AA+", "bucket": "AA"},
    {"ticker": "MSFT", "name": "마이크로소프트", "rating": "AAA", "bucket": "AAA"},
    {"ticker": "NVDA", "name": "엔비디아", "rating": "AA-", "bucket": "AA"},
    {"ticker": "TSLA", "name": "테슬라", "rating": "BBB", "bucket": "BBB"},
    {"ticker": "AMZN", "name": "아마존", "rating": "AA", "bucket": "AA"},
    {"ticker": "AVGO", "name": "브로드컴", "rating": "BBB+", "bucket": "BBB"},
    {"ticker": "AMD", "name": "AMD", "rating": "A-", "bucket": "A"},
]

# GPU 임대가격 (Price of Compute 무료 API, 공급사별 시간당 가격의 일별 중앙값)
GPU_SKUS = [
    {"sku": "b300", "name": "B300 (블랙웰 울트라)"},
    {"sku": "b200", "name": "B200 (블랙웰)"},
    {"sku": "h200-sxm", "name": "H200 SXM"},
    {"sku": "h100-sxm", "name": "H100 SXM"},
]

# 위험 색깔 기준 (README에 설명)
RISK = {
    "cds_red": 400, "cds_yellow": 150,          # 최근 CDS(bp)
    "spread_red": 500, "spread_yellow": 200,    # 등급 스프레드(bp)
    "m1_red": -25.0, "m1_yellow": -10.0,        # 1개월 주가 변화(%)
    "cds_max_age_days": 90,                     # 이보다 오래된 CDS는 색 판정에서 제외
}
