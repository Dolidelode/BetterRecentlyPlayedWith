# Reproducible Windows build: produces dist\BetterRecentlyPlayedWith.exe and dist\brpw-cli.exe (terminal version)
$ErrorActionPreference = "Stop"
python -m pip install -r requirements-dev.txt
python -m pytest -q
# assets\shared_service.json (shared list address + publishable key, not in the repository) is bundled when present
$extra = @()
if (Test-Path assets/shared_service.json) { $extra = @("--add-data", "assets/shared_service.json;assets") }
python -m PyInstaller --noconfirm --onefile --windowed --icon assets/app.ico --add-data "assets/icons;assets/icons" --add-data "assets/twitch_live.png;assets" --exclude-module numpy @extra --name BetterRecentlyPlayedWith run.py
# the terminal version (Windows cmd / PowerShell / Terminal)
python -m PyInstaller --noconfirm --onefile --console --icon assets/app.ico --exclude-module numpy @extra --name brpw-cli run_cli.py
Get-FileHash dist\BetterRecentlyPlayedWith.exe, dist\brpw-cli.exe -Algorithm SHA256
