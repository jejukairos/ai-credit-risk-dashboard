@echo off
rem 미국 장 마감 뒤 갱신: 작업 스케줄러가 한국 05:30·06:30에 부름 -> us_close_check.py 가 마감 20분 뒤 첫 번만 통과시킴
chcp 65001 >nul
cd /d "%~dp0"
python us_close_check.py || exit /b 0
call run_update.bat
