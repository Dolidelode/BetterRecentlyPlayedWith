# Research: Xbox "Recently played with" and SOTREP linking

Researched 2026-10-26. Evidence is labelled **verified** (seen in a primary source or fetched myself) or
**unverified** (recalled from community knowledge, could not be confirmed here).

## Repository state
`RafaelOudshoorn/BetterRecentlyPlayedWith` was empty (no files, license or releases) when inspected
([repo](https://github.com/RafaelOudshoorn/BetterRecentlyPlayedWith)). Nothing to preserve.

## 1. Xbox recent players

| Question | Finding |
|---|---|
| Official public read API? | **None found.** Microsoft's Multiplayer Activity API only lets a *title* **write** recent-player entries ([MPA overview](https://devdocs.xbox.com/services/xbox-services/multiplayer/mpa/live-mpa-overview.md)). It requires a game's own service config, so it is not usable by an ordinary desktop app. |
| Documented `social.xboxlive.com` People API | Documents friends/followed users only (`/users/{owner}/people/...`), not recent players ([ref](https://devdocs.xbox.com/reference/live/rest/uri/people/uri-usersowneridpeopletargetidget)). |
| Maintained library | [OpenXbox/xbox-webapi-python](https://github.com/OpenXbox/xbox-webapi-python) (MIT) exposes friends, summaries, recommendations, presence. Its [PeopleProvider docs](https://xbox-webapi-python.readthedocs.io/en/stable/source/xbox.webapi.api.provider.people/) have **no recent-players method**. It also needs an Azure app registration and a client secret. |
| Unofficial `peoplehub` endpoint | `peoplehub.xboxlive.com` is a real host used by the Xbox app and community libraries for friends/presence/search ([openpak/xbl](https://github.com/openpak/xbl), [den.dev](https://den.dev/blog/convert-gamertag-to-xuid/)). A `/users/me/people/recentplayers` route is what the Xbox app is believed to call. **Unverified**: no primary source or public library documents it, and I cannot authenticate in this environment to test it. |
| Local data from the Xbox app / OS | None found. |
| Third-party (xboxapi.com, OpenXBL) | Not recent-players sources; they require third-party accounts/keys and are outside the "no backend" goal. |

**Distinctions:** recently *played games*, friends/followers, presence, MPA activity feeds and the
*Recently played with* list are different datasets. Only the last is wanted, and no documented API reads it.

### Feasibility
* Automatic retrieval: **possible but unproven.** Must use an undocumented endpoint with the user's own Xbox
  token. It may return a different shape, need different headers, be gated, or change without notice. The
  response format used in this project is therefore parsed defensively and clearly labelled experimental.
* Manual import: **fully feasible and reliable.** The user pastes gamertags (optionally `gamertag,xuid`) or a
  JSON export. Imported data is stored with `source = import`, never shown as live.

### OpenXBL (added later)
[OpenXBL](https://xbl.io) publishes an OpenAPI spec ([openapi.json](https://api.xbl.io/openapi.json)) with `GET /v2/recent-players` ("Returns players you have recently played with"), `X-Authorization` API key, items with `xuid`, `gamertag`, `titleName`, `timestamp`. **Verified from the spec only**; not tested with a real key. Tradeoffs: third-party service sees the user's Xbox data, free tier 150 requests/hour (the [getting-started guide](https://xbl.io/getting-started)), phone verification, service may change or stop. It is now the primary live provider since it needs no Azure setup.

### Decision: direct Azure/Xbox sign-in removed
The direct route (Microsoft device-code sign-in, then the undocumented `peoplehub` `recentplayers` URL) was implemented, then **removed**: it needed an Azure app registration that ordinary personal accounts can't easily create (AADSTS50020 without a tenant), could not be verified, and OpenXBL gives the same data through a documented endpoint. Revisit only if OpenXBL becomes unavailable.

### Recommended data source
Provider interface with two providers:
1. `ManualImportProvider` (works today, no auth).
2. `XboxLiveProvider` (experimental): Microsoft device-code sign-in -> Xbox Live user token -> XSTS token
   (`rp://http://xboxlive.com`) -> `GET https://peoplehub.xboxlive.com/users/me/people/recentplayers`
   (`x-xbl-contract-version: 1`). Responses with an unrecognised shape raise a clear error; nothing is invented.

### Authentication and setup
* Device-code flow needs an **Azure app client ID** (public client, "Personal Microsoft accounts", device-code
  flow enabled). A client ID is not a secret, but the project cannot ship one on the maintainer's behalf
  without registering it. The ID is a setting (`azure_client_id`); `README.md` explains the one-time
  registration. No passwords are ever seen by the app; sign-in happens on microsoft.com.
* Tokens are kept in the per-user app-data folder (`auth.json`), never in the player database or the repo.
  Limitation: stored as a plain file protected only by the user's profile permissions.
* Xbox/Microsoft rate limits and ToS for unofficial endpoints are not documented. The app polls at most once
  per 60 s (minimum enforced 30 s) and backs off on HTTP 429. Use is at the user's own risk.

### Failure modes
Endpoint 404/403 (route changed or blocked), expired/revoked token (re-sign-in), child account restrictions,
HTTP 429, offline, response shape change, source list not including timestamps (then "last seen" is the time
this app first saw the account in the live list, and is labelled as such).

### Alternatives if the endpoint is unavailable
Manual import (built), a future provider reading a documented API if Microsoft ever publishes one, or a
user-supplied export from another tool.

## 2. SOTREP ([sotrep.com](https://www.sotrep.com/))
Verified by fetching the site's public HTML/JS:
* Profile pages are `https://www.sotrep.com/search/<slug>`. The page script accepts either a **numeric
  profile id** or a slug `xbox-<gamertag>` where the gamertag is lower-cased, whitespace -> `-`, and every
  character outside `[a-z0-9-]` removed.
* It is lossy: special characters (including a `#1234` suffix) are dropped and the numeric profile id
  cannot be derived from an XUID or gamertag by the client, so **no guaranteed-direct profile URL exists**.
  The app builds `/search/xbox-<slug>` and labels it a SOTREP *search* link. Duplicate gamertags resolve to
  whatever the site picks.
* No public API documentation. The internal `/api/profile/<id>` returns 403 with a CAPTCHA requirement for
  guests, so **the app does not call the API or scrape**; it only opens links in the user's browser.
* [Terms](https://www.sotrep.com/terms) do not mention automation but prohibit overloading the site and
  harvesting data about players for harassment. [robots.txt](https://www.sotrep.com/robots.txt) only
  declares AI/search content signals. Opening a link per user click is within these.

## 3. Licensing choice
MIT: permissive, maximally simple for a small public utility, no dependency on third-party code.
