"""
Minigames and their wait. Run: python3 test_minigames.py

The owner: "we need to get rid of ladder and make this a button in hud that
says minigames this dungeon will have a 1 day wait timer for players", and the
day starts when you press Go. minigames/ (game 0.93.3).

  MG-1  THE LIST: every minigame, its wait, and yours - ready at first.
  MG-2  GO STARTS THE DAY: the first Go is let in, a second inside the day is
        refused (409, how long is left, in words), and the list says so.
  MG-3  A DAY LATER IT OPENS AGAIN; per account, so another character does not
        start a fresh day, and another account has a day of its own.
  MG-4  THE OWNER SKIPS THE WAIT, and is written all the same.
  MG-5  NOT A MINIGAME, NOT JSON, NO LOGIN: 400, 400, 401.
"""
import importlib.util, os, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_minigames_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "mgowner"
os.environ.pop("ELUSION_GAMEDATA", None)
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
import minigames.services as services  # noqa: E402

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

PW = "hunter2hunter2"
_ip = [60]
def register(name):
    _ip[0] += 1
    r = client.post("/api/auth/register", json={"username": name, "password": PW},
                    environ_base={"REMOTE_ADDR": "198.51.100.%d" % _ip[0]})
    assert r.status_code == 201, r.get_json()
    return {"Authorization": "Bearer " + r.get_json()["token"]}

def raw(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(sql, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()

def endless(headers):
    games = (client.get("/api/minigames", headers=headers).get_json() or {}).get("games", [])
    return next((g for g in games if g.get("id") == "endless"), {})

def go(headers, game="endless"):
    return client.post("/api/minigames/enter", headers=headers, json={"game": game})


ann = register("mgann")
bob = register("mgbob")
owner = register("mgowner")
uid = lambda name: raw("SELECT id FROM users WHERE username = ?", (name,))[0][0]

section("MG-1 THE LIST")
first = endless(ann)
check("the Endless is a minigame, a day's wait, and you may go in now",
      first.get("name") == "The Endless" and first.get("wait_seconds") == services.DAY == 86400
      and first.get("ready_in") == 0 and first.get("entered_at") == 0, first)

section("MG-2 GO STARTS THE DAY")
res = go(ann)
body = res.get_json() or {}
check("the first Go is let in, and says the next wait is a day", res.status_code == 200 and body.get("ok") is True
      and body.get("ready_in") == services.DAY, (res.status_code, body))
res = go(ann)
body = res.get_json() or {}
check("a second Go inside the day is refused (409), with how long is left, in words",
      res.status_code == 409 and services.DAY - 5 <= body.get("ready_in", 0) <= services.DAY
      and body.get("message", "").startswith("The Endless opens for you again in 2"), (res.status_code, body))
listed = endless(ann)
check("  and the list says the same", services.DAY - 5 <= listed.get("ready_in", 0) <= services.DAY
      and listed.get("entered_at", 0) > 0, listed)

section("MG-3 A DAY LATER, AND WHOSE DAY")
check("another account has a day of its own", go(bob).status_code == 200)
raw("UPDATE minigame_entries SET entered_at = entered_at - ? WHERE user_id = ?", (services.DAY - 90, uid("mgann")))
res = go(ann)
check("  with 90 seconds of the day left, still refused, '1m'", res.status_code == 409
      and "again in 1m" in (res.get_json() or {}).get("message", ""), res.get_json())
raw("UPDATE minigame_entries SET entered_at = entered_at - 100 WHERE user_id = ?", (uid("mgann"),))
check("  past the day it opens again", endless(ann).get("ready_in") == 0 and go(ann).status_code == 200)
check("the wait is the account's: one row per account and minigame, whichever character",
      raw("SELECT COUNT(*) FROM minigame_entries WHERE user_id = ?", (uid("mgann"),))[0][0] == 1
      and "slot" not in [r[1] for r in raw("PRAGMA table_info(minigame_entries)")])

section("MG-4 THE OWNER SKIPS THE WAIT")
check("the owner goes in twice running", go(owner).status_code == 200 and go(owner).status_code == 200)
check("  is never shown a wait, and is written all the same",
      endless(owner).get("ready_in") == 0 and endless(owner).get("entered_at", 0) > 0
      and (client.get("/api/minigames", headers=owner).get_json() or {}).get("skips_wait") is True)

section("MG-5 REFUSALS")
check("not a minigame: 400", go(ann, "bigfield").status_code == 400 and go(ann, 7).status_code == 400)
check("not JSON: 400", client.post("/api/minigames/enter", headers=ann, data="x").status_code == 400)
check("no login: 401", client.get("/api/minigames").status_code == 401
      and client.post("/api/minigames/enter", json={"game": "endless"}).status_code == 401)
check("wait_words: hours and minutes, never '0m'", services.wait_words(48000) == "13h 20m"
      and services.wait_words(3600) == "1h" and services.wait_words(59) == "under a minute")

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
sys.exit(1 if failed else 0)
