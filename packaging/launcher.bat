@echo off
chcp 65001 >nul
title Кадровый агент
cd /d "%~dp0"
if not exist "python\python.exe" (
  echo Сначала распакуйте архив целиком, затем запустите этот файл из распакованной папки.
  pause
  exit /b 1
)
set PYTHONUTF8=1
set "TA_DATA_DIR=%~dp0data"
set "TA_MODELS_DIR=%~dp0data\models"
set TA_OPEN_BROWSER=1
set HF_HUB_OFFLINE=1
echo Кадровый агент запускается. Первый запуск занимает до минуты.
"python\python.exe" -m app.main
pause
