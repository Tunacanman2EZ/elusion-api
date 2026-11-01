"""googlesignin/services.py - the rules, the codes, Google's keys and our page
(game 0.101.0). Pure, except google_certs(), the one place Google is asked
for anything, and only for its published keys - kept for as long as Google
says they may be.
"""
import base64
import hashlib
import html
import json
import re
import secrets
import time
import urllib.request

# How long a code may wait for the page, how often the game asks, and how
# many a minute an address may begin.
GOOGLE_CODE_SECONDS = 600
GOOGLE_POLL_SECONDS = 2
GOOGLE_STARTS_PER_WINDOW = 10
GOOGLE_START_WINDOW = 600
# A finished or run-out code is kept this long past its end, then pruned.
GOOGLE_CODES_KEPT_SECONDS = 3600

PAGE_PATH = "/api/auth/google/page"
FINISH_PATH = "/api/auth/google/finish"
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
# Google's keys, kept at least this long and at most this long whatever its
# Cache-Control says.
CERTS_KEEP_LEAST = 300
CERTS_KEEP_MOST = 6 * 3600
CERTS_TIMEOUT = 5

CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
CLIENT_ID_PATTERN = re.compile(r"^[0-9A-Za-z._-]{1,200}\.apps\.googleusercontent\.com$")
TOKEN_MOST = 4096

NOT_SET_UP = "Google sign-in is not set up on this server yet."
STAFF_REFUSED = "Staff accounts sign in with their password and code, not Google."
RUN_OUT = "This sign-in has run out. Press Sign in with Google in the game again."
LINKED_ELSEWHERE = "That Google account is already linked to another Elusion account."
DONE_SIGNIN = "Done - go back to the game."
DONE_LINK = "Linked - go back to the game."


def clean_client_id(raw):
    """The Client ID from the setting, or "" when it is unset or not one."""
    raw = str(raw or "").strip()
    return raw if CLIENT_ID_PATTERN.match(raw) else ""


def new_code():
    return secrets.token_urlsafe(24)


def code_hash(code):
    return hashlib.sha256(str(code).encode("utf-8")).hexdigest()


def check_code(code):
    """The short code the game and the page both show - "K7Q-4MX" - so a
    player can see the page is for their game."""
    digest = base64.b32encode(hashlib.sha256(("check:" + str(code)).encode("utf-8")).digest()).decode("ascii")
    return "%s-%s" % (digest[:3], digest[3:6])


def code_ok(code):
    return isinstance(code, str) and bool(CODE_PATTERN.match(code))


def page_url(scheme, host, code):
    """Our page for `code`, on the host the game reached us by. Google allows
    http only for localhost, so a local 127.0.0.1 is said as localhost."""
    host = str(host or "localhost")
    if host.startswith("127.0.0.1"):
        host = "localhost" + host[len("127.0.0.1"):]
    return "%s://%s%s?c=%s" % (scheme or "https", host, PAGE_PATH, code)


# =============================================================================
# GOOGLE'S KEYS, AND ITS TOKEN CHECKED
# =============================================================================

_certs = {"certs": {}, "until": 0}


def _fetch_certs():
    """(Google's keys {key id: PEM}, how long they may be kept)."""
    with urllib.request.urlopen(GOOGLE_CERTS_URL, timeout=CERTS_TIMEOUT) as answer:
        keep = 3600
        match = re.search(r"max-age=(\d+)", answer.headers.get("Cache-Control", "") or "")
        if match:
            keep = int(match.group(1))
        return json.loads(answer.read().decode("utf-8")), keep


# THE SUITE'S DOOR: a test hands its own keys here (test_googlesignin.py).
fetch_certs = _fetch_certs


def google_certs(now=None):
    """Google's keys, fetched when the last ones are older than Google said."""
    now = time.time() if now is None else now
    if _certs["certs"] and now < _certs["until"]:
        return _certs["certs"]
    certs, keep = fetch_certs()
    _certs["certs"] = dict(certs)
    _certs["until"] = now + max(CERTS_KEEP_LEAST, min(CERTS_KEEP_MOST, int(keep)))
    return _certs["certs"]


def forget_certs():
    _certs["certs"] = {}
    _certs["until"] = 0


