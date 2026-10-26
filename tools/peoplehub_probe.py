"""Standalone probe: does Xbox PeopleHub return LINKED ACCOUNTS (Twitch, Discord, Steam...) for a player?

Usage:  python tools/peoplehub_probe.py <azure-client-id> <xuid> [<xuid> ...] [--save-raw FILE]

* Needs a free Azure app registration ("Personal Microsoft accounts only", "Allow public client flows" = Yes). The client ID
  is NOT a secret and there is no client secret: this uses Microsoft's device-code sign-in, so you approve the login yourself
  in your browser and no password is ever typed into this script.
* Tokens stay in memory. Nothing is written to disk unless you pass --save-raw (which stores the raw JSON responses).
* It only reads profile data and prints structure (field names) plus any field that mentions a social network.
* Best first test: your OWN XUID. If your own linked accounts (set to "Show icon on my profile") appear, the data exists.
Uses only the Python standard library. Not part of the app.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCOPE = "XboxLive.signin XboxLive.offline_access"
WORDS = ("twitch", "linked", "social", "discord", "steam", "network", "reddit", "youtube", "facebook")
DECORATIONS = ["detail,preferredColor,presenceDetail", "detail,socialManager,multiplayerSummary", ""]


def post(url, data, headers=None, form=False):
    h = {"Accept": "application/json", "User-Agent": "peoplehub-probe", **(headers or {})}
    body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    h["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300]}


def get(url, headers):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "peoplehub-probe", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def sign_in(client_id):
    st, flow = post("https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode",
                    {"client_id": client_id, "scope": SCOPE}, form=True)
    if st != 200:
        sys.exit(f"Could not start sign-in (HTTP {st}): {flow.get('error_description') or flow}")
    print(f"\nOpen {flow['verification_uri']} and enter the code:  {flow['user_code']}\n(waiting for you to approve...)")
    deadline, interval = time.time() + int(flow.get("expires_in", 900)), int(flow.get("interval", 5))
    while time.time() < deadline:
        time.sleep(interval)
        st, tok = post("https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                       {"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": client_id,
                        "device_code": flow["device_code"]}, form=True)
        if st == 200:
            break
        if tok.get("error") == "slow_down":
            interval += 5
        elif tok.get("error") != "authorization_pending":
            sys.exit(f"Sign-in failed: {tok.get('error_description') or tok}")
    else:
        sys.exit("Sign-in was not completed in time.")
    st, xbl = post("https://user.auth.xboxlive.com/user/authenticate",
                   {"RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT",
                    "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                                   "RpsTicket": "d=" + tok["access_token"]}}, {"x-xbl-contract-version": "1"})
    if st != 200:
        sys.exit(f"Xbox Live sign-in failed (HTTP {st}): {xbl}")
    st, xs = post("https://xsts.auth.xboxlive.com/xsts/authorize",
                  {"RelyingParty": "http://xboxlive.com", "TokenType": "JWT",
                   "Properties": {"UserTokens": [xbl["Token"]], "SandboxId": "RETAIL"}}, {"x-xbl-contract-version": "1"})
    if st != 200:
        sys.exit(f"Xbox token request failed (HTTP {st}): {xs}")
    return xs["DisplayClaims"]["xui"][0]["uhs"], xs["Token"]


def scan(node, path=""):
    """Yield (path, value-or-type) for every key or string value that mentions a social network."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            if any(w in k.lower() for w in WORDS):
                yield p, v if isinstance(v, (str, int, float, bool, type(None))) else type(v).__name__
            yield from scan(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from scan(v, f"{path}[{i}]")
    elif isinstance(node, str) and any(w in node.lower() for w in WORDS):
        yield path, node


def shape(node, depth=0):
    if isinstance(node, dict):
        return {k: shape(v, depth + 1) for k, v in node.items()} if depth < 3 else "{...}"
    if isinstance(node, list):
        return [shape(node[0], depth + 1)] if node else []
    return type(node).__name__


PROFILE_URL = "https://profile.xboxlive.com/users/batch/profile/settings"   # documented by Microsoft, contract 2
LINK_SETTINGS = ["DisplayedLinkedAccounts"]   # name taken from the XboxReplay library; not in Microsoft's docs


def decode_setting(value):
    """A setting value is a string; linked accounts may be JSON inside that string."""
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def profile_probe(uhs, token, xuids):
    """Ask the profile service for the DisplayedLinkedAccounts setting (OpenXBL never requests it)."""
    headers = {"Authorization": f"XBL3.0 x={uhs};{token}", "x-xbl-contract-version": "2"}
    found = False
    for settings in (["Gamertag"] + LINK_SETTINGS, LINK_SETTINGS):
        st, body = post(PROFILE_URL, {"userIds": xuids, "settings": settings}, headers)
        print(f"\n== profile settings {settings} | HTTP {st}")
        users = body.get("profileUsers") if isinstance(body, dict) else None
        if not users:
            print("   no profileUsers in the answer:", json.dumps(body)[:300])
            continue
        for u in users:
            for s in u.get("settings", []):
                if s.get("id") in LINK_SETTINGS:
                    val = decode_setting(s.get("value"))
                    has = val not in (None, "", [], {})
                    found = found or has
                    print(f"   {u.get('id')}: {s['id']} = {json.dumps(val, ensure_ascii=False)[:600]}")
                elif s.get("id") == "Gamertag":
                    print(f"   {u.get('id')}: Gamertag = {s.get('value')}")
        if st == 200:
            break
    print("\nPROFILE RESULT:", "DisplayedLinkedAccounts HAS DATA, see above." if found
          else "DisplayedLinkedAccounts empty or not returned.")
    return found


def recent_probe(uhs, token):
    """Does Xbox's own recent-players list work with this sign-in (it could replace OpenXBL)? Counts only, no names."""
    headers = {"Authorization": f"XBL3.0 x={uhs};{token}", "x-xbl-contract-version": "1", "Accept-Language": "en-US"}
    st, body = get("https://peoplehub.xboxlive.com/users/me/people/recentplayers", headers)
    print(f"\n== Xbox recent players (peoplehub) | HTTP {st}")
    try:
        people = json.loads(body).get("people")
    except (ValueError, AttributeError):
        people = None
    if not isinstance(people, list):
        print("   no people list:", body[:200])
        return
    with_titles = sum(1 for p in people if isinstance(p.get("recentPlayer"), dict) and p["recentPlayer"].get("titles"))
    sot = sum(1 for p in people if "sea of thieves" in json.dumps(p.get("recentPlayer") or {}).lower())
    print(f"   players: {len(people)} | with game info: {with_titles} | mentioning Sea of Thieves: {sot}")
    if people:
        rp = people[0].get("recentPlayer")
        print("   recentPlayer field structure:", json.dumps(shape(rp))[:400])


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    save = None
    if "--save-raw" in argv:
        i = argv.index("--save-raw")
        save = argv[i + 1]
        args = [a for a in args if a != save]
    if len(args) < 2:
        sys.exit(__doc__)
    client_id, xuids = args[0], args[1:]
    uhs, token = sign_in(client_id)
    profile_probe(uhs, token, xuids)                       # the new, most promising test first
    recent_probe(uhs, token)
    if "--profile-only" in argv:
        return
    headers = {"Authorization": f"XBL3.0 x={uhs};{token}", "x-xbl-contract-version": "1", "Accept-Language": "en-US"}
    raw_out, found_any = [], False
    for xuid in xuids:
        for dec in DECORATIONS:
            url = f"https://peoplehub.xboxlive.com/users/me/people/xuids({xuid})" + (f"/decoration/{dec}" if dec else "")
            st, body = get(url, headers)
            print(f"\n== {xuid} | decoration: {dec or '(none)'} | HTTP {st}")
            try:
                data = json.loads(body)
            except ValueError:
                print("   not JSON:", body[:200])
                continue
            raw_out.append({"xuid": xuid, "decoration": dec, "status": st, "body": data})
            people = data.get("people") if isinstance(data, dict) else None
            if people:
                p = people[0]
                print("   person keys:", sorted(p.keys()))
                if isinstance(p.get("detail"), dict):
                    print("   detail keys:", sorted(p["detail"].keys()))
            hits = list(scan(data))
            for path, val in hits[:15]:
                print(f"   FOUND  {path} = {val}")
            found_any = found_any or bool(hits)
            if not hits:
                print("   (nothing mentioning linked accounts / social networks)")
    if save:
        with open(save, "w", encoding="utf-8") as f:
            json.dump(raw_out, f, indent=2)
        print(f"\nRaw responses saved to {save} (contains profile data; do not share it).")
    print("\nRESULT:", "linked-account style data FOUND, see FOUND lines above." if found_any
          else "NOT FOUND: PeopleHub did not return linked accounts for these players.")


if __name__ == "__main__":
    main(sys.argv[1:])
