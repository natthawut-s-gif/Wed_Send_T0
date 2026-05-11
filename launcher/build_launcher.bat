@echo off
setlocal
cd /d "%~dp0\.."

pyinstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name WedSendT0Launcher ^
  --collect-all cv2 ^
  --collect-all fitz ^
  --collect-all numpy ^
  --collect-all PIL ^
  launcher\wed_send_t0_launcher.py

echo.
echo Build complete.
echo Output: dist\WedSendT0Launcher.exe
