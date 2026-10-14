@echo off
chcp 65001 >nul
setlocal

rem Запуск системы «Склад зеркал» одним файлом (Windows).
rem Требуется установленный и запущенный Docker Desktop.

cd /d "%~dp0"

echo.
echo === Склад зеркал: запуск ===
echo.

where docker >nul 2>nul
if errorlevel 1 (
	echo Docker не найден.
	echo.
	echo 1. Установите Docker Desktop: https://www.docker.com/products/docker-desktop/
	echo 2. Запустите его и дождитесь надписи "Docker Desktop is running".
	echo 3. Запустите этот файл снова.
	echo.
	pause
	exit /b 1
)

docker info >nul 2>nul
if errorlevel 1 (
	echo Docker установлен, но не запущен.
	echo Откройте Docker Desktop, дождитесь "Docker Desktop is running" и повторите.
	echo.
	pause
	exit /b 1
)

if not exist ".env" (
	echo Создаю файл настроек .env из .env.example
	copy /y ".env.example" ".env" >nul
	powershell -NoProfile -Command "$k=[guid]::NewGuid().ToString('N')+[guid]::NewGuid().ToString('N'); (Get-Content '.env') -replace '^SECRET_KEY=.*',('SECRET_KEY='+$k) -replace '^SESSION_COOKIE_SECURE=.*','SESSION_COOKIE_SECURE=0' -replace '^POSTGRES_PASSWORD=.*','POSTGRES_PASSWORD=sklad_local' -replace '^DATABASE_URL=.*','DATABASE_URL=postgresql+psycopg://sklad:sklad_local@postgres:5432/sklad' | Set-Content '.env' -Encoding UTF8"
	echo Ключ подписи сессий сгенерирован.
)

echo Собираю и поднимаю контейнеры (в первый раз это несколько минут)...
docker compose up -d --build
if errorlevel 1 (
	echo.
	echo Не удалось поднять контейнеры. Посмотрите сообщение выше.
	pause
	exit /b 1
)

echo.
echo Жду, пока приложение ответит...
set TRIES=0
:waitloop
set /a TRIES+=1
curl -s -o nul http://localhost/health
if not errorlevel 1 goto ready
if %TRIES% GEQ 60 (
	echo Приложение не ответило за минуту. Логи: docker compose logs app
	pause
	exit /b 1
)
timeout /t 2 /nobreak >nul
goto waitloop

:ready
echo Приложение работает: http://localhost
echo.

docker compose run --rm app python -m app.cli list-users 2>nul | find "admin" >nul
if errorlevel 1 (
	echo Администраторов ещё нет — создадим первого.
	docker compose run --rm app python -m app.cli create-admin
	echo.
	echo Загружаю справочник артикулов из docs\input\TOP.xlsx
	docker compose run --rm app python -m app.cli import-top docs/input/TOP.xlsx
)

echo.
echo === Готово ===
echo Откройте в браузере: http://localhost
echo Остановить: docker compose down
echo.
start "" http://localhost
pause
