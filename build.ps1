# Reproducible Windows build: produces dist\BetterRecentlyPlayedWith.exe
$ErrorActionPreference = "Stop"
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m PyInstaller --noconfirm --onefile --windowed --icon assets/app.ico --add-data "assets/icons;assets/icons" --add-data "assets/twitch_live.png;assets" --name BetterRecentlyPlayedWith run.py
Get-FileHash dist\BetterRecentlyPlayedWith.exe -Algorithm SHA256
