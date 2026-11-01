"""Signing in with Google (game 0.101.0): googlesignin/.

  GS-1  NOT SET UP: no Client ID, and every Google route says so (503) - the
        page too - and nothing else changes.
  GS-2  GOOGLE'S TOKEN IS CHECKED HERE: signed by Google's key (a stand-in
        key the suite hands services.fetch_certs), made for our Client ID,
        not run out, made by Google, for a verified email. Each wrong one is
        refused, and a code is never finished by one.
  GS-3  A NEW PLAYER: start, the page, finish, poll ("pick a name"), register
        - an account with Google's email as its confirmed recovery address,
        signed in like a password login, the address and computer recorded.
  GS-4  THE CODE IS THIS COMPUTER'S: another install, a made-up code, a used
        or run-out one; only one collect; too many begun from one address.
  GS-5  COMING BACK: the same Google account signs into the same account; a
        second game account can never be made with it; a banned account gets
        its ban; the password login still works beside it.
  GS-6  STAFF NEVER: a staff account linked by hand is refused at the page
        and at the poll; Google cannot be linked to staff; no Google sign-up
        may take the owner's name.
  GS-7  LINKING AN EXISTING ACCOUNT: link (the password again on a hardened
        session), the page, the poll; a Google account linked elsewhere is
        refused; unlink, but never for an account made with Google.
  GS-8  THE GATES OF A NEW ACCOUNT: names, a closed server, ban evasion by
        address and by computer.
  GS-9  DELETING THE ACCOUNT takes its Google link with it.

Run: python test_googlesignin.py
"""
import importlib.util, os, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_googlesignin_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "gsowner"
os.environ.pop("ELUSION_GAMEDATA", None)
os.environ.pop("ELUSION_GOOGLE_CLIENT_ID", None)
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + suffix):
        os.remove(DB_PATH + suffix)
sys.path.insert(0, HERE)


