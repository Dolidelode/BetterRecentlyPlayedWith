# Better Recently Played With

A small Windows app that keeps a local history of your Xbox **Recently played with** players and opens
their [SOTREP](https://www.sotrep.com/) page with one click. No server, no installer, no Python needed.

> **Status: early (v0.2.11).** Read "What works and what doesn't" before relying on it.

## What works and what doesn't
| Feature | State |
|---|---|
| Local history (SQLite), search, sorting, unique count, export CSV/JSON | Works, tested |
| Tags (your own labels on players, searchable) and an Encounters view for one or several selected players | Works, tested |
| **Import** gamertags (paste/file) | Works, tested. Marked "Import" in the list |
| **Live** sync | Your Xbox **Recently played with** list, refreshed by itself. Two ways to connect: **Sign in with Microsoft** (recommended; the app asks Xbox directly) or an OpenXBL key (third-party service, free key, 150 requests/hour). Both tested against a real account |
| What the list contains | With **Microsoft sign-in**, Xbox gives the game and the time you last played together, so the list is **only Sea of Thieves** and every encounter has its **real time**. With an OpenXBL key the list covers **all games**, without game names or times: "Last seen" is then when the app first saw the player. "Xbox list #" is the position in Xbox's list (1 = top). The default sort is Xbox list order, then players no longer in the list (newest first). Click any column header to sort; click again to reverse |
| Friend highlight | Rows of players who are your friends (you follow each other) get a different colour. Uses your Xbox friends list (1 extra request per 30 min, and refreshed whenever you open **Friends**), matched by XUID, never by name. Switching account jumps back to the top of the list |
| **Twitch icon** | Experimental. A Twitch icon appears on a player's row only while **Xbox itself** reports an active Twitch broadcast for that account. It checks the 20 most recent players automatically (10 players per request, at most 30 requests per hour). No channel link or stream title is available, so none is shown. Not yet seen on a real broadcaster. See [docs/TWITCH_RESEARCH.md](docs/TWITCH_RESEARCH.md) |
| **Twitch (optional)** | Save a player's Twitch channel once (copy the name from SOTREP); the app verifies it with Twitch to an exact channel ID and then checks live status for all saved channels in one request every 2 minutes: title, game, viewers, watch link. Needs your own free Twitch application keys, entered once and stored only on your PC. Tested against the real Twitch API |
| **Message inbox** | **Messages** (top bar): all your Xbox conversations, newest first, with the last message, the time, **bold for unread** and ● for who is online; the chat opens on the right. Typing filters by name, Enter finds anyone on Xbox to start a new chat. A **new message pops up** at the bottom right of the screen (click it to open that chat) and **Messages** shows how many unread chats you haven't opened yet; checked once a minute, can be turned off under **☰ → Pop-up for new Xbox messages**. Needs an account signed in with Microsoft (OpenXBL's inbox doesn't work). Group chats aren't shown yet. Tested against a real inbox (45 conversations) |
| **Xbox friends and messages** | **Friends** (top bar): your Xbox friends in Online / Offline sections, what each is doing or when they were last seen, ★ for favourites. Typing filters them; **Enter searches all of Xbox** to add or message anyone. Buttons (and right-click): **Messages** (a chat window with your history as bubbles), **Invite / join in Game Bar**, **Xbox profile** (opens the Xbox app, or xbox.com), **SOTREP** and **Add friend / Remove friend**. In the main list, right-click a player for the same. One request per click, never automatic. There is no API to invite someone to a game or join their game: Xbox only allows that in its own apps, so **Invite / join in Game Bar** opens Game Bar's Xbox Social widget and copies the gamertag; there you right-click the player for **Invite to game** / **Join game** while you play. **☰ → Switch Game Bar to this account** opens the Xbox app's sign-in screen (and copies the gamertag), so Game Bar uses that account; if Windows can't open it, the Xbox app is started normally. Messages are at most 250 characters. With **Microsoft sign-in**, sending uses Microsoft's documented Xbox send service and was tested with a real message between two accounts; with an OpenXBL key it goes through OpenXBL (not tested with a real message). Friend list, search, message history and the inbox tested against a real account; adding and removing friends only against a fake server |
| SOTREP link | A *search* link (`/search/xbox-<gamertag>`). SOTREP offers no way to get a guaranteed profile URL from a gamertag |
| Updates | Notifies you of a new release. **Update now** downloads it from GitHub, checks it against the release's SHA-256 checksum, swaps the program file and restarts. Nothing installs without your click |

## Using it
1. Download `BetterRecentlyPlayedWith.exe` from [Releases](https://github.com/Dolidelode/BetterRecentlyPlayedWith/releases) and run it. (Windows may warn about an unsigned app.)
2. **☰ → Import players...** to paste gamertags, or **☰ → Add Xbox account...** for live sync (below).
3. Double-click a player to open SOTREP. Right-click a player (or several selected rows) for everything else, for example **Tag...** to label players (for example an alt and its main with the same tag; tags are never added automatically) and **Encounters** for a summary of the selected players (first/last seen, times seen, name changes, friend status, Xbox's "last online" when known) plus every recorded encounter.
4. Leave it open: it refreshes every 60 s (changeable next to **Auto**, minimum 30 s; set **Auto** to Off any time). **Auto** decides when: *Only while SoT is open* (the game runs on this PC), *While Xbox says I play SoT* (your own Xbox presence, so playing on a **console** counts too; Xbox is asked at most every 2 minutes), *Always* or *Off*. The first round after you stop playing still runs, so the players of your last session are picked up. With *Appear offline* on Xbox, Xbox reports you as offline, so use one of the other choices then.

Your data lives in `%APPDATA%\BetterRecentlyPlayedWith\` (`players.db`, `backups\`, `ms_accounts.json`, `openxbl_keys.json`), so updating or moving the exe never touches it.

### Connecting
**Sign in with Microsoft (recommended)**
1. Click **☰ → Add Xbox account...** → **Sign in with Microsoft**. Microsoft's own sign-in page opens in your browser: choose the Xbox account and sign in there. The app never sees your password.
2. The account appears in the account drop-down at the top left and the app talks to Xbox directly: no xbl.io account, no key and no third party in between.
3. The sign-in is saved in `ms_accounts.json` in your app-data folder, **encrypted with Windows** (only your Windows user on this PC can read it) and never in the database. **☰ → Remove this account** forgets it; you can also revoke the app at <https://account.live.com/consent/Manage>.

**Or: an OpenXBL key**
1. Click **☰ → Add Xbox account...** → **Use an OpenXBL key instead...**, sign in at <https://xbl.io> with your Microsoft account (phone verification) and copy your API key from the dashboard.
2. Paste it into the app. Keys are stored in `openxbl_keys.json` in your app-data folder, never in the database. OpenXBL is a third-party service: it sees your Xbox data when you sign in there.

Several accounts: **☰ → Add Xbox account...** again for each one (both kinds can be mixed), pick the active one in the drop-down at the top left (switching refreshes at once) or **☰ → Remove this account**. All accounts feed the same player history.

## Twitch (optional)
Xbox only reports broadcasts that go through Xbox, so it misses most PC streamers. This section asks Twitch directly, for players whose channel **you** saved:
1. **☰ → Twitch setup...**: open the Twitch developer console, register a free application (OAuth redirect `http://localhost`, category Other, client type Confidential), then paste its Client ID and a new Secret into the app. They are stored only in `%APPDATA%\BetterRecentlyPlayedWith\twitch.json` (never in the repository, the program file or the database), and a wrong key is rejected with a clear message.
2. Right-click **one** player → **Link Twitch channel...** and type the channel (name, `@name` or a twitch.tv link; empty removes it). Twitch resolves it to an exact numeric channel ID, so a renamed channel keeps working and no name is ever guessed.
3. The **Twitch** column shows `LIVE <viewers> - <name>` while the channel is live (with the live icon), or just the name. Click the Twitch cell (or right-click → **Open Twitch**) to open the channel; **Check live** (top bar) lists everyone who is live right now, with game, title and viewers. **Live streams** (top bar) shows everyone streaming Sea of Thieves on Twitch, to link a streamer you just met.
Checks run by themselves every 2 minutes (100 channels per Twitch request) and stay silent unless something fails. Without keys the rest of the app works as before.

## Shared Twitch list (optional)
One master list of players and their Twitch channels, kept by the list owner and downloaded by every copy of the app. Players in your history get their channel from it automatically (marked ◆); your own links always win. **☰ → Shared Twitch list... → Send my links** sends the links you made yourself to the owner, who approves or rejects them inside the app before anyone else gets them. What is sent: gamertag, XUID, Twitch channel, an optional name and a random ID of your copy of the app. Owner setup and how it is protected: [docs/SHARED_LIST.md](docs/SHARED_LIST.md).

## Twitch icon
Press **Check live** (top bar) to see who is live right now among the players with source Live: every player linked to a Twitch channel is asked on Twitch (100 per request), and Xbox is asked about the 30 most recently seen (3 requests). Only those who are live are listed. For specific players, select them and choose **Check live** in the right-click menu (up to 20); that window also shows who is offline or broadcasting somewhere other than Twitch.

A small icon on a row means Xbox reports that player as broadcasting right now (Xbox's own `isBroadcasting` flag; it names the service only when its broadcast record does). It runs by itself every 5 minutes for the 20 most recent players in Xbox's current list, 10 players per request, never guesses from names, spends at most 30 requests per hour, remembers results for a while, and pauses for 15 minutes if it is rate-limited. What is and isn't possible is explained in [docs/TWITCH_RESEARCH.md](docs/TWITCH_RESEARCH.md).

## Themes and icon
Nine themes (**☰ → Theme**) each pair an icon design with a full colour scheme for the whole window, title bars included (Windows 11); the choice is remembered. The exe file itself always uses the "Deep teal" icon. The artwork was generated with ChatGPT; it is stylised, not an official Sea of Thieves asset, and this project is not affiliated with or endorsed by Rare, Microsoft, Xbox, OpenXBL or SOTREP. If you reuse the icons, check for yourself that you are comfortable with that.

## Privacy
Everything stays on your PC in `%APPDATA%\BetterRecentlyPlayedWith\`. The app only talks to Microsoft's sign-in (`login.microsoftonline.com`) and Xbox's own services (`*.xboxlive.com`) for accounts signed in with Microsoft, or to `api.xbl.io` for accounts with an OpenXBL key, `api.github.com` and `github.com` (update check, and the download when you click Update now) and opens `sotrep.com` links in your browser. If a shared list is set up, it also downloads that list and sends your links only when you press **Send my links**. No telemetry. Never post `ms_accounts.json`, `openxbl_keys.json`, `players.db` or export files publicly; the repo's `.gitignore` excludes them.

## Development
```
python -m pytest -q        # tests
python run.py              # run from source
./build.ps1                # build dist\BetterRecentlyPlayedWith.exe
```
CI (`.github/workflows/build.yml`) tests and builds on Windows; pushing a tag `vX.Y.Z` attaches the exe and a SHA-256 file to a release. The hash comes from the same place as the exe, so it detects corruption, not tampering; code signing is not set up yet.

License: MIT (simple, permissive). Not affiliated with Microsoft, Xbox, Rare or SOTREP.
