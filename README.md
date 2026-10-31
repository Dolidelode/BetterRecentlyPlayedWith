# Better Recently Played With

A small Windows app that keeps a local history of your Xbox **Recently played with** players and opens
their [SOTREP](https://www.sotrep.com/) page with one click. No server, no installer, no Python needed.

> **Status: early (v0.2.5).** Read "What works and what doesn't" before relying on it.

## What works and what doesn't
| Feature | State |
|---|---|
| Local history (SQLite), search, sorting, unique count, export CSV/JSON | Works, tested |
| Tags (your own labels on players, searchable) and an Encounters view for one or several selected players | Works, tested |
| **Import** gamertags (paste/file) | Works, tested. Marked "Import" in the list |
| **Live** sync via OpenXBL | Uses OpenXBL's documented `GET /v2/recent-players` (third-party service, free key, 150 requests/hour). Parsed from its published spec; **not yet tested against a real account** |
| What OpenXBL's list contains | Your **whole** Xbox Recently played with list (all games), without game names or times. The app can't filter to Sea of Thieves; "Last seen by app" is when the app first saw the player, and "Xbox list #" is the position in Xbox's list (1 = top, presumably the most recent). The default sort is Xbox list order, then players no longer in the list (newest first). Click any column header to sort; click again to reverse |
| Friend highlight | Rows of players who are your friends (you follow each other) get a different colour. Uses your OpenXBL friends list (1 extra request per 30 min), matched by XUID, never by name. Switching account jumps back to the top of the list |
| **Twitch icon** | Experimental. A Twitch icon appears on a player's row only while **Xbox itself** reports an active Twitch broadcast for that account. It checks the 20 most recent players automatically (one OpenXBL request each, max 30 per hour). No channel link or stream title is available, so none is shown. Not yet seen on a real broadcaster. See [docs/TWITCH_RESEARCH.md](docs/TWITCH_RESEARCH.md) |
| **Twitch (optional)** | Save a player's Twitch channel once (copy the name from SOTREP); the app verifies it with Twitch to an exact channel ID and then checks live status for all saved channels in one request every 2 minutes: title, game, viewers, watch link. Needs your own free Twitch application keys, entered once and stored only on your PC. Tested against the real Twitch API |
| SOTREP link | A *search* link (`/search/xbox-<gamertag>`). SOTREP offers no way to get a guaranteed profile URL from a gamertag |
| Updates | Notifies you of a new release. **Update now** downloads it from GitHub, checks it against the release's SHA-256 checksum, swaps the program file and restarts. Nothing installs without your click |

## Using it
1. Download `BetterRecentlyPlayedWith.exe` from [Releases](https://github.com/Dolidelode/BetterRecentlyPlayedWith/releases) and run it. (Windows may warn about an unsigned app.)
2. **Import...** to paste gamertags, or **Add account...** for live sync (below).
3. Double-click a player (or **Open SOTREP**). Select several rows and **Tag...** to label players (for example an alt and its main with the same tag; tags are never added automatically), and **Encounters...** for a summary of the selected players (first/last seen, times seen, name changes, friend status, Xbox's "last online" when known) plus every recorded encounter.
4. Leave it open: it refreshes every 60 s (changeable, minimum 30 s; **Pause** any time).

Your data lives in `%APPDATA%\BetterRecentlyPlayedWith\` (`players.db`, `backups\`, `openxbl_keys.json`), so updating or moving the exe never touches it.

### Connecting (easiest: OpenXBL)
1. Click **Add account...**, sign in at <https://xbl.io> with your Microsoft account (phone verification), copy your API key from the dashboard.
2. Paste it into the app. The app looks up the gamertag and shows it under **Syncing as Xbox account**. Keys are stored in `openxbl_keys.json` in your app-data folder, never in the database.
3. Several accounts: **Add account...** again for each one, pick the active one in the drop-down (switching refreshes at once) or **Remove** it. All accounts feed the same player history.

OpenXBL is a third-party service: it sees your Xbox data when you sign in there. Use it only if you are comfortable with that. Disconnect any time with **Remove**.

## Twitch (optional)
Xbox only reports broadcasts that go through Xbox, so it misses most PC streamers. This section asks Twitch directly, for players whose channel **you** saved:
1. **Twitch setup...** (top right): open the Twitch developer console, register a free application (OAuth redirect `http://localhost`, category Other, client type Confidential), then paste its Client ID and a new Secret into the app. They are stored only in `%APPDATA%\BetterRecentlyPlayedWith\twitch.json` (never in the repository, the program file or the database), and a wrong key is rejected with a clear message.
2. Select **one** player, press **Twitch...** and type the channel (name, `@name` or a twitch.tv link; empty removes it). Twitch resolves it to an exact numeric channel ID, so a renamed channel keeps working and no name is ever guessed.
3. The **Twitch** column shows `LIVE <viewers> - <name>` while the channel is live (with the live icon), or just the name. **Open Twitch** opens the channel; **Check live** also shows game, title and viewers for the selected players.
Checks run by themselves every 2 minutes (100 channels per Twitch request) and stay silent unless something fails. Without keys the rest of the app works as before.

## Shared Twitch list (optional)
One master list of players and their Twitch channels, kept by the list owner and downloaded by every copy of the app. Players in your history get their channel from it automatically (marked ◆); your own links always win. **Shared list... → Send my links** sends the links you made yourself to the owner, who approves or rejects them inside the app before anyone else gets them. What is sent: gamertag, XUID, Twitch channel, an optional name and a random ID of your copy of the app. Owner setup and how it is protected: [docs/SHARED_LIST.md](docs/SHARED_LIST.md).

## Twitch icon
Select players and press **Check live** to ask Xbox about them right now (up to 20 per click, 10 per OpenXBL request); a results window shows what Xbox says for each, including players who are offline or broadcasting somewhere other than Twitch.

A small icon on a row means Xbox reports that player as broadcasting right now (Xbox's own `isBroadcasting` flag; it names the service only when its broadcast record does). It runs by itself every 5 minutes for the 20 most recent players in Xbox's current list, 10 players per request, never guesses from names, spends at most 30 OpenXBL requests per hour, remembers results for a while, and pauses for 15 minutes if OpenXBL rate-limits you. What is and isn't possible is explained in [docs/TWITCH_RESEARCH.md](docs/TWITCH_RESEARCH.md).

## Themes and icon
Nine themes (top right) each pair an icon design with a full colour scheme for the whole window; the choice is remembered. The exe file itself always uses the "Deep teal" icon. The artwork was generated with ChatGPT; it is stylised, not an official Sea of Thieves asset, and this project is not affiliated with or endorsed by Rare, Microsoft, Xbox, OpenXBL or SOTREP. If you reuse the icons, check for yourself that you are comfortable with that.

## Privacy
Everything stays on your PC in `%APPDATA%\BetterRecentlyPlayedWith\`. The app only talks to `api.xbl.io` (your list, using your own key), `api.github.com` and `github.com` (update check, and the download when you click Update now) and opens `sotrep.com` links in your browser. If a shared list is set up, it also downloads that list and sends your links only when you press **Send my links**. No telemetry. Never post `openxbl_keys.json`, `players.db` or export files publicly; the repo's `.gitignore` excludes them.

## Development
```
python -m pytest -q        # tests
python run.py              # run from source
./build.ps1                # build dist\BetterRecentlyPlayedWith.exe
```
CI (`.github/workflows/build.yml`) tests and builds on Windows; pushing a tag `vX.Y.Z` attaches the exe and a SHA-256 file to a release. The hash comes from the same place as the exe, so it detects corruption, not tampering; code signing is not set up yet.

Publish: `git init && git add . && git commit -m "Initial version" && git branch -M main && git remote add origin https://github.com/Dolidelode/BetterRecentlyPlayedWith.git && git push -u origin main`

License: MIT (simple, permissive). Not affiliated with Microsoft, Xbox, Rare or SOTREP.
