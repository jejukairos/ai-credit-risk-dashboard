@echo off
rem 미국 장 마감 뒤 갱신: 작업 스케줄러가 한국 금·토 05:30·06:30, 일~목 07:30 + 컴퓨터 로그인·잠금 해제 2분 뒤에 부름 -> us_close_check.py 가 마감 20분 뒤 첫 번만 통과시킴
chcp 65001 >nul
cd /d "%~dp0"
python us_close_check.py || exit /b 0
rem 아침 갱신 표시: update.py 가 밤사이 안 바뀌는 한국 이익 전망(약 9분)을 건너뜀
set US_CLOSE=1
call run_update.bat
