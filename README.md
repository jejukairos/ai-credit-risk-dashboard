# AI 신용위험 대시보드

CDS(부도 보험료)가 거래되는 AI 빚 많은 기업 5곳의
주가, 신용등급별 회사채 스프레드, CDS(뉴스 수집), 엔비디아 GPU 임대가격을 폰에서 보는 대시보드입니다.

- 보는 주소: https://jejukairos.github.io/ai-credit-risk-dashboard/
- 매일 오후 4시(한국시간) 이 컴퓨터의 작업 스케줄러가 `run_update.bat` 을 실행해 자동 갱신합니다.
  (컴퓨터가 켜져 있어야 합니다. 꺼져 있었다면 켜진 뒤 바로 한 번 실행됩니다.)
- 저녁 8시에 한 번 더 갱신합니다(그날 확정된 D램 가격·DXI 반영).
- CDS·AI 매출(ARR)은 주 1회(월요일 오전) Claude 예약 작업이 뉴스를 검색해 `cds_log.csv`·`arr_log.csv` 에 추가합니다.

## 추적 종목

- 코어위브(CRWV), 오라클(ORCL), 소프트뱅크그룹(9984.T, 엔화), 엔비디아(NVDA), 브로드컴(AVGO)
- CDS 수치가 없는 네오클라우드·데이터센터 기업(네비우스·아이렌·어플라이드 디지털·테라울프·사이퍼)은 2026-10-17 제외
- 쉬운 설명: CDS = 회사가 망할 때를 대비한 보험료 / 신용등급 = 돈 갚을 능력 성적표 / 스프레드 = 같은 성적표 회사들이 미국 정부보다 이자를 얼마나 더 주는지
- 종목 변경은 config.py 의 TICKERS 에서

## 데이터 출처 (모두 무료)

| 항목 | 출처 | 비고 |
|---|---|---|
| 주가 | Yahoo Finance (yfinance) | 2024-01-01부터 일별 종가 |
| 채권 스프레드 | FRED — ICE BofA 등급별 옵션조정 스프레드 | 종목별 개별 채권이 아니라 **같은 신용등급 회사채 평균**. (FINRA 무료 API에는 종목별 TRACE 거래 데이터가 없어 대체) |
| CDS 5년물 | 뉴스 기사 | `cds_log.csv` 에 날짜·종목·bp·출처URL·메모. 출처 없는 숫자는 넣지 않음 |
| GPU·LLM 토큰 지수 | [Silicon Data](https://www.silicondata.com/products/silicon-index) 공개 페이지 | H100·H200·B200·B300 임대지수, LLM Token Index(오픈웨이트·독점 포함). 페이지에 실린 최근 30일치를 매일 저장해 쌓음(2026-09-18~) |
| GPU 임대가격(참고) | [Price of Compute](https://priceofcompute.com) 무료 API | 여러 클라우드의 온디맨드 가격 일별 중앙값 |
| AI 매출(ARR) | 회사 발표·언론 보도·TickerTrends 추정 보도 | `arr_log.csv` 에 날짜·회사·금액($B)·종류(발표/추정/총액식)·출처URL. 매주 월요일 자동 검색으로 추가 |
| D램 가격 | TrendForce/DRAMeXchange 공개 시세 + 주간 'Memory Spot Price Update' 기사 | DDR4 8Gb 계약가격(반월 갱신)도 함께. 대표 3개만: DXI 지수, DDR5 16Gb 현물, DDR4 8Gb 현물(2023-07부터). DXI·DDR5는 과거치가 유료라 2026-10-16부터 매일 쌓임. 뉴스 수치는 `dram_log.csv` 에 출처와 함께 추가 가능 |
| 미수금·반대매매·신용융자 | 금융투자협회 FreeSIS | 2015년부터 일별 |
| CNN 공포·탐욕 지수 | CNN 사이트가 쓰는 공개 데이터 주소(비공식) | 2021년부터 일별. 10 이하 구간과 이후 S&P500 수익률 자동 계산 |
| VIX 지수 | Yahoo Finance (^VIX) | 2020년부터 일별. 20 위 주의, 30 위 공포 |
| 하이일드 스프레드 | FRED — ICE BofA US High Yield OAS (블룸버그 하이일드 스프레드와 같은 계열의 무료 데이터) | 2024년부터 일별, 블룸버그 화면보다 하루 늦고 0.1%p 안팎 차이. 박스권 위쪽 3.2% 기준선은 `config.py` 의 `HY_BOX_TOP` |
| 내부자 매수·매도 | Yahoo Finance (SEC Form 4 공시를 정리한 자료) | CDS 종목 중 미국 상장사(코어위브·오라클·엔비디아·브로드컴)만. 최근 약 150건만 주므로 매일 받아 쌓음. 10b5-1 계획매도 여부는 표시되지 않음. SEC 원본(EDGAR)은 요청에 연락처 이메일이 필요해 쓰지 않음 |
| 국민연금 자산배분 | 국민연금기금운용본부 홈페이지 | 매월 공시되는 실제 비중 + 올해 목표비중. 허용범위는 공개되면 `config.py` 의 `NPS_BAND` 에 입력 |

신용등급 → 스프레드 지수 연결은 `config.py` 에 있습니다 (예: 오라클 BBB- → BBB 지수, 코어위브 B+ → B 지수).

## CDS → 5년 내 부도확률

- 1년 부도율 ≈ CDS ÷ (1 − 회수율 40%), 5년 누적 부도확률 = 1 − e^(−5 × 1년 부도율)
- 예: 300bp → 약 22%, 1,000bp → 약 57%, 1,090bp → 약 60%. 회수율은 `config.py` 의 `CDS_RECOVERY`
- `cds_log.csv` 의 출처가 `user:` 로 시작하면 사용자가 준 자료 화면(예: 블룸버그 캡처)이라는 뜻. 원본 이미지는 `user_sources/` 폴더(공개 안 함)

## 하이일드 스프레드 사건 표시

- 최근 차트: `config.py` 의 `HY_EVENTS` 기간 안 최고점에 사건 이름 표시
- 장기 차트(1986~): FRED 하이일드는 최근 3년만 공개라 Moody's Baa 스프레드(BAA10Y)로 대신, `LONG_EVENTS` 의 위기 14개 표시

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
