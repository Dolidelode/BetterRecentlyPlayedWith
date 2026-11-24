## What's new in 0.4.0

**Linux.** `BetterRecentlyPlayedWith-linux` is the same app for Linux (`chmod +x`, then run it). It finds Sea of Thieves running through Steam's Proton, scrolls with the mouse wheel and keeps its data in `~/.local/share/BetterRecentlyPlayedWith`. On Linux the Microsoft sign-in is kept in a file only your user can read, updates are offered as a link to this page, and the Xbox app / Game Bar account switch isn't there.

**Terminal version.** `brpw-cli.exe` (Windows) and `brpw-cli-linux`: your recently-played list with Twitch channels (yours and the shared master list) and SOTREP links, your accounts, and Discord, in cmd, PowerShell, Windows Terminal or a Linux terminal.
- Double-click `brpw-cli.exe` for a menu that stays open; the first time, a short setup adds your account.
- `brpw-cli help` lists every command; `brpw-cli watch` keeps refreshing; `--json` for scripts.
- The window opens big enough for the list, and the list fits any width (links never get cut off).
- It shares accounts and history with the app.

**Discord.** ☰ → Discord... (or `brpw-cli discord set URL`): paste a webhook address and your list is posted to that channel, each player linked to SOTREP with hours and Twitch (🔴 LIVE). One message is kept up to date instead of a new one each time, nothing is sent when the list hasn't changed, and a gamertag can never ping anyone. Post by hand or after every refresh.

**Fixes.** Closing the app while an extra tab (like Discord) was open could show an error.

Your data and settings carry over; nothing needs to be set up again. The full history of changes is in CHANGELOG.md.
