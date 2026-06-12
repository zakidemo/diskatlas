@echo off
REM ============================================================
REM  DiskAtlas - build DiskAtlas.exe (one click)
REM  Requirements: Python 3.10+ installed and on PATH
REM ============================================================
echo.
echo [1/3] Installing PyInstaller...
python -m pip install --quiet --upgrade pyinstaller
if errorlevel 1 (
    echo ERROR: pip failed. Is Python on your PATH?
    pause & exit /b 1
)

echo [2/3] Building single-file executable...
REM  "python -m PyInstaller" works even when the Scripts folder
REM  is not on PATH (the most common Windows setup issue).
python -m PyInstaller --onefile --windowed --name DiskAtlas diskatlas_app.py
if errorlevel 1 (
    echo ERROR: build failed.
    pause & exit /b 1
)

echo [3/3] Done!
echo.
echo   Your executable:  %CD%\dist\DiskAtlas.exe
echo   You can copy this single file anywhere - no Python needed to run it.
echo.
pause
