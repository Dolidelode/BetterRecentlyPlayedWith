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
A **Live now** tab that checks only the top `LIVE_SCOPE = 20` players of Xbox's current recent-players list (not the
whole history), within a budget of `HOURLY_BUDGET = 30` single-player presence requests per hour (20% of the free
tier). Never-checked players first, then stale ones (online players re-checked after 10 min, offline after 3 h). Results are cached in SQLite (`live_status`). A player is shown
only if the fresh (<20 min) presence record contains a broadcast whose provider is Twitch; otherwise nothing is
claimed. HTTP 429 pauses checks for 15 minutes; a rejected key asks the user to re-add it. Limits and the missing
channel/title are stated in the tab.

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
