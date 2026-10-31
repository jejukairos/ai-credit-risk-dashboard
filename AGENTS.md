# AGENTS.md — AI 신용위험 대시보드 작업 안내 (AI 코딩 도우미용)

사용자는 개발자가 아닙니다. 답변은 **한국어로, 쉽게** 해 주세요. 사용자가 직접 할 일(가입·키 발급 등)은 단계별로 안내합니다.

## 무엇인가
- 폰 세로화면용 정적 대시보드. 공개 주소: https://jejukairos.github.io/ai-credit-risk-dashboard/
- GitHub Pages가 `main` 브랜치의 `docs/` 폴더를 보여 줌 (저장소: jejukairos/ai-credit-risk-dashboard)
- 데이터·출처·위험 기준 설명은 `README.md` 에 있음 — 먼저 읽을 것

## 파일
- `config.py` — 종목(TICKERS), 등급 스프레드, GPU, 사건 목록, 위험 기준, `HIDDEN_SECTIONS`(화면에서 숨긴 섹션)
- `update.py` — 모든 데이터를 받아 `data/` 에 저장하고 `docs/data.json`, `docs/status.json` 생성. 단계별로 실패해도 이전 데이터 유지, 실패는 status.json에 기록 → 화면 상단에 표시
- `docs/index.html` — 화면 전체(Plotly.js 2.35.2, 섹션마다 render* 함수)
- `docs/market.js` + `docs/market.json` — 🚦 시장 탭. 사용자의 다른 대시보드(밸류 신호등, `C:\Users\hyunm\valuation-dashboard`)에서 데이터를 복사해 옴(README 참고). 그쪽 폴더는 다른 세션이 작업 중이니 건드리지 않음
- `cds_log.csv`, `dram_log.csv`, `arr_log.csv`, `opinion_log.csv` — 사람/AI가 뉴스에서 찾아 직접 추가하는 기록
- `run_update.bat` — 갱신 + git commit/push. 로그는 `logs\update.log`
- `fg_update.py/.bat` — CNN 공포·탐욕 지수가 바뀌면 그때만 push

## 절대 규칙
1. **출처 없는 숫자는 절대 넣지 않는다.** 모든 수치는 출처 URL과 함께. 사용자가 준 캡처 화면은 출처를 `user:설명` 으로 적음. 추측·보간·계산으로 만든 값 금지(계산식이 README에 공개된 파생값만 예외: 예 CDS→부도확률)
2. 비밀 정보: FRED API 키는 `.env` 에만(커밋 금지). `user_sources/`(유료 화면 원본)는 공개·커밋 금지. 사용자 이메일을 외부 서비스에 보내지 않음
3. 기록 CSV는 기존 행을 고치지 말고 맨 아래에 추가만. 중복 금지
4. 차트 원칙: 이중 y축 금지(여러 칸으로 나눠 날짜축 공유), 마우스 올리면 날짜 전체(`%Y-%m-%d`)·숫자 표시, **그래프 클릭 시 다른 페이지로 이동 금지**(출처는 차트 아래 링크 목록으로)
5. 모든 섹션에 초등학생도 이해할 한두 줄 설명
6. 수정 후 폰 화면(가로 375px)에서 가로 스크롤이 생기지 않는지, 콘솔 오류가 없는지 확인

## 자동 실행 (이 컴퓨터, Windows)
- 작업 스케줄러: 매일 16:00, 20:00 `run_update.bat` / CNN 지수는 `fg_update.bat` 수시 실행
- 주 1회 CDS 뉴스 수집은 Claude 데스크톱 예약 작업(월요일 오전)으로 돌고 있음 — 뉴스에서 `config.py` 의 TICKERS 5곳 + CDS_EXTRA 5곳(메타·알파벳·마이크로소프트·인텔·스페이스X) CDS(bp)를 찾아 출처와 함께 `cds_log.csv` 에 추가 후 `run_update.bat` 실행. 지시문: `%USERPROFILE%\.claude\scheduled-tasks\weekly-cds-news-update\SKILL.md`. 종목별 CDS를 매일 무료로 주는 곳은 없음(유료 데이터) — 사용자가 화면을 주면 `user:` 출처로 추가

## 직접 실행해 보기
```
python update.py        # 데이터 갱신만 (업로드 안 함)
python -m http.server 8765 -d docs   # http://localhost:8765 에서 미리보기
run_update.bat          # 갱신 + GitHub 업로드
```
