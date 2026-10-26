# Research: "Live now" (Sea of Thieves players streaming on Twitch)

Researched 2026-10-26. **Verified** = fetched or tested by me. **Unverified** = documented elsewhere or assumed.

## Goal and constraints
Show players from the local history who are live on Twitch, with only an OpenXBL key as setup: no Twitch
account linking, no Twitch API key, no hosted backend, no secrets in the exe, no false matches.

## Findings

### OpenXBL
* Its [OpenAPI spec](https://api.xbl.io/openapi.json) has **no Twitch, broadcast or linked-account fields**
  anywhere (searched the whole spec). **Verified.**
* Presence exists as `GET /v2/presence/{xuid}`. I tested it with the user's key on a non-friend from the history:
  it returns `state` (`Offline`) plus `lastSeen {titleName, timestamp}`. Presence of non-friends works, at least
  for offline players. **Verified.**
* **No usable batch presence.** `GET /v2/{xuids}/presence` is documented as comma-separated but returned
  `SerializationError` (HTTP 400) for batches of 3 and 20 XUIDs. So each player costs **one request**. **Verified.**
* Limits: free tier 150 requests/hour, paid from $5/month for 500 ([guide](https://xbl.io/getting-started));
  Microsoft additionally throttles per service (about 10 requests per 15 s for profile/social, see the
  [fine-grained rate limits](https://learn.microsoft.com/en-us/gaming/gdk/docs/services/develop/best-practices/live-fine-grained-rate-limiting?view=gdk-2510#implemented-limits)).
  The app already spends ~60 requests/hour on the recent-players list.
* Arithmetic: checking all ~250 history accounts takes ~250 requests, more than a whole free hour. Polling the
  history continuously is **not possible**.

### Xbox presence
* Microsoft defines a broadcast record in presence with exactly: `broadcastId` ("Id for this broadcast as
  defined by the broadcasting service"), `session` GUID, `provider`, `viewerCount`, `startTime`
  ([XblPresenceBroadcastRecord](https://devdocs.xbox.com/reference/live/xsapi-c/presence_c/structs/xblpresencebroadcastrecord)).
  **There is no Twitch channel name, stream title or URL.** **Verified (documentation).**
* Xbox links streams to Xbox accounts only when the streamer links Xbox and Twitch on Twitch's side
  ([Windows Central](https://windowscentral.com/xbox-live-twitch-stream-notifications-capture-card)).
* **Unverified:** whether OpenXBL passes this record through in the REST JSON, its exact path/field names, and
  whether `broadcastId` equals a Twitch stream id. I could not observe a broadcasting player during testing
  (the only probed player was offline). The parser therefore searches for the documented field names rather than
  assuming a path, and reports nothing unless a Twitch provider is present.

### Twitch
* [Get Streams](https://dev.twitch.tv/docs/api/reference/#get-streams) requires an **app access token or user
  access token**. It can filter by `user_id`, `user_login` or `game_id` and returns title, viewers, login. **Verified.**
* An app token needs the **client secret**; a public desktop app cannot keep a secret, and shipping one violates
  Twitch's rules and the project rule "no secrets in the repo/exe".
* A "Public" Twitch application has no secret and must use the Device Code or Implicit flow, i.e. **the user
  approves the app once in a browser** ([Twitch forum](https://discuss.dev.twitch.com/t/auth-flow-for-native-application-without-a-backend-server/64539)).
  That is an extra setup step, which the requirements exclude.
* Anything that holds a secret on the user's behalf would be a hosted backend, which the project excludes.
* Twitch's website uses an undocumented GraphQL endpoint that needs no sign-in. Rejected: unofficial, unstable
  and outside Twitch's documented API terms.

### Matching gamertags to Twitch channels
* Xbox profile data exposed through OpenXBL has no linked-account field. **Verified (spec).**
* Matching by name (gamertag vs Twitch login) would create false positives (common names, impersonation, changed
  gamertags) and is **not done**.
* [SOTREP](https://www.sotrep.com/) clearly links Xbox, Twitch and Discord identities, but has no public API; its
  guest API is CAPTCHA-protected and its terms forbid overloading it. Not used.
  **Checked again on request (2026-10-26)** by fetching a public profile page
  (`https://www.sotrep.com/search/<id>`): the *Social Links* panel (where a Twitch account would appear) shows
  "Log in to unlock this info", and searching needs a "Quick Security Check" first. So the Twitch link is only visible
  to a signed-in SOTREP user in a browser. An app could only read it by getting past that check or by handling the
  user's SOTREP login. Both are ruled out (no bypassing bot checks, no collecting credentials). The existing
  **Open SOTREP** button already lets a signed-in user see it in their own browser.

## Feasibility
| Requirement | Possible with OpenXBL key only? |
|---|---|
| Confirm a player is broadcasting on Twitch **according to Xbox** | Only if OpenXBL passes the presence broadcast record through (unverified) |
| Check the whole history automatically | **No** (one request per player, 150/hour) |
| Streamer name | Gamertag only |
| Stream title | **No** (needs Twitch API) |
| Twitch channel / watch link | **No** (needs Twitch API plus a reliable Xbox-to-Twitch id mapping) |
| Viewer count | Yes, from the Xbox record, if present |
| No false matches | Yes (nothing is inferred from names) |

## What was implemented
A background check (shown as a Twitch icon on the player's row; there is no separate tab) that checks only the top `LIVE_SCOPE = 20` players of Xbox's current recent-players list (not the
whole history), within a budget of `HOURLY_BUDGET = 30` single-player presence requests per hour (20% of the free
tier). Never-checked players first, then stale ones (online players re-checked after 10 min, offline after 3 h). Results are cached in SQLite (`live_status`). A player is shown
only if the fresh (<20 min) presence record contains a broadcast whose provider is Twitch; otherwise nothing is
claimed. HTTP 429 pauses checks for 15 minutes; a rejected key asks the user to re-add it. Limits and the missing
channel/title are noted in the README.

## Not implemented, and options
1. **One-time Twitch approval (Device Code flow, public client, no secret).** Would allow Get Streams
   (`game_id` = Sea of Thieves) to be listed with titles/links, **but** it still needs a reliable Xbox-to-Twitch link.
   It only becomes exact if `broadcastId` turns out to be the Twitch stream id (then a stream listing could be matched
   by id with no guessing). Excluded now because it adds a setup step; worth testing once a broadcasting player's
   presence record has been observed.
2. A hosted service holding a Twitch secret: excluded (backend).
3. Wait for SOTREP or OpenXBL to publish linked-account/stream data. The realistic route is to ask the SOTREP owner for an
   official, documented endpoint (for example: XUIDs in, linked Twitch channel out, with an app key), which would make a
   one-request lookup for the latest 20 players possible without scraping.

## Update: batched summary with `isBroadcasting` (verified 2026-10-26)
Testing OpenXBL's `GET /v2/player/summary/{xuids}` with a real key showed:
* each person record has `isBroadcasting` (bool), `presenceState`, `presenceText` (localised, e.g. what they play or
  "49m ago last seen"), `presenceDevices`, and a separate `broadcast` field. No linked/social accounts, no Twitch name.
* it takes **comma-separated XUIDs: 3 and 10 work; 15 and 20 fail** upstream with "Bad Request - Invalid URL". The app
  uses batches of 10, so the 20 most recent players cost **2 requests** (previously 20 single presence requests).
* The earlier finding that the legacy batch *presence* endpoint fails (`SerializationError`) is unchanged.
* Still **unverified**: what a true broadcast record contains (provider, viewers, start time) and whether the service
  is Twitch. Until a real broadcaster is seen, a player is labelled "broadcasting per Xbox" and only called Twitch when
  the broadcast record says so. Profile (`/v2/account/{xuid}`) returns 16 settings, none about linked accounts.

## Correction: Xbox DOES have linked accounts (2026-10-26)
Earlier text said Xbox "seems not to share" linked accounts. That was wrong. Xbox has a **Linked accounts** setting (Discord,
Facebook, Reddit, Steam, Twitch) with a per-account "Show icon on my profile" switch, and other players can see those icons in the
Xbox app / Game Bar ([Xbox support guide, not official Microsoft page](https://todmccoy.com/wp-content/uploads/2025/11/Link-your-social-network-accounts-to-Xbox-FINAL-Tod-McCoy.docx);
[OnMSFT](https://onmsft.com/?p=219042)). SOTREP most likely reads Twitch from there.
What I verified: **OpenXBL does not relay it.** In `/v2/player/summary` the Xbox "decoration" slots (`detail`, `socialManager`,
`broadcast`, `follower`, ...) are empty for 10 real players, `/v2/account/{xuid}` has 16 profile settings and none is a linked
account, and the query parameters `decoration=` / `decorations=` are ignored. I could not find Microsoft documentation for the
Xbox endpoint or field name that carries linked accounts, so that is **unverified**; the next step is to capture the real response
(browser dev tools on xbox.com while signed in) or ask the OpenXBL developer to expose it.
Players only appear with a Twitch link if they linked it AND enabled "Show icon on my profile".

## Update: broadcaster details via single presence (2026-10-26)
When the batched summary says a player is broadcasting but its `broadcast` record does not name the service, the app
makes one extra `GET /v2/presence/{xuid}` for that player only, to read the broadcast record (service, viewers, start
time). The result is cached with the player for the 20-minute freshness window, so a long broadcast costs one request, not
one per cycle. In the automatic cycle it respects the hourly budget; **Check live** is an explicit action and is not blocked.
If presence fails or has no record, the player stays "broadcasting per Xbox (service not stated)". The presence record's
REST shape is still **unverified** against a real broadcaster.

## Implemented: optional Twitch section (2026-10-26)
Findings that shaped it: a PC streamer who was live on Twitch showed as **offline** in Xbox presence
and summary, so Xbox cannot see such streams; SOTREP's own page script merges Twitch links from its database, from
connected accounts and from the player's **Discord profile**, not from Xbox linked accounts; Twitch's Get Streams needs an
application token. So the app now has an opt-in section: the user enters their own Twitch client ID/secret once (local file
only), saves a Twitch channel per player (verified through Twitch Get Users to an exact numeric ID, which SOTREP also shows),
and live status comes from Get Streams in batches of 100. Verified end to end with a real Twitch application: the channel
resolved to the same numeric ID SOTREP shows, and Get Streams reported it live with title, game and viewers.
Limits: the Twitch channel must still be supplied by the user (copied from SOTREP); the app cannot discover it.
