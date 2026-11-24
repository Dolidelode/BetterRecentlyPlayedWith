"""Sign in to Xbox with a Microsoft account, instead of an OpenXBL key.

* The user signs in on Microsoft's own page in the browser (OAuth 2.0 authorization code + PKCE for a public desktop
  client). The app never sees the password.
* Microsoft returns a refresh token. It is stored encrypted with Windows DPAPI (only this Windows user on this PC can
  decrypt it) in ms_accounts.json in the app-data folder, never in the database, the program file or the repository.
* From it the app asks Xbox for an XSTS token for its own app identity (relying party http://xboxlive.com) and keeps
  that only in memory until it expires.

The client ID belongs to the project's Azure app registration ("Personal Microsoft accounts", public client with
redirect http://localhost:8080/auth/callback). A public client has no secret, so the ID is meant to be public.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .providers import ProviderError, _iso_to_epoch, http_json

CLIENT_ID = os.environ.get("BRPW_MS_CLIENT_ID") or "81f7d7f4-3d19-4659-a2d4-23b948a6456c"
AUTHORIZE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
USER_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
REFRESH_SCOPE = "XboxLive.signin offline_access"
# A new sign-in also asks for the email (openid email), so the Xbox app's account picker can be answered by itself
# (xboxui.py). Refreshes keep the old scope: asking for more there fails for sign-ins made before (AADSTS70000).
SCOPE = REFRESH_SCOPE + " openid email"
REDIRECT_PATH = "/auth/callback"
PORTS = (8080, 8081, 8082)      # Microsoft ignores the port of a http://localhost redirect for desktop apps
MARK = "msa:"                   # an account "key" starting with this is a Microsoft sign-in, followed by the XUID

XSTS_ERRORS = {
    2148916233: "This Microsoft account has no Xbox profile yet. Sign in once at xbox.com to create one.",
    2148916235: "Xbox Live isn't available in this account's country or region.",
    2148916236: "This account needs adult verification on xbox.com first.",
    2148916237: "This account needs adult verification on xbox.com first.",
    2148916238: "This is a child account: an adult must add it to a Microsoft family first.",
}


# ---------------------------------------------------------------- DPAPI (Windows per-user encryption)
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    entropy = b"BetterRecentlyPlayedWith"
    ebuf = ctypes.create_string_buffer(entropy, len(entropy))
    ent = Blob(len(entropy), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    # (data in, description, extra entropy, reserved, prompt, CRYPTPROTECT_UI_FORBIDDEN, data out)
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):
        raise OSError("Windows could not " + ("encrypt" if protect else "decrypt") + " the saved sign-in.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


PLAIN = "file-only:"              # Linux/macOS: no DPAPI; the file itself is readable by your user only (0600)


def protect(text: str) -> str:
    if sys.platform != "win32":
        return PLAIN + base64.b64encode(text.encode("utf-8")).decode("ascii")
    return base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def unprotect(blob: str) -> str:
    if blob.startswith(PLAIN):
        return base64.b64decode(blob[len(PLAIN):]).decode("utf-8")
    if sys.platform != "win32":
        raise OSError("This sign-in was saved encrypted on Windows; sign in again here.")
    return _dpapi(base64.b64decode(blob), False).decode("utf-8")


# ---------------------------------------------------------------- browser sign-in
def _pkce() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def browser_sign_in(client_id: str = CLIENT_ID, open_url: Callable = webbrowser.open, timeout: int = 300,
                    post=http_json) -> dict:
    """Microsoft sign-in in the user's browser. Returns Microsoft's token response (access + refresh token)."""
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    got: Dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != REDIRECT_PATH:
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
            ok = "code" in got and got.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<html><body style='font-family:Segoe UI;background:#10202a;color:#e6eef2;padding:40px'>"
                              + ("<h2>Signed in.</h2>You can close this tab and go back to BetterRecentlyPlayedWith."
                                 if ok else "<h2>Sign-in did not complete.</h2>Close this tab and try again.")
                              + "</body></html>").encode("utf-8"))

        def log_message(self, *a):
            pass

    server = None
    for port in PORTS:
        try:
            server = http.server.HTTPServer(("localhost", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise ProviderError("Ports 8080-8082 are in use, so the sign-in can't receive Microsoft's answer.")
    redirect = f"http://localhost:{server.server_port}{REDIRECT_PATH}"
    server.timeout = 1
    try:
        open_url(AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": SCOPE,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "prompt": "select_account"}))
        end = time.time() + timeout
        while not got and time.time() < end:
            server.handle_request()
    finally:
        server.server_close()
    if not got:
        raise ProviderError("Sign-in timed out. Try again.")
    if got.get("state") != state:
        raise ProviderError("Sign-in answer did not match this request. Try again.")
    if "code" not in got:
        raise ProviderError("Sign-in was cancelled or refused: " + (got.get("error_description") or
                                                                    got.get("error") or "no reason given"))
    return post(TOKEN_URL, {"client_id": client_id, "grant_type": "authorization_code", "code": got["code"],
                            "redirect_uri": redirect, "code_verifier": verifier, "scope": SCOPE}, None, True)


def refresh_ms_token(refresh_token: str, client_id: str = CLIENT_ID, http=http_json) -> dict:
    try:
        return http(TOKEN_URL, {"client_id": client_id, "grant_type": "refresh_token",
                                "refresh_token": refresh_token, "scope": REFRESH_SCOPE}, None, True)
    except ProviderError as e:
        if getattr(e, "code", 0) in (400, 401):
            raise ProviderError("The Microsoft sign-in expired or was revoked. Sign in again.", reauth=True)
        raise


def email_from(ms: dict) -> str:
    """The account's email from the id_token of a sign-in (straight from Microsoft's token endpoint over HTTPS, so its
    signature isn't checked). Empty when there is none."""
    try:
        part = str(ms.get("id_token") or "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, TypeError):
        return ""
    for k in ("email", "preferred_username"):
        v = str(claims.get(k) or "").strip()
        if "@" in v:
            return v
    return ""


def xbox_token(ms_access_token: str, http=http_json) -> dict:
    """Microsoft access token -> Xbox user token -> XSTS token. Returns {header, xuid, gamertag, expires}."""
    ver = {"x-xbl-contract-version": "1"}
    user = http(USER_AUTH_URL, {"RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT",
                                "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                                               "RpsTicket": "d=" + ms_access_token}}, ver)
    try:
        xsts = http(XSTS_URL, {"RelyingParty": "http://xboxlive.com", "TokenType": "JWT",
                               "Properties": {"UserTokens": [user["Token"]], "SandboxId": "RETAIL"}}, ver)
    except ProviderError as e:
        for code, msg in XSTS_ERRORS.items():
            if str(code) in str(e):
                raise ProviderError(msg, reauth=True)
        raise
    claims = xsts["DisplayClaims"]["xui"][0]
    return {"header": f"XBL3.0 x={claims['uhs']};{xsts['Token']}", "xuid": str(claims.get("xid") or ""),
            "gamertag": str(claims.get("gtg") or ""), "expires": _iso_to_epoch(xsts.get("NotAfter")) or 0}


# ---------------------------------------------------------------- saved sign-ins
class MsAccounts:
    """Microsoft sign-ins by XUID. Refresh tokens are DPAPI-encrypted on disk; Xbox tokens live only in memory."""

    def __init__(self, path: Path, http=http_json, protect_fn=protect, unprotect_fn=unprotect,
                 client_id: str = CLIENT_ID):
        self.path, self.http, self.client_id = Path(path), http, client_id
        self.protect, self.unprotect = protect_fn, unprotect_fn
        self.lock = threading.RLock()
        self.cache: Dict[str, dict] = {}            # xuid -> xbox_token() result

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d.get("accounts"), dict):
                return d
        except (OSError, ValueError, AttributeError):
            pass
        return {"accounts": {}}

    def _save(self, d: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)                         # only your user can read it (Linux/macOS)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def xuids(self) -> List[str]:
        with self.lock:
            return list(self._load()["accounts"])

    def gamertag(self, xuid: str) -> str:
        with self.lock:
            return self._load()["accounts"].get(xuid, {}).get("gamertag", "")

    def email(self, xuid: str) -> str:
        """The account's email when it was signed in with the email permission, else ""."""
        with self.lock:
            blob = self._load()["accounts"].get(xuid, {}).get("email")
        try:
            return self.unprotect(blob) if blob else ""
        except (OSError, ValueError):
            return ""

    def add_from_ms_token(self, ms: dict) -> Tuple[str, str]:
        """Store a fresh sign-in (from browser_sign_in). Returns (xuid, gamertag)."""
        tok = xbox_token(ms["access_token"], self.http)
        if not tok["xuid"]:
            raise ProviderError("Xbox did not return an account for this sign-in.")
        with self.lock:
            d = self._load()
            acc = {"gamertag": tok["gamertag"], "refresh": self.protect(ms["refresh_token"]), "added": int(time.time())}
            email = email_from(ms)
            if email:
                acc["email"] = self.protect(email)            # only on this PC, like the refresh token
            d["accounts"][tok["xuid"]] = acc
            self._save(d)
            self.cache[tok["xuid"]] = tok
        return tok["xuid"], tok["gamertag"]

    def sign_in(self, open_url: Callable = webbrowser.open, timeout: int = 300) -> Tuple[str, str]:
        return self.add_from_ms_token(browser_sign_in(self.client_id, open_url, timeout, self.http))

    def remove(self, xuid: str) -> None:
        with self.lock:
            d = self._load()
            d["accounts"].pop(xuid, None)
            self._save(d)
            self.cache.pop(xuid, None)

    def auth(self, xuid: str) -> dict:
        """A valid Xbox token for this account, refreshed when it is about to expire."""
        with self.lock:
            tok = self.cache.get(xuid)
            if tok and tok["expires"] - time.time() > 300:
                return tok
            d = self._load()
            acc = d["accounts"].get(xuid)
            if not acc:
                raise ProviderError("This Microsoft sign-in was removed. Sign in again.", reauth=True)
            try:
                refresh = self.unprotect(acc["refresh"])
            except (OSError, ValueError):
                raise ProviderError("The saved sign-in can't be read on this PC or Windows user. Sign in again.",
                                    reauth=True)
            ms = refresh_ms_token(refresh, self.client_id, self.http)
            tok = xbox_token(ms["access_token"], self.http)
            if ms.get("refresh_token") and ms["refresh_token"] != refresh:   # Microsoft rotates refresh tokens
                acc["refresh"] = self.protect(ms["refresh_token"])
            acc["gamertag"] = tok["gamertag"] or acc.get("gamertag", "")
            self._save(d)
            self.cache[xuid] = tok
            return tok
