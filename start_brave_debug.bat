@echo off
echo Closing existing Brave instances...
taskkill /F /IM brave.exe 2>nul
timeout /t 2 /nobreak >nul

echo Starting Brave with remote debugging on port 9222...
start "" "C:\Users\saini\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe" ^
  --remote-debugging-port=9222 ^
  --no-first-run ^
  --restore-last-session

echo.
echo Brave is running with debug port 9222.
echo You can now run: python cricket_bot.py
echo.
pause
