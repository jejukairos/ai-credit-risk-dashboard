@echo off
rem 매일 자동 실행되는 파일: 데이터 갱신 -> GitHub에 올리기
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
echo ===== %date% %time% ===== >> logs\update.log
python update.py >> logs\update.log 2>&1
echo python exit code %errorlevel% >> logs\update.log
git add docs data cds_log.csv dram_log.csv arr_log.csv >> logs\update.log 2>&1
git commit -m "자동 갱신 %date%" >> logs\update.log 2>&1
git push origin main >> logs\update.log 2>&1
echo push exit code %errorlevel% >> logs\update.log
