"""
test_canary.py - the canary on a timer (0.74.0).

The owner: "Checking the gold totals automatically ... You already have the
checker (canary.py); it just isn't running on a timer yet. Set up once on the
server, it would tell you the same day if gold or lusions appear or vanish
without being recorded." This suite holds canary.py, its cron line and the GM
panel's two routes to that:

  CA-1  the checks find what they are for: gold and lusions appearing without
        a ledger row, a negative balance, a skill past the cap
  CA-2  read-only: the canary's connection cannot write, and a run changes
        nothing in the database
  CA-3  the status file: what was found, written whole (a reader never sees
        half of one, a failed write leaves the last one as it was), and the
        exit code as before - 0, 1, and 2 for no database
  CA-4  the mail: every owner account's CONFIRMED address and no other, a
        co-owner only while the switch is on, the server's settings handed on
        standard input (the environment winning), sent when there is news and
        not every hour - and no address or credential ever printed or written.
        No mail set up: the status file all the same, a non-zero exit, quiet.
  CA-5  the routes: the last status (or checked_at null), its age; Check now
        runs the checks read-only, sees a drift at once, and leaves the
        status file to the timer; owner and co-owner only
  CA-6  the timer itself: update_server.sh's cron line - hourly, as the
        service's user, the paths the script and DEPLOY.md use, the status
        file where the API reads it - and its install is idempotent

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_canary.py
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_canary_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "canaryowner"
os.environ["ELUSION_CO_OWNERS"] = "canarycoowner"
# The CI runner sets console mode for test_mail.py; this suite says exactly
# which mail settings each case has, so none comes from outside.
for _name in ("ELUSION_MAIL_CONSOLE", "ELUSION_SMTP_HOST", "ELUSION_SMTP_PORT", "ELUSION_SMTP_USER",
              "ELUSION_SMTP_PASSWORD", "ELUSION_MAIL_FROM"):
    os.environ.pop(_name, None)

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
sys.modules["elusion_app"] = app_module
spec.loader.exec_module(app_module)
client = app_module.app.test_client()

import canary  # noqa: E402
import mailer  # noqa: E402

passed = 0
failed = 0
failures = []


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  pass  %s" % label)
    else:
        failed += 1
        failures.append(label)
        print("  FAIL  %s   %s" % (label, detail))


def section(title):
    print("\n--- %s ---" % title)


_ip = [0]


def register(name):
    _ip[0] += 1
    res = client.post("/api/auth/register", json={"username": name, "password": "password123"},
                      environ_base={"REMOTE_ADDR": "198.51.100.%d" % _ip[0]})
    assert res.status_code == 201, res.get_json()
    return {"Authorization": "Bearer %s" % res.get_json()["token"]}


def sql(statement, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def run(argv, stdin_text=None, environ=None, deliver=None, now=None):
    """canary.main() as the cron line runs it: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = None
    kwargs = {"argv": argv, "environ": dict(environ or {}), "now": now}
    if stdin_text is not None:
        kwargs["stdin"] = io.StringIO(stdin_text)
    if deliver is not None:
        kwargs["deliver"] = deliver
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            canary.main(**kwargs)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def status_text_of(path):
    """The status file's text, or "" when there is none - so a run that did
    not write one fails its check rather than stopping the suite."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def status_of(path):
    try:
        return json.loads(status_text_of(path))
    except ValueError:
        return {}


def forget(path):
    if os.path.exists(path):
        os.remove(path)


def database_rows():
    """Every row of every table, for "nothing changed"."""
    conn = sqlite3.connect(DB_PATH)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


OWNER = register("canaryowner")
CO = register("canarycoowner")
PLAYER = register("canaryplayer")
assert client.put("/api/save", headers=PLAYER, json={"slot": 0, "class_id": "warrior", "name": "Cass"}).status_code == 200
PLAYER_ID = int(sql("SELECT id FROM users WHERE username = 'canaryplayer'")[0]["id"])
# The account row the bank and the lusions live on - made on first use by the
# game, made here so a lusion can be planted on it.
sql("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (PLAYER_ID,))
STATUS = canary.default_status_path(DB_PATH)
SECRET = "not-a-real-password-8813"
OWNER_MAIL = "owner.alarm@example.com"
CO_MAIL = "co.alarm@example.com"

# =============================================================================
section("CA-1  the checks find what they are for")
# =============================================================================
report = canary.run_checks(DB_PATH, 1000)
check("a fresh world balances: every check holds", report["ok"] is True and report["failures"] == []
      and [c["name"] for c in report["checks"]] == ["gold conservation (ledger == held)",
                                                    "lusion conservation (ledger == held)", "no negative balances",
                                                    "no skill above the cap (99)"] and report["checked_at"] == 1000,
      report)
sql("UPDATE saves SET gold = gold + 77 WHERE user_id = ?", (PLAYER_ID,))
report = canary.run_checks(DB_PATH)
check("77 gold appearing without a ledger row is gold conservation broken, by 77",
      report["ok"] is False and [f["name"] for f in report["failures"]] == ["gold conservation (ledger == held)"]
      and "drift 77" in report["failures"][0]["detail"], report["failures"])
sql("UPDATE saves SET gold = gold - 77 WHERE user_id = ?", (PLAYER_ID,))
sql("UPDATE accounts SET lusions = lusions + 3 WHERE user_id = ?", (PLAYER_ID,))
check("  lusions appearing likewise", [f["name"] for f in canary.run_checks(DB_PATH)["failures"]]
      == ["lusion conservation (ledger == held)"])
sql("UPDATE accounts SET lusions = lusions - 3 WHERE user_id = ?", (PLAYER_ID,))
sql("INSERT OR REPLACE INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, 0, 'fishing', 120, 0)", (PLAYER_ID,))
check("  a skill past the cap", [f["name"] for f in canary.run_checks(DB_PATH)["failures"]]
      == ["no skill above the cap (99)"])
sql("DELETE FROM skills WHERE user_id = ? AND skill_id = 'fishing' AND level = 120", (PLAYER_ID,))
check("  and put right, every check holds again", canary.run_checks(DB_PATH)["ok"] is True)

# =============================================================================
section("CA-2  read-only")
# =============================================================================
con = canary.open_read_only(DB_PATH)
try:
    con.execute("UPDATE saves SET gold = 0")
    wrote = True
except sqlite3.OperationalError:
    wrote = False
finally:
    con.close()
check("the canary's connection cannot write", not wrote)
before = database_rows()
run(["--db", DB_PATH, "--status-file", STATUS, "--quiet", "--mail"])
check("  and a whole run, status file and all, changes nothing in the database", database_rows() == before)
odd_dir = os.path.join(testdir.PATH, "a folder # with ? odd names")
os.makedirs(odd_dir)
shutil.copy(DB_PATH, os.path.join(odd_dir, "copy.db"))
check("a path with a space, a # and a ? is the folder it names",
      canary.run_checks(os.path.join(odd_dir, "copy.db"))["ok"] is True)

# =============================================================================
section("CA-3  the status file")
# =============================================================================
if os.path.exists(STATUS):
    forget(STATUS)
code, out, err = run(["--db", DB_PATH, "--status-file", STATUS, "--quiet"], now=5000)
written = status_of(STATUS)
check("all well: exit 0, nothing printed, and the status file says so",
      code == 0 and out == "" and err == "" and written["ok"] is True and written["checked_at"] == 5000
      and written["failures"] == [] and len(written["checks"]) == 4, [code, out, err, written])
check("  beside the database, where the API reads it", os.path.dirname(STATUS) == os.path.dirname(DB_PATH)
      and os.path.basename(STATUS) == canary.STATUS_NAME)
check("  readable by the service and its group, nobody else", stat.S_IMODE(os.stat(STATUS).st_mode) == 0o640,
      oct(stat.S_IMODE(os.stat(STATUS).st_mode)))
check("  and nothing half-written left in the folder",
      not [n for n in os.listdir(os.path.dirname(STATUS)) if n.startswith(".canary_status.")])
sql("UPDATE saves SET gold = gold + 5 WHERE user_id = ?", (PLAYER_ID,))
code, out, err = run(["--db", DB_PATH, "--status-file", STATUS, "--quiet"], now=6000)
written = status_of(STATUS)
check("broken: exit 1, the table printed even under --quiet, and the file replaced with the failure",
      code == 1 and "BROKEN" in out and written["ok"] is False and written["checked_at"] == 6000
      and written["failures"][0]["name"] == "gold conservation (ledger == held)", [code, written])
real_dump = canary.json.dump


def half_then_fail(obj, handle, **kwargs):
    handle.write('{"checked_at": 7000, "ok": tr')
    raise OSError("the disk filled")


canary.json.dump = half_then_fail
try:
    code, out, err = run(["--db", DB_PATH, "--status-file", STATUS, "--quiet"], now=7000)
finally:
    canary.json.dump = real_dump
kept = status_of(STATUS)
check("a write that fails half-way leaves the last whole report, and no scrap of the new one",
      kept.get("checked_at") == 6000 and "could not write the status file (OSError)" in err
      and not [n for n in os.listdir(os.path.dirname(STATUS)) if n.startswith(".canary_status.")], [kept, err])
check("  and the exit is still the checks' own", code == 1)
gone = os.path.join(testdir.PATH, "nowhere", "elusion.db")
os.makedirs(os.path.dirname(gone))
code, out, err = run(["--db", gone, "--status-file", os.path.join(os.path.dirname(gone), "s.json")])
nowhere = status_of(os.path.join(os.path.dirname(gone), "s.json"))
check("no database: exit 2, said as before, and the status file says the database could not be read",
      code == 2 and ("[CANARY] FAIL: no database at %s" % gone) in err and nowhere["ok"] is False
      and nowhere["failures"][0]["name"] == "the database could be read", [code, err, nowhere])
check("a status file that is not one reads as none", canary.read_status(os.path.join(testdir.PATH, "nope.json")) is None)
with open(os.path.join(testdir.PATH, "junk.json"), "w") as junk:
    junk.write("{not json")
check("  nor does junk", canary.read_status(os.path.join(testdir.PATH, "junk.json")) is None)

# =============================================================================
section("CA-4  the mail")
# =============================================================================
sql("UPDATE users SET email = ?, email_verified = 1 WHERE username = 'canaryowner'", (OWNER_MAIL,))
sql("UPDATE users SET email = ?, email_verified = 1 WHERE username = 'canarycoowner'", (CO_MAIL,))
sql("UPDATE users SET email = 'player@example.com', email_verified = 1 WHERE username = 'canaryplayer'")
SETTINGS = "\n".join(["# the server's settings file", "ELUSION_OWNER=canaryowner", "ELUSION_CO_OWNERS=canarycoowner",
                      "ELUSION_SMTP_HOST=smtp.example.com", "ELUSION_SMTP_PORT=587", "ELUSION_SMTP_USER=sender@example.com",
                      "ELUSION_SMTP_PASSWORD=%s" % SECRET, 'ELUSION_MAIL_FROM="Elusion RPG <sender@example.com>"',
                      "ELUSION_TURN_KEY_TOKEN=also-not-for-the-canary"])
SENT = []


def deliver(settings, to_address, subject, body):
    SENT.append({"to": to_address, "subject": subject, "body": body, "host": settings["host"],
                 "sender": settings["sender"], "password": settings["password"]})
    return True, None


merged = canary.settings_with_stdin({"ELUSION_SMTP_HOST": "from-the-environment"}, SETTINGS)
check("the settings on standard input are read by envfile.py's rules, the environment winning",
      merged["ELUSION_SMTP_HOST"] == "from-the-environment" and merged["ELUSION_SMTP_PASSWORD"] == SECRET
      and merged["ELUSION_MAIL_FROM"] == "Elusion RPG <sender@example.com>")
check("  and only the mail and owner settings are kept", "ELUSION_TURN_KEY_TOKEN" not in merged)
forget(STATUS)
code, out, err = run(["--db", DB_PATH, "--status-file", STATUS, "--mail", "--env-stdin", "--quiet"],
                     stdin_text=SETTINGS, deliver=deliver, now=10000)
status_text = status_text_of(STATUS)
written = json.loads(status_text)
check("the books broken: the owner's confirmed address is mailed, through the server's settings",
      code == 1 and [m["to"] for m in SENT] == [OWNER_MAIL] and SENT[0]["host"] == "smtp.example.com"
      and SENT[0]["sender"] == "Elusion RPG <sender@example.com>" and SENT[0]["password"] == SECRET, [code, SENT])
check("  saying what broke, by how much, and where to look",
      "gold conservation (ledger == held)" in SENT[0]["body"] and "drift 5" in SENT[0]["body"]
      and "Economy tab" in SENT[0]["body"] and SENT[0]["subject"].startswith("Elusion canary"))
check("  a co-owner is not mailed while the owner's switch is off, and a player never",
      CO_MAIL not in [m["to"] for m in SENT] and "player@example.com" not in [m["to"] for m in SENT])
check("  the status file says it mailed one address, and when - not which",
      written["mail"] == "sent" and written["mailed"] == 1 and written["mailed_at"] == 10000
      and "@" not in status_text and SECRET not in status_text, status_text)
check("  and nothing printed holds an address or the password",
      "@" not in out + err and SECRET not in out + err and "mail: sent (1 of 1 owner address)" in out, out + err)
code, out, err = run(["--db", DB_PATH, "--status-file", STATUS, "--mail", "--env-stdin", "--quiet"],
                     stdin_text=SETTINGS, deliver=deliver, now=10000 + 3600)
check("an hour later, still broken the same way: the owner already knows - no second mail",
      len(SENT) == 1 and status_of(STATUS)["mail"] == "already told"
      and status_of(STATUS)["mailed_at"] == 10000)
assert client.post("/api/server/coowners", headers=OWNER, json={"on": True}).status_code == 200
sql("UPDATE accounts SET lusions = lusions + 2 WHERE user_id = ?", (PLAYER_ID,))
code, out, err = run(["--db", DB_PATH, "--status-file", STATUS, "--mail", "--env-stdin", "--quiet"],
                     stdin_text=SETTINGS, deliver=deliver, now=10000 + 7200)
check("something else breaks: news - mailed again, the co-owner too now the switch is on",
      [m["to"] for m in SENT[1:]] == [OWNER_MAIL, CO_MAIL] and status_of(STATUS)["mailed"] == 2, SENT[1:])
code, out, err = run(["--db", DB_PATH, "--status-file", STATUS, "--mail", "--env-stdin", "--quiet"],
                     stdin_text=SETTINGS, deliver=deliver, now=10000 + 7200 + canary.MAIL_AGAIN_SECONDS)
check("  and a day later, still broken: told again", len(SENT) == 5)
sql("UPDATE users SET email_verified = 0 WHERE username = 'canarycoowner'")
check("an unconfirmed address is not the owner's: only confirmed addresses, each once",
      canary.owner_addresses(canary.open_read_only(DB_PATH), canary.settings_with_stdin({}, SETTINGS)) == [OWNER_MAIL])
sql("UPDATE users SET email_verified = 1 WHERE username = 'canarycoowner'")


def failing_deliver(settings, to_address, subject, body):
    return False, ConnectionRefusedError("smtp.example.com refused %s" % to_address)


forget(STATUS)
code, out, err = run(["--db", DB_PATH, "--status-file", STATUS, "--mail", "--env-stdin"],
                     stdin_text=SETTINGS, deliver=failing_deliver, now=20000)
failed_status = status_text_of(STATUS)
check("a mail that fails says the kind of failure and nothing the server said back",
      json.loads(failed_status)["mail"] == "failed (ConnectionRefusedError)" and "@" not in failed_status
      and "@" not in out + err and json.loads(failed_status)["mailed_at"] is None, [failed_status, out])
forget(STATUS)
code, out, err = run(["--db", DB_PATH, "--status-file", STATUS, "--mail", "--quiet"], deliver=deliver, now=30000)
nomail = status_of(STATUS)
check("no mail set up: the status file written all the same, exit 1, and --quiet says nothing about mail",
      code == 1 and nomail["mail"] == "not configured" and nomail["ok"] is False and "mail:" not in out
      and len(SENT) == 5, [code, nomail.get("mail"), out])
code, out, err = run(["--db", DB_PATH, "--status-file", STATUS, "--mail", "--env-stdin"],
                     stdin_text="ELUSION_OWNER=canaryowner\nELUSION_MAIL_CONSOLE=1\n", deliver=deliver, now=40000)
check("console mode (a PC): said, not sent - the count, never the address",
      "mail (console mode, nothing sent) to 1 owner address" in out and "@" not in out and len(SENT) == 5, out)
check("the policy: first break, a change, and a day later are news; an hour later is not",
      canary.should_mail(None, {"ok": False, "failures": [{"name": "a"}]}, 0)
      and canary.should_mail({"ok": True}, {"ok": False, "failures": [{"name": "a"}]}, 0)
      and not canary.should_mail({"ok": False, "failures": [{"name": "a"}], "mailed_at": 0},
                                 {"ok": False, "failures": [{"name": "a"}]}, 3600)
      and canary.should_mail({"ok": False, "failures": [{"name": "a"}], "mailed_at": 0},
                             {"ok": False, "failures": [{"name": "b"}]}, 3600)
      and canary.should_mail({"ok": False, "failures": [{"name": "a"}], "mailed_at": 0},
                             {"ok": False, "failures": [{"name": "a"}]}, canary.MAIL_AGAIN_SECONDS)
      and not canary.should_mail(None, {"ok": True, "failures": []}, 0))

# THE CONVERSATION ITSELF, mailer.py's, which the canary and the server's own
# send_mail() share: SSL on 465, STARTTLS anywhere else, a login only with a
# user - and nothing printed whatever happens.
SMTP_SEEN = []


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        SMTP_SEEN.append(("connect", host, port, timeout))

    def starttls(self):
        SMTP_SEEN.append(("starttls",))

    def login(self, user, password):
        SMTP_SEEN.append(("login", user, password))

    def send_message(self, message):
        SMTP_SEEN.append(("send", message["To"], message["From"], message["Subject"], message.get_content().strip()))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        SMTP_SEEN.append(("quit",))


class FakeSSL(FakeSMTP):
    def __init__(self, host, port, timeout=None):
        SMTP_SEEN.append(("ssl",))
        super().__init__(host, port, timeout)


FAKE = type("smtp", (), {"SMTP": FakeSMTP, "SMTP_SSL": FakeSSL})
said = io.StringIO()
with contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
    sent = mailer.deliver(mailer.settings_from({"ELUSION_SMTP_HOST": " smtp.example.com ", "ELUSION_SMTP_USER": "u",
                                                "ELUSION_SMTP_PASSWORD": SECRET, "ELUSION_MAIL_FROM": "Elusion <s@example.com>"}),
                          OWNER_MAIL, "Subject", "Body", smtp=FAKE)
check("mailer.py: STARTTLS on 587, the login, the message as written, closed",
      sent == (True, None) and SMTP_SEEN == [("connect", "smtp.example.com", 587, 15), ("starttls",), ("login", "u", SECRET),
                                            ("send", OWNER_MAIL, "Elusion <s@example.com>", "Subject", "Body"), ("quit",)]
      and said.getvalue() == "", SMTP_SEEN)
SMTP_SEEN.clear()
mailer.deliver({"host": "smtp.example.com", "port": 465, "user": "", "password": "", "sender": "s@example.com"},
               OWNER_MAIL, "S", "B", smtp=FAKE)
check("  SSL on 465, and no login without a user", SMTP_SEEN[0] == ("ssl",) and ("starttls",) not in SMTP_SEEN
      and not any(step[0] == "login" for step in SMTP_SEEN), SMTP_SEEN)


class Refused(FakeSMTP):
    def __init__(self, host, port, timeout=None):
        raise ConnectionRefusedError("refused by %s" % host)


said = io.StringIO()
with contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
    try:
        sent, error = mailer.deliver({"host": "smtp.example.com", "port": 587, "user": "u", "password": SECRET,
                                      "sender": "s@example.com"}, OWNER_MAIL, "S", "B",
                                     smtp=type("smtp", (), {"SMTP": Refused, "SMTP_SSL": Refused}))
    except Exception as raised:  # noqa: BLE001 - raising is the fault this checks for
        sent, error = "raised", raised
check("  a refusal is handed back, never raised and never printed", sent is False
      and isinstance(error, ConnectionRefusedError) and said.getvalue() == "")
check("the mail settings are read as the server reads them: the port 587 unless said, console by name",
      mailer.settings_from({})["port"] == 587 and mailer.settings_from({"ELUSION_SMTP_PORT": "465"})["port"] == 465
      and not mailer.can_send(mailer.settings_from({"ELUSION_SMTP_HOST": "h"}))
      and mailer.can_send(mailer.settings_from({"ELUSION_SMTP_HOST": "h", "ELUSION_MAIL_FROM": "f"}))
      and mailer.can_send(mailer.settings_from({"ELUSION_MAIL_CONSOLE": "1"})))
kept = (app_module.smtplib, app_module.SMTP_HOST, app_module.SMTP_PORT, app_module.SMTP_USER, app_module.SMTP_PASSWORD,
        app_module.MAIL_FROM, app_module.MAIL_CONSOLE)
SMTP_SEEN.clear()
app_module.smtplib = FAKE
app_module.SMTP_HOST, app_module.SMTP_PORT, app_module.SMTP_USER = "smtp.example.com", 587, "u"
app_module.SMTP_PASSWORD, app_module.MAIL_FROM, app_module.MAIL_CONSOLE = SECRET, "Elusion <s@example.com>", False
try:
    server_sent = app_module.send_mail(OWNER_MAIL, "From the server", "Its own body")
finally:
    (app_module.smtplib, app_module.SMTP_HOST, app_module.SMTP_PORT, app_module.SMTP_USER, app_module.SMTP_PASSWORD,
     app_module.MAIL_FROM, app_module.MAIL_CONSOLE) = kept
check("and the server's own send_mail() is that same conversation, with its own settings",
      server_sent is True and SMTP_SEEN == [("connect", "smtp.example.com", 587, 15), ("starttls",), ("login", "u", SECRET),
                                            ("send", OWNER_MAIL, "Elusion <s@example.com>", "From the server",
                                             "Its own body"), ("quit",)], SMTP_SEEN)

# =============================================================================
section("CA-5  the routes")
# =============================================================================
forget(STATUS)
never = client.get("/api/owner/economy/canary", headers=OWNER)
check("never run: checked_at null", never.status_code == 200 and never.get_json()["checked_at"] is None
      and never.get_json()["ok"] is None, never.get_json())
now = int(time.time())
run(["--db", DB_PATH, "--status-file", STATUS, "--quiet"], now=now - 120)
last = client.get("/api/owner/economy/canary", headers=OWNER).get_json()
check("after a run: the last report as the file holds it, its age, not stale",
      last["checked_at"] == now - 120 and last["ok"] is False and 120 <= last["age_seconds"] < 150
      and last["stale"] is False and last["failures"][0]["name"] == "gold conservation (ledger == held)", last)
run(["--db", DB_PATH, "--status-file", STATUS, "--quiet"], now=now - 3 * 3600)
check("  three hours old is stale - the timer looks stopped",
      client.get("/api/owner/economy/canary", headers=OWNER).get_json()["stale"] is True)
file_before = status_text_of(STATUS)
sql("UPDATE saves SET gold = gold - 5 WHERE user_id = ?", (PLAYER_ID,))
sql("UPDATE accounts SET lusions = lusions - 2 WHERE user_id = ?", (PLAYER_ID,))
rows_before = database_rows()
fresh = client.post("/api/owner/economy/canary", headers=OWNER)
check("Check now: the checks run now, and see the books put right at once",
      fresh.status_code == 200 and fresh.get_json()["ok"] is True and fresh.get_json()["checked_at"] >= now
      and len(fresh.get_json()["checks"]) == 4, fresh.get_json())
check("  leaving the status file to the timer, and the database as it was",
      status_text_of(STATUS) == file_before and database_rows() == rows_before)
check("  and holding no write lock while it reads: it writes nothing, and the ledger is long",
      getattr(app_module.app.view_functions["owner_canary_check"], "_elusion_no_write_lock", False) is True)
sql("UPDATE saves SET gold = gold + 9 WHERE user_id = ?", (PLAYER_ID,))
check("  and a drift made a moment ago is seen by the next Check now",
      client.post("/api/owner/economy/canary", headers=CO).get_json()["failures"][0]["detail"].endswith("drift 9"))
sql("UPDATE saves SET gold = gold - 9 WHERE user_id = ?", (PLAYER_ID,))
check("a co-owner reads both (the switch is on); a player gets the bare 404; no login the 401",
      client.get("/api/owner/economy/canary", headers=CO).status_code == 200
      and client.get("/api/owner/economy/canary", headers=PLAYER).status_code == 404
      and client.post("/api/owner/economy/canary", headers=PLAYER).status_code == 404
      and client.get("/api/owner/economy/canary").status_code == 401
      and client.post("/api/owner/economy/canary").status_code == 401)

# =============================================================================
section("CA-6  the timer itself")
# =============================================================================
SCRIPT = os.path.join(HERE, "deploy", "update_server.sh")
source = open(SCRIPT, encoding="utf-8").read()
settings = dict(re.findall(r"^(API|VENV|ENV_FILE|DB)=(\S+)$", source, re.M))
bash = shutil.which("bash")
if bash is None:
    print("  (no bash here: the cron line is read from the script's text instead)")
    line_text = ""
else:
    line_text = subprocess.run([bash, SCRIPT, "--canary-cron"], capture_output=True, text=True, timeout=30).stdout
lines = [line for line in line_text.splitlines() if line and not line.startswith("#")]
job = next((line for line in lines if re.match(r"^\d+ \* \* \* \* ", line)), "")
if bash is not None:
    status_path = os.path.join(os.path.dirname(settings.get("DB", "")), canary.STATUS_NAME)
    check("--canary-cron prints the file and needs nothing: one job, hourly, at a fixed minute",
          job != "" and len([line for line in lines if re.match(r"^\d+ ", line)]) == 1, line_text)
    check("  run as root only to hand over the settings file; the canary itself as elusion, with the venv's Python",
          job.split()[5] == "root" and ("/usr/sbin/runuser -u elusion -- %s/bin/python %s/canary.py"
                                        % (settings["VENV"], settings["API"])) in job, job)
    check("  on the live database, the status file beside it - where the API reads it - mail on, quiet",
          ("--db %s" % settings["DB"]) in job and ("--status-file %s" % status_path) in job
          and status_path == canary.default_status_path(settings["DB"])
          and all(flag in job for flag in ("--mail", "--env-stdin", "--quiet")), job)
    check("  the settings on standard input, from the settings file - never a value on the line",
          ("< %s" % settings["ENV_FILE"]) in job and not re.search(r"ELUSION_[A-Z_]+=", line_text), job)
    check("  and no % anywhere (cron would read one as a new line)", "%" not in line_text)
    deploy_md = open(os.path.join(HERE, "DEPLOY.md"), encoding="utf-8").read()
    check("DEPLOY.md shows the very line the script writes", job in deploy_md)
    cron_d = tempfile.mkdtemp(prefix="elusion_cron_", dir=testdir.PATH)
    env = dict(os.environ, ELUSION_CRON_D=cron_d)
    first = subprocess.run([bash, SCRIPT, "--install-canary-cron"], env=env, capture_output=True, text=True, timeout=30)
    target = os.path.join(cron_d, "elusion-canary")
    installed = open(target, encoding="utf-8").read() if os.path.exists(target) else ""
    second = subprocess.run([bash, SCRIPT, "--install-canary-cron"], env=env, capture_output=True, text=True, timeout=30)
    check("--install-canary-cron writes the file, exactly the printed text, mode 644",
          first.returncode == 0 and installed == line_text and "written" in first.stdout
          and stat.S_IMODE(os.stat(target).st_mode) == 0o644, [first.stdout, first.stderr])
    check("  and run again changes nothing, and says so - idempotent",
          second.returncode == 0 and "unchanged" in second.stdout
          and open(target, encoding="utf-8").read() == installed, second.stdout)
    with open(target, "a", encoding="utf-8") as edited:
        edited.write("* * * * * root echo hijacked\n")
    third = subprocess.run([bash, SCRIPT, "--install-canary-cron"], env=env, capture_output=True, text=True, timeout=30)
    check("  an edited file is put back as it should be",
          third.returncode == 0 and "written" in third.stdout and open(target, encoding="utf-8").read() == installed)
    check("  and nothing else is left in cron.d - no temporary file for cron to read",
          os.listdir(cron_d) == ["elusion-canary"], os.listdir(cron_d))
    locked = tempfile.mkdtemp(prefix="elusion_cron_locked_", dir=testdir.PATH)
    os.chmod(locked, 0o500)
    refused = subprocess.run([bash, SCRIPT, "--install-canary-cron"], env=dict(os.environ, ELUSION_CRON_D=locked),
                             capture_output=True, text=True, timeout=30)
    os.chmod(locked, 0o700)
    if os.geteuid() != 0:
        check("  a folder it cannot write is said, and a non-zero exit",
              refused.returncode != 0 and "cannot write" in refused.stdout, refused.stdout)
    else:
        print("  (running as root, so a read-only folder is still writable - that case is not testable here)")
main_body = source[source.index("main() {"):]
check("every update puts the line back: after a healthy restart, and on a run with nothing new to pull",
      main_body.count("install_canary_cron || true") == 2
      and main_body.index("install_canary_cron || true") < main_body.index('say "Already running') + 400)
check("  and the canary is not allowed to stop an update: only its own mode's exit is the install's",
      main_body.count("install_canary_cron") == 3
      and main_body.count("install_canary_cron\n        exit $?") == 1)

print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