def load_app(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "app.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


app_module = load_app("elusion_app")
client = app_module.app.test_client()
import googlesignin.services as gs  # noqa: E402

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

def raw(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(sql, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()

# ---- a stand-in Google: its own key, and tokens signed with it ----
import datetime  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from google.auth import crypt as google_crypt, jwt as google_jwt  # noqa: E402

def make_key(kid):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "stand-in google")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return google_crypt.RSASigner.from_string(pem_key, kid), cert.public_bytes(serialization.Encoding.PEM).decode()

GOOGLE_SIGNER, GOOGLE_CERT = make_key("k1")
STRANGER_SIGNER, _ = make_key("k1")
fetches = []
def stand_in_certs():
    fetches.append(1)
    return {"k1": GOOGLE_CERT}, 3600
gs.fetch_certs = stand_in_certs
gs.forget_certs()

CLIENT = "123456789-standin.apps.googleusercontent.com"

def token(sub="g-100", email="player@gmail.com", verified=True, aud=CLIENT, iss="https://accounts.google.com",
          exp_in=600, signer=None):
    now = int(time.time())
    payload = {"iss": iss, "aud": aud, "sub": sub, "email": email, "email_verified": verified,
               "iat": now - 5, "exp": now + exp_in}
    return google_jwt.encode(signer or GOOGLE_SIGNER, payload).decode()

INSTALL_A = "a" * 64
INSTALL_B = "b" * 64
_ip = [10]
def next_ip():
    _ip[0] += 1
    return "203.0.113.%d" % _ip[0]

def game(install=INSTALL_A, build=None, ip="203.0.113.250"):
    out = {"X-Elusion-Install": install}
    if build is not None:
        out["X-Elusion-Build"] = str(build)
    return {"headers": out, "environ_base": {"REMOTE_ADDR": ip}}

def start(install=INSTALL_A, ip="203.0.113.250"):
    return client.post("/api/auth/google/start", json={}, **game(install, ip=ip))

def finish(code, credential):
    return client.post("/api/auth/google/finish", json={"code": code, "credential": credential})

def poll(code, install=INSTALL_A, ip="203.0.113.250"):
    return client.post("/api/auth/google/poll", json={"code": code}, **game(install, ip=ip))

def google_register(code, name, install=INSTALL_A, ip="203.0.113.250"):
    return client.post("/api/auth/google/register", json={"code": code, "username": name}, **game(install, ip=ip))

def new_player(sub, name, email=None, install=INSTALL_A):
    """The whole road for a new Google player; (register answer, its body)."""
    ip = next_ip()
    code = start(install, ip).get_json()["code"]
    finish(code, token(sub=sub, email=email or "%s@gmail.com" % name))
    poll(code, install, ip)
    res = google_register(code, name, install, ip)
    return res, res.get_json() or {}

PW = "hunter2hunter2"
def password_account(name):
    r = client.post("/api/auth/register", json={"username": name, "password": PW},
                    environ_base={"REMOTE_ADDR": next_ip()})
    assert r.status_code == 201, r.get_json()
    return r.get_json()

uid = lambda name: raw("SELECT id FROM users WHERE username = ?", (name,))[0][0]


section("GS-1 NOT SET UP")
check("no Client ID: start, finish, the link route and the page all say Google sign-in is not set up",
      start().status_code == 503 and finish("x" * 32, "t").status_code == 503
      and gs.NOT_SET_UP in client.get("/api/auth/google/page?c=" + "x" * 32).get_data(as_text=True))
os.environ["ELUSION_GOOGLE_CLIENT_ID"] = "not-a-client-id"
check("  and a setting that is not a Client ID counts as unset", start().status_code == 503)
os.environ["ELUSION_GOOGLE_CLIENT_ID"] = CLIENT
check("  set: start answers a code, a check code and our page on the host the game reached",
      start().status_code == 200)
first = start().get_json()
check("  the page URL, the short check code, ten minutes, every two seconds",
      first["url"].startswith("http://localhost/api/auth/google/page?c=") and first["code"] in first["url"]
      and len(first["check"]) == 7 and first["check"][3] == "-" and first["expires_in"] == 600
      and first["poll_every"] == 2, first)
page = client.get("/api/auth/google/page?c=" + first["code"])
html = page.get_data(as_text=True)
check("the page: Google's button for our Client ID, the check code, no caching, a referrer Google can read",
      page.status_code == 200 and "accounts.google.com/gsi/client" in html and CLIENT in html
      and first["check"] in html and page.headers.get("Cache-Control") == "no-store"
      and page.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
      and page.headers.get("Content-Type", "").startswith("text/html"), page.headers)
check("  and not framed by anyone (the app's own X-Frame-Options)", page.headers.get("X-Frame-Options") == "DENY")
check("  a code that never was gets no button, only the words",
      "gsi/client" not in client.get("/api/auth/google/page?c=" + "z" * 32).get_data(as_text=True))
check("  nor does anything that is not a code (nothing of it reaches the page)",
      "<script>" not in client.get('/api/auth/google/page?c=%3Cscript%3Ealert(1)').get_data(as_text=True))
check("an older game with no install id cannot begin one",
      client.post("/api/auth/google/start", json={}).status_code == 400)


section("GS-2 GOOGLE'S TOKEN IS CHECKED HERE")
code = start().get_json()["code"]
refused = {
    "signed by another key": token(signer=STRANGER_SIGNER),
    "made for another app": token(aud="999-other.apps.googleusercontent.com"),
    "run out": token(exp_in=-120),
    "not made by Google": token(iss="https://evil.example.com"),
    "an email Google has not verified": token(verified=False),
    "not a token at all": "abc.def.ghi",
}
answers = {why: finish(code, t).status_code for why, t in refused.items()}
check("refused: %s" % ", ".join(refused), all(s == 400 for s in answers.values()), answers)
check("  and the code is still waiting for a good one", poll(code).status_code == 202)
check("Google's keys are fetched once and kept (not on every sign-in)", len(fetches) == 1, len(fetches))
check("a good token finishes it", finish(code, token(sub="g-check")).status_code == 200)
check("  once: a second token for the same code is turned away (410)",
      finish(code, token(sub="g-other")).status_code == 410)


section("GS-3 A NEW PLAYER")
ip = next_ip()
began = start(INSTALL_A, ip).get_json()
check("waiting until the page is done (202)", poll(began["code"], ip=ip).status_code == 202)
done = finish(began["code"], token(sub="g-ann", email="Ann.Player@gmail.com"))
check("the page is told to go back to the game", done.status_code == 200
      and done.get_json().get("message") == gs.DONE_SIGNIN, done.get_json())
asked = poll(began["code"], ip=ip)
check("  a Google account with no game account yet: the game asks for a name",
      asked.status_code == 200 and asked.get_json() == {"needs_name": True}, asked.get_json())
made = google_register(began["code"], "gsann", ip=ip)
body = made.get_json() or {}
check("the name taken: an account made and signed in, the login's own answer, nothing more asked",
      made.status_code == 201 and body.get("username") == "gsann" and body.get("token")
      and body.get("role") == "player" and body.get("needs_email") is False and body.get("via") == "google",
      body)
row = raw("SELECT email, email_verified, password_hash FROM users WHERE username = 'gsann'")[0]
check("  Google's verified email is its recovery address, confirmed; no password anybody knows",
      row[0] == "Ann.Player@gmail.com" and row[1] == 1 and row[2] == app_module._TIMING_DUMMY_HASH, row)
check("  linked, made with Google", raw("SELECT sub, made_with_google FROM google_accounts WHERE user_id = ?",
                                        (uid("gsann"),)) == [("g-ann", 1)])
check("  the token works", client.get("/api/auth/session", headers={"Authorization": "Bearer " + body["token"]})
      .status_code == 200)
check("  its address and computer recorded, as at any sign-up",
      raw("SELECT COUNT(*) FROM account_ips WHERE user_id = ?", (uid("gsann"),))[0][0] == 1
      and raw("SELECT COUNT(*) FROM account_installs WHERE user_id = ?", (uid("gsann"),))[0][0] == 1)
check("  and its password cannot be guessed into: the password login refuses it",
      client.post("/api/auth/login", json={"username": "gsann", "password": "anything-at-all"},
                  environ_base={"REMOTE_ADDR": next_ip()}).status_code == 401)
check("the code is spent", poll(began["code"], ip=ip).status_code == 410
      and google_register(began["code"], "gsann2", ip=ip).status_code == 410)


section("GS-4 THE CODE IS THIS COMPUTER'S")
ip = next_ip()
mine = start(INSTALL_A, ip).get_json()["code"]
finish(mine, token(sub="g-ann"))
check("another computer cannot collect it - the same 404 as a code that never was",
      poll(mine, INSTALL_B).status_code == 404 and poll("q" * 32).status_code == 404)
check("  nor a request with no install id", client.post("/api/auth/google/poll", json={"code": mine}).status_code == 404)
check("  this computer can", poll(mine, ip=ip).status_code == 200)
check("  once", poll(mine, ip=ip).status_code == 410)
late = start(INSTALL_A, next_ip()).get_json()["code"]
raw("UPDATE google_codes SET expires_at = ? WHERE code_hash = ?", (int(time.time()) - 1, gs.code_hash(late)))
check("a code run out: the page says so, finishing it is 410, polling it is 410",
      gs.RUN_OUT in client.get("/api/auth/google/page?c=" + late).get_data(as_text=True)
      and finish(late, token()).status_code == 410 and poll(late).status_code == 410)
flood = "198.51.100.77"
codes = [start(INSTALL_A, flood).status_code for _ in range(gs.GOOGLE_STARTS_PER_WINDOW + 1)]
check("at most %d begun from one address in ten minutes (429)" % gs.GOOGLE_STARTS_PER_WINDOW,
      codes[:-1] == [200] * gs.GOOGLE_STARTS_PER_WINDOW and codes[-1] == 429, codes)


section("GS-5 COMING BACK")
ip = next_ip()
back = start(INSTALL_A, ip).get_json()["code"]
finish(back, token(sub="g-ann"))
again = poll(back, ip=ip)
check("the same Google account signs into the same account", again.status_code == 200
      and again.get_json().get("username") == "gsann" and again.get_json().get("token"), again.get_json())
res, body = new_player("g-ann", "gsann_alt")
check("one Google account, one game account: it is never asked for a name again, and no second is made",
      res.status_code in (409, 410) and not raw("SELECT id FROM users WHERE username = 'gsann_alt'"), body)
raw("UPDATE users SET is_banned = 1, ban_expires_at = NULL, ban_reason = 'cheating' WHERE username = 'gsann'")
ip = next_ip()
banned = start(INSTALL_A, ip).get_json()["code"]
finish(banned, token(sub="g-ann"))
answer = poll(banned, ip=ip)
check("banned: the ban and its reason, as the password login gives them", answer.status_code == 403
      and (answer.get_json().get("ban") or {}).get("reason") == "cheating", answer.get_json())
check("  and the same Google account cannot make another (no name is asked of it)",
      new_player("g-ann", "gsann_new", install="c" * 64)[0].status_code in (409, 410)
      and not raw("SELECT id FROM users WHERE username = 'gsann_new'"))
raw("UPDATE users SET is_banned = 0 WHERE username = 'gsann'")
password_account("gspat")
check("the password login works as ever beside it", client.post(
      "/api/auth/login", json={"username": "gspat", "password": PW},
      environ_base={"REMOTE_ADDR": next_ip()}).status_code == 200)


section("GS-6 STAFF NEVER")
password_account("gsmod")
raw("UPDATE users SET role = 'mod' WHERE username = 'gsmod'")
raw("INSERT INTO google_accounts (user_id, sub, email, linked_at, made_with_google) VALUES (?, 'g-mod', '', 0, 0)",
    (uid("gsmod"),))
ip = next_ip()
staff_code = start(INSTALL_A, ip).get_json()["code"]
at_page = finish(staff_code, token(sub="g-mod"))
check("a staff account's Google is refused at the page", at_page.status_code == 403
      and at_page.get_json().get("message") == gs.STAFF_REFUSED, at_page.get_json())
check("  and at the poll, with no session made", poll(staff_code, ip=ip).status_code == 403
      and raw("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid("gsmod"),))[0][0] == 1)
mod_token = client.post("/api/auth/login", json={"username": "gsmod", "password": PW},
                        environ_base={"REMOTE_ADDR": next_ip()}).get_json().get("token", "")
check("Google cannot be linked to a staff account", client.post(
      "/api/account/google/link", json={}, headers={"Authorization": "Bearer " + mod_token,
                                                     "X-Elusion-Install": INSTALL_A}).status_code == 403)
res, body = new_player("g-own", "gsowner")
check("no Google sign-up may take the owner's name", res.status_code == 409, body)


section("GS-7 LINKING AN EXISTING ACCOUNT")
pat = client.post("/api/auth/login", json={"username": "gspat", "password": PW, "install": INSTALL_A},
                  headers={"X-Elusion-Install": INSTALL_A, "X-Elusion-Build": "6"},
                  environ_base={"REMOTE_ADDR": next_ip()}).get_json()
hard = {"Authorization": "Bearer " + pat["token"], "X-Elusion-Install": INSTALL_A, "X-Elusion-Build": "6"}
status = client.get("/api/account/google", headers=hard).get_json()
check("not linked yet; Google sign-in available", status.get("linked") is False and status.get("available") is True,
      status)
raw("UPDATE sessions SET confirmed_at = 0 WHERE user_id = ?", (uid("gspat"),))
stale = client.post("/api/account/google/link", json={}, headers=hard)
check("linking asks for the password again on a session not freshly confirmed (confirm_password)",
      stale.status_code == 403 and stale.get_json().get("confirm_password") is True, stale.get_json())
client.post("/api/auth/confirm", json={"password": PW}, headers=hard)
link = client.post("/api/account/google/link", json={}, headers=hard)
check("  confirmed: a code and the page, as at sign-in", link.status_code == 200 and link.get_json().get("url"),
      link.get_json())
lcode = link.get_json()["code"]
taken = finish(lcode, token(sub="g-ann"))
check("a Google account linked to another account is refused", taken.status_code == 409
      and taken.get_json().get("message") == gs.LINKED_ELSEWHERE, taken.get_json())
check("  and the game is told why", poll(lcode).status_code == 403)
lcode = client.post("/api/account/google/link", json={}, headers=hard).get_json()["code"]
ok = finish(lcode, token(sub="g-pat", email="pat@gmail.com"))
told = poll(lcode)
check("a free one links: the page says so, the game hears linked, no session is made by it",
      ok.status_code == 200 and ok.get_json().get("message") == gs.DONE_LINK and told.status_code == 200
      and told.get_json().get("linked") is True and "token" not in told.get_json(), told.get_json())
row = raw("SELECT email, email_verified FROM users WHERE username = 'gspat'")[0]
check("  an account with no confirmed address takes Google's", row == ("pat@gmail.com", 1), row)
status = client.get("/api/account/google", headers=hard).get_json()
check("  and it says so, the address masked", status.get("linked") is True and status.get("made_with_google") is False
      and "pat@gmail.com" not in status.get("email", "") and status.get("email"), status)
check("linking twice is refused (409)", client.post("/api/account/google/link", json={}, headers=hard)
      .status_code == 409)
ip = next_ip()
via = start(INSTALL_A, ip).get_json()["code"]
finish(via, token(sub="g-pat"))
check("now Google signs into it too", (poll(via, ip=ip).get_json() or {}).get("username") == "gspat")
pat = client.post("/api/auth/login", json={"username": "gspat", "password": PW, "install": INSTALL_A},
                  headers={"X-Elusion-Install": INSTALL_A, "X-Elusion-Build": "6"},
                  environ_base={"REMOTE_ADDR": next_ip()}).get_json()
hard["Authorization"] = "Bearer " + pat["token"]
check("unlink takes it off", client.post("/api/account/google/unlink", json={}, headers=hard).status_code == 200
      and raw("SELECT COUNT(*) FROM google_accounts WHERE user_id = ?", (uid("gspat"),))[0][0] == 0)
ann_login = poll(start(INSTALL_A, next_ip()).get_json()["code"])
ip = next_ip()
c = start(INSTALL_A, ip).get_json()["code"]
finish(c, token(sub="g-ann"))
ann = poll(c, ip=ip).get_json()
check("an account made with Google cannot unlink it (it has no other way in)", client.post(
      "/api/account/google/unlink", json={}, headers={"Authorization": "Bearer " + ann["token"],
                                                       "X-Elusion-Install": INSTALL_A}).status_code == 409)


section("GS-8 THE GATES OF A NEW ACCOUNT")
ip = next_ip()
c = start(INSTALL_A, ip).get_json()["code"]
finish(c, token(sub="g-new1"))
poll(c, ip=ip)
check("a name the rules do not take (400), one taken (409, any case)",
      google_register(c, "no spaces!", ip=ip).status_code == 400
      and google_register(c, "GSPAT", ip=ip).status_code == 409)
raw("UPDATE users SET is_banned = 1, ban_expires_at = NULL, ban_reason = 'x' WHERE username = 'gspat'")
dodge_ip = next_ip()
raw("INSERT OR REPLACE INTO account_ips (user_id, ip, first_seen, last_seen) VALUES (?, ?, ?, ?)",
    (uid("gspat"), dodge_ip, int(time.time()), int(time.time())))
c = start(INSTALL_A, dodge_ip).get_json()["code"]
finish(c, token(sub="g-dodge"))
check("from a banned player's address: refused, as a sign-up is (403)",
      google_register(c, "gsdodge", ip=dodge_ip).status_code == 403)
raw("DELETE FROM account_ips WHERE ip = ?", (dodge_ip,))
c = start(INSTALL_A, dodge_ip).get_json()["code"]
finish(c, token(sub="g-dodge2"))
check("  and from a banned player's computer (403)", google_register(c, "gsdodge2", ip=dodge_ip).status_code == 403)
raw("UPDATE users SET is_banned = 0 WHERE username = 'gspat'")


section("GS-9 DELETING THE ACCOUNT")
res, body = new_player("g-gone", "gsgone", install="d" * 64)
gone_id = uid("gsgone")
raw("UPDATE users SET password_hash = ? WHERE id = ?", (app_module.generate_password_hash(PW), gone_id))
deleted = client.post("/api/account/delete", json={"confirm": "gsgone", "password": PW},
                      headers={"Authorization": "Bearer " + body.get("token", ""), "X-Elusion-Install": "d" * 64})
check("deleting the account takes its Google link and codes with it", deleted.status_code == 200
      and raw("SELECT COUNT(*) FROM google_accounts WHERE user_id = ?", (gone_id,))[0][0] == 0
      and raw("SELECT COUNT(*) FROM google_codes WHERE user_id = ?", (gone_id,))[0][0] == 0,
      (deleted.status_code, deleted.get_json()))
res, body = new_player("g-gone", "gsback", install="e" * 64)
check("  so the same Google account may make a new one", res.status_code == 201, body)

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
sys.exit(1 if failed else 0)
