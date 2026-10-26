# Research: getting a player's Twitch channel automatically (Xbox linked accounts)

Researched 2026-10-26/27. **Verified** = tested with real accounts or read in primary documentation. **Unverified** = from
a third-party library, a forum, or an AI answer.

## The question
The app knows recent players by Xbox account (XUID). We want their Twitch channel without guessing names, because many
people use different names on Twitch and Xbox.

## What was tested, and the result
| Source | Test | Result |
|---|---|---|
| OpenXBL `/v2/account/{xuid}` | Own account, which has Steam and Discord linked and shown | 16 profile settings, no linked accounts. **Verified.** |
| OpenXBL `/v2/account/{xuid}?settings=...` | Asked for `DisplayedLinkedAccounts` | Ignored; always the same 16 settings. **Verified.** |
| OpenXBL `/v2/player/summary` | 10 real players, plus `decoration=` parameters | `detail`, `socialManager`, `broadcast`... empty; parameters ignored. **Verified.** |
| Xbox PeopleHub directly (own Azure app, device-code sign-in) | Own account, 3 decoration sets incl. `detail` | Works (HTTP 200). `detail` = account tier, bio, blocked, follower/following counts, verified, location, mute, tenure, watermarks. **No linked accounts.** **Verified.** |
| Xbox presence / summary while a PC streamer was live | One player streaming on Twitch from a PC | Xbox said **offline** and not broadcasting: Xbox cannot see PC streams. **Verified.** |
| Twitch API | Docs + Twitch developer forum | No endpoint for a user's connected accounts ([forum](https://discuss.dev.twitch.com/t/twitch-and-xbox-account/28623)). |
| Discord API | [Official docs](https://docs.discord.com/developers/resources/user) | `GET /users/@me/connections` with the `connections` scope returns the signed-in user's connections, and `xbox` and `twitch` are both listed types, each with a `visibility` flag. Only for the user who signs in. **Verified (docs).** |
| SOTREP | Its own public page script | Merges Twitch from its database, from connected accounts and from the player's Discord profile, not from Xbox. **Verified (their code).** |

## The one remaining lead: profile setting `DisplayedLinkedAccounts`
* The [XboxReplay xboxlive-api](https://github.com/XboxReplay/xboxlive-api) library lists 17 profile settings. 16 of them are
  exactly the ones OpenXBL returns. The 17th is **`DisplayedLinkedAccounts`**, and OpenXBL never requests it.
* Microsoft documents the endpoint that serves profile settings:
  [`POST https://profile.xboxlive.com/users/batch/profile/settings`](https://learn.microsoft.com/en-us/gaming/gdk/docs/reference/live/rest/uri/profilev2/uri-usersbatchprofilesettingspost)
  with `x-xbl-contract-version: 2` and a body of `userIds` + `settings`. Microsoft does **not** list `DisplayedLinkedAccounts`;
  the name comes only from XboxReplay. **Unverified.**
* `tools/peoplehub_probe.py` now asks for exactly that, first, with your own sign-in.

### Morning test (about 2 minutes)
```
python tools/peoplehub_probe.py <your-azure-client-id> <your-xuid> --profile-only
```
Approve the code at microsoft.com/link. Look for `PROFILE RESULT`. Run it on **your own** XUID first: you know Steam and
Discord are linked and shown. Without `--profile-only` it also checks Xbox's own recent-players list (counts only).

### What each answer means
* **HAS DATA** (your Steam/Discord appear): Xbox exposes linked accounts that players chose to show. Then test a player you
  know shows Twitch on Xbox. If that works, the app can read Twitch links **for every recent player who chose to show them**,
  in batches, with one Microsoft sign-in per app user (the device-code flow with your client ID; no secret is needed).
* **Empty / not returned**: Xbox does not hand linked accounts to apps like this. Stop looking on the Xbox side.

## If the Xbox route is closed: realistic options
1. **Manual Twitch names (built).** The Twitch section: paste the name once (e.g. copied from SOTREP), verified to an exact
   Twitch ID, then batched live checks. Works today.
2. **Opt-in self-registration via Discord.** A player signs in with Discord once; `/users/@me/connections` gives their own
   `xbox` and `twitch` connections (respecting the visibility flag). That pairing is exact and consented. Catch: Discord's
   login needs a redirect web page, and a *shared* list needs somewhere to store it, so this means hosting something.
3. **Opt-in shared list without a server.** A JSON file in this repository that the app downloads read-only; players add
   **themselves** through a GitHub issue form or pull request, with a documented removal process. No copying from SOTREP
   (their data is login-gated and some players asked to be hidden).
4. **Name matching (rejected).** Twitch `search/channels?query=<gamertag>` is what several AI answers suggest. It misses
   everyone with a different name and can point at the wrong person. If ever used, only as a suggestion the user confirms,
   never as a claim that someone is live.

## Notes
* Xbox also rate limits per service: the profile service answered "60 requests per 300 s" during testing (OpenXBL passes this
  on as HTTP 200 with `code: 429` in the body; the app now handles that).
* Linked accounts only exist for players who linked them **and** switched on "Show icon on my profile".
* The probe keeps tokens in memory only, prints field names, and saves raw data only with `--save-raw`.
