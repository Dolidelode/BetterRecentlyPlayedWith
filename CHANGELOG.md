# Changelog

## 0.4.0 — 2026-11-24
**New**
- **Linux version** (`BetterRecentlyPlayedWith-linux`): the same app on Linux. Finds Sea of Thieves running through Steam's Proton, scrolls with the mouse wheel (X11 and XWayland), keeps data in `~/.local/share/BetterRecentlyPlayedWith` (or `$XDG_DATA_HOME`). The Microsoft sign-in is kept in a file only your user can read (Windows encrypts it per Windows user instead); updates are offered as a link to the release page; the Xbox app / Game Bar account switch is Windows-only.
- **Terminal version** (`brpw-cli.exe`, `brpw-cli-linux`): the recently-played list with Twitch channels (own links and the shared master list) and SOTREP links; add, switch and remove accounts; `watch` keeps refreshing; `--json` for scripts; Discord commands. Double-clicked on Windows it opens a menu that stays open, with a first-use setup; `brpw-cli help` lists every command. The console window opens big enough for the list and the list fits any width (SOTREP links move to their own line rather than being cut off).
- **Discord webhook** (☰ → Discord..., or `brpw-cli discord set URL`): posts the list to a channel, each player linked to SOTREP with hours, Friend and Twitch (🔴 LIVE). Keeps one message up to date by editing it, sends nothing when the list hasn't changed, never pings anyone; post by hand or after every refresh; 5–30 players. The address is stored only in `discord.json`.
- **Builds:** GitHub now builds and tests on Windows and Linux; a release has four programs and one `SHA256SUMS.txt`.

**Fixed**
- Closing the app while an extra tab (like Discord) was open could raise an error.
- The keyboard's menu key binding crashed the app at start on Linux (it's called `Menu` there, `App` on Windows).

## 0.3.0 — 2026-11-24
- **One window with tabs**: Players, Friends & messages, Live streams and Live now; chats, Encounters, Import, Add account, Twitch setup and the shared list open as closable tabs. Switching tabs never reloads; questions appear inside the window instead of message boxes.
- **Slim window** down to about 480 pixels: rows wrap, tab names shorten, Friends & messages shows the list or the chat.
- **List views** Cards / Compact / Rows on Players, Live streams and Live now; search, sort and filters can be hidden; views, sorting and filters are remembered.
- **Friends & messages**: chats, friends and friend requests in one tab (opens on Friends); accept, ignore or cancel requests, pop-up for new ones; the real Xbox friend list; Add friend sends a real friend request; draggable list width.
- **Your crew**: who is on your ship and on friends' ships; people you sail with in two or more sessions are tagged crew and hidden (Crew hidden / Crew shown).
- **Live streams**: Twitch profile pictures; click a stream to unfold it with Twitch's live snapshot and Watch on Twitch.
- **Stays small**: players not seen for 2 weeks are forgotten (tags, unshared Twitch links and the shared list are kept).
- Friends are a slightly darker card instead of a colour; sent friend requests no longer show as received; Invite / join in Game Bar removed (Sea of Thieves doesn't allow joining or inviting from another program).

## 0.2.12 — 2026-11-23
- Message inbox: faster, mute / unmute / delete chats and messages, messages the sender deleted, Xbox profile pictures, confirmations inside the window.
- Player cards instead of a table, with profile pictures and hours played in Sea of Thieves; pictures for your own accounts.
- Switch Game Bar to this account signs the Xbox app out through its own menu and picks the account's email in Microsoft's picker.

## 0.2.11 — 2026-11-15
- Sending Xbox messages through Microsoft's documented send service (Xbox stopped delivering the old way); a pop-up for new messages; muted chats stay quiet; SOTREP from a chat.

## 0.2.9 – 0.2.10 — 2026-11-15
- Sign in with Microsoft: the app talks to Xbox directly instead of through an OpenXBL key.
- Messages window with every conversation; Xbox friends window with player search; themed title bars and one top bar with a ☰ menu.
- Auto-refresh mode "While Xbox says I play SoT" (console sessions count too).

## 0.2.8 and earlier
- Local history of Xbox's Recently played with list, search, sorting, tags, encounters, import and export.
- Optional Twitch: linked channels with live status, Live streams of Sea of Thieves, Check live, and the shared Twitch master list.
- Themes, in-app updates checked against the release's SHA-256 checksum.
- See the git history for details.
