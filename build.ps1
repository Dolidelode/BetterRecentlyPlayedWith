# Reproducible Windows build: produces dist\BetterRecentlyPlayedWith.exe
$ErrorActionPreference = "Stop"
python -m pip install -r requirements-dev.txt
python -m pytest -q
# assets\shared_service.json (shared list address + publishable key, not in the repository) is bundled when present
$extra = @()
if (Test-Path assets/shared_service.json) { $extra = @("--add-data", "assets/shared_service.json;assets") }
python -m PyInstaller --noconfirm --onefile --windowed --icon assets/app.ico --add-data "assets/icons;assets/icons" --add-data "assets/twitch_live.png;assets" @extra --name BetterRecentlyPlayedWith run.py
Get-FileHash dist\BetterRecentlyPlayedWith.exe -Algorithm SHA256
