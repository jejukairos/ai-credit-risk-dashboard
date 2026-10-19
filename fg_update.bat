@echo off
rem 30분마다 실행: CNN 공포·탐욕 지수가 바뀌었을 때만 GitHub에 올리기
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
echo ===== %date% %time% ===== >> logs\fg.log
python fg_update.py >> logs\fg.log 2>&1
if not "%errorlevel%"=="2" goto :eof
git add docs data >> logs\fg.log 2>&1
git commit -m "공포탐욕 지수 갱신 %date% %time:~0,5%" >> logs\fg.log 2>&1
git push origin main >> logs\fg.log 2>&1
echo push exit code %errorlevel% >> logs\fg.log
