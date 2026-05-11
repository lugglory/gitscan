@echo off
setlocal enabledelayedexpansion

set "GITSCAN_DIR=%~dp0"
if "!GITSCAN_DIR:~-1!"=="\" set "GITSCAN_DIR=!GITSCAN_DIR:~0,-1!"

echo [1/3] Creating virtual environment...
python -m venv "%GITSCAN_DIR%\.venv"
if errorlevel 1 (
    echo Error: failed to create venv. Is Python installed and in PATH?
    pause
    exit /b 1
)

echo [2/3] Installing dependencies...
"%GITSCAN_DIR%\.venv\Scripts\pip" install -r "%GITSCAN_DIR%\requirements.txt"
if errorlevel 1 (
    echo Error: pip install failed.
    pause
    exit /b 1
)

echo [3/3] Adding to PATH...
powershell -NoProfile -Command ^
    "$dir = '%GITSCAN_DIR%\scripts';" ^
    "$cur = [Environment]::GetEnvironmentVariable('Path', 'User');" ^
    "if ($cur -split ';' | Where-Object { $_ -ieq $dir }) {" ^
    "    Write-Host 'Already in PATH, skipping.'" ^
    "} else {" ^
    "    $sep = if ($cur -and -not $cur.EndsWith(';')) { ';' } else { '' };" ^
    "    [Environment]::SetEnvironmentVariable('Path', $cur + $sep + $dir, 'User');" ^
    "    Write-Host 'Added to user PATH.'" ^
    "    Write-Host 'Open a new terminal for changes to take effect.'" ^
    "}"

echo.
echo Done! Run 'gitscan' from any git repository.
pause
