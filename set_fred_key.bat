@echo off
rem FRED API 키를 이 컴퓨터에만 저장합니다 (.env 파일, GitHub에는 올라가지 않음)
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo FRED 웹사이트에서 받은 API 키(32자리 영문+숫자)를 붙여넣고 Enter를 누르세요.
echo (붙여넣기: 마우스 오른쪽 클릭)
set /p KEY=키:
if "%KEY%"=="" (
  echo 입력이 없어 취소했습니다.
  pause
  exit /b
)
> .env echo FRED_API_KEY=%KEY%
echo.
echo 저장 완료! 이제 이 창을 닫아도 됩니다.
pause
