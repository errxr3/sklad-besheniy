@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Останавливаю систему «Склад зеркал»...
docker compose down
echo Готово. Данные сохранены, повторный запуск — start.bat
pause