def verify_id_token(token, client_id):
    """Google's ID token checked: (who, "") with who {"sub", "email"}, or
    (None, why). Signed by one of Google's keys, made for `client_id`, not run
    out (google-auth's decode, with ten seconds' slack), made by Google, and
    for an email Google has verified."""
    if not client_id:
        return None, "not-set-up"
    if not isinstance(token, str) or token == "" or len(token) > TOKEN_MOST:
        return None, "no-token"
    from google.auth import jwt as google_jwt
    try:
        certs = google_certs()
    except Exception:  # noqa: BLE001 - Google unreachable is "try again", never a crash
        return None, "no-keys"
    try:
        claims = google_jwt.decode(token, certs=certs, audience=client_id, clock_skew_in_seconds=10)
    except Exception:  # noqa: BLE001 - any token google-auth refuses is refused
        return None, "bad-token"
    if claims.get("iss") not in GOOGLE_ISSUERS:
        return None, "bad-issuer"
    if claims.get("email_verified") is not True:
        return None, "unverified"
    sub = claims.get("sub")
    if not isinstance(sub, str) or not 1 <= len(sub) <= 255:
        return None, "no-sub"
    return {"sub": sub, "email": str(claims.get("email") or "")[:254]}, ""


# =============================================================================
# OUR PAGE
# =============================================================================

PAGE_STYLE = (
    "body{background:#16130f;color:#efe3c8;font-family:system-ui,-apple-system,Segoe UI,sans-serif;"
    "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;padding:16px;"
    "box-sizing:border-box}"
    ".box{max-width:420px;width:100%;padding:28px;border:2px solid #b8893a;border-radius:10px;"
    "background:#211b15;text-align:center}"
    "h1{font-size:22px;margin:0 0 12px;color:#ffd977}"
    ".code{font:700 30px ui-monospace,Consolas,monospace;letter-spacing:4px;color:#ffd977;margin:8px 0 18px}"
    ".gbtn{display:flex;justify-content:center;min-height:44px}"
    ".say{min-height:1.4em;margin-top:16px}.bad{color:#ff9a85}.good{color:#8fe08a}"
)

PAGE_SCRIPT = """
function say(text, kind) {
  var line = document.getElementById("say");
  line.textContent = text;
  line.className = "say " + (kind || "");
}
function elusionSignedIn(answer) {
  say("Signing in...");
  fetch(FINISH, {method: "POST", headers: {"Content-Type": "application/json"},
                 body: JSON.stringify({code: CODE, credential: answer.credential})})
    .then(function (r) { return r.json().then(function (d) { return [r.ok, d]; }); })
    .then(function (x) {
      if (x[0]) { say(x[1].message || "Done - go back to the game.", "good"); }
      else { say(x[1].message || "That did not work. Press Sign in with Google in the game again.", "bad"); }
    })
    .catch(function () { say("Could not reach the server. Try once more.", "bad"); });
}
"""


def page_html(code, client_id, words=""):
    """Our page. With a live code and a Client ID: the check code and Google's
    button. Otherwise only `words` (run out, not set up)."""
    head = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Sign in to Elusion</title><style>%s</style>' % PAGE_STYLE)
    if words or not client_id or not code_ok(code):
        return (head + '</head><body><div class="box"><h1>Sign in to Elusion</h1><p class="bad">%s</p>'
                       '</div></body></html>' % html.escape(words or RUN_OUT))
    return (head
            + '<script src="https://accounts.google.com/gsi/client" async></script>'
            + '<script>var CODE = %s; var FINISH = %s;%s</script>' % (json.dumps(code), json.dumps(FINISH_PATH),
                                                                     PAGE_SCRIPT)
            + '</head><body><div class="box"><h1>Sign in to Elusion</h1>'
            + '<p>Your game shows this code. Only go on if it is the same:</p>'
            + '<div class="code">%s</div>' % html.escape(check_code(code))
            + '<div id="g_id_onload" data-client_id="%s" data-callback="elusionSignedIn"' % html.escape(client_id)
            + ' data-auto_prompt="false" data-ux_mode="popup"></div>'
            + '<div class="gbtn"><div class="g_id_signin" data-type="standard" data-size="large"'
            + ' data-text="signin_with" data-shape="pill" data-theme="filled_black"></div></div>'
            + '<p class="say" id="say"></p></div></body></html>')


def staff_name(role_for, default_role, username):
    """True when an account of this name would be staff the moment it was
    made - the owner's or a co-owner's name - which no Google sign-up may take."""
    return role_for({"username": username, "role": default_role}) != default_role
