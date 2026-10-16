# AI 신용위험 대시보드

AI 관련 10개 종목(코어위브·오라클·메타·구글·마이크로소프트·엔비디아·테슬라·아마존·브로드컴·AMD)의
주가, 신용등급별 회사채 스프레드, CDS(뉴스 수집), 엔비디아 GPU 임대가격을 폰에서 보는 대시보드입니다.

- 보는 주소: https://jejukairos.github.io/ai-credit-risk-dashboard/
- 매일 오후 4시(한국시간) 이 컴퓨터의 작업 스케줄러가 `run_update.bat` 을 실행해 자동 갱신합니다.
  (컴퓨터가 켜져 있어야 합니다. 꺼져 있었다면 켜진 뒤 바로 한 번 실행됩니다.)
- CDS는 주 1회(월요일 오전) Claude 예약 작업이 뉴스를 검색해 `cds_log.csv` 에 추가합니다.

## 데이터 출처 (모두 무료)

| 항목 | 출처 | 비고 |
|---|---|---|
| 주가 | Yahoo Finance (yfinance) | 2024-01-01부터 일별 종가 |
| 채권 스프레드 | FRED — ICE BofA 등급별 옵션조정 스프레드 | 종목별 개별 채권이 아니라 **같은 신용등급 회사채 평균**. (FINRA 무료 API에는 종목별 TRACE 거래 데이터가 없어 대체) |
| CDS 5년물 | 뉴스 기사 | `cds_log.csv` 에 날짜·종목·bp·출처URL·메모. 출처 없는 숫자는 넣지 않음 |
| GPU 임대가격 | [Price of Compute](https://priceofcompute.com) 무료 API | 여러 클라우드의 온디맨드 가격 일별 중앙값 |

신용등급 → 스프레드 지수 연결은 `config.py` 에 있습니다 (예: 오라클 BBB- → BBB 지수, 코어위브 B+ → B 지수).

## 위험 색깔 기준

- 빨강: 최근 CDS ≥ 400bp, 또는 스프레드 ≥ 500bp, 또는 1개월 주가 ≤ −25%
- 노랑: CDS ≥ 150bp, 또는 스프레드 ≥ 200bp, 또는 1개월 주가 ≤ −10%
- 초록: 그 외. (CDS는 90일 이내 기록만 반영) — 기준은 `config.py` 의 `RISK` 에서 바꿀 수 있습니다.

## 직접 해볼 수 있는 것

- **지금 바로 갱신**: 폴더에서 `run_update.bat` 더블클릭 → 1~2분 뒤 폰에서 새로고침
- **CDS 수치 직접 추가**: `cds_log.csv` 를 메모장으로 열어 맨 아래 줄에
  `2026-10-20,ORCL,300,https://기사주소,메모` 형식으로 추가 (출처 주소 필수)
- **FRED 키 등록(권장)**: 키 없이도 동작하지만 FRED가 가끔 응답을 안 줘서, 무료 키가 있으면 더 안정적입니다.
  1. https://fredaccount.stlouisfed.org/login/secure/ 에서 회원가입(이메일만 있으면 됨)
  2. 로그인 후 https://fredaccount.stlouisfed.org/apikeys 에서 **Request API Key** → 사용 목적에 "personal dashboard" 정도 입력 → 키 복사
  3. 이 폴더의 `set_fred_key.bat` 더블클릭 → 키 붙여넣기 → Enter
- **갱신 기록 보기**: `logs\update.log`

## 파일 설명

- `update.py` — 데이터 받아서 `docs/data.json`, `docs/status.json` 생성
- `config.py` — 종목·등급·기준값 설정
- `docs/index.html` — 화면 (GitHub Pages가 `docs` 폴더를 웹사이트로 보여줌)
- `data/` — 받아둔 데이터 (갱신 실패 시 이 데이터로 화면 유지)
- `run_update.bat` — 자동 실행 파일 (갱신 + GitHub 업로드)

투자 권유가 아닌 참고용 정보입니다.
