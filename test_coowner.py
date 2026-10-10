"""
test_coowner.py - the co-owners (game 0.21.0), and setting a player's level.

The owner, 10 Oct: "i would promote allmind to owner but there can only be 1
however thats why i want to build another gate that allows him to enter", then
"maybe a switch in my gm panel that gives him access as long as i leave it on"
and "i also need the ability to set a players level so they can try out the
game".

  CO-1  NAMED, SWITCHED OFF: an account ELUSION_CO_OWNERS names is the rank its
        row says, and the owner's name in the list is dropped.
  CO-2  THE SWITCH IS THE OWNER'S ALONE: nobody else throws it, a co-owner
        included; on with nobody named is a 409; it is logged.
  CO-3  ON: the owner's powers - every owner route, is_owner on the login, the
        session and the heartbeat, the maintenance exemption - and rank
        "coowner" between dev and owner.
  CO-4  THE OWNER STAYS ABOVE: a co-owner cannot moderate, rank, give to,
        level, read the history of or roll back the owner; the owner can kick
        a co-owner.
  CO-5  OFF AGAIN takes effect on the next request, without signing anybody out.
  CO-6  A PLAYER'S LEVEL: the character they are playing, a snapshot first, on
        their record, their game signed out; the save history puts it back.
  CO-7  THE GIFTS LEDGER SAYS WHO GAVE IT.
  CO-8  NOTHING STORES THE RANK: not the role route, not the column, not
        set_role.py, and a row reading 'coowner' is a player.
"""
import importlib.util, json, os, sqlite3, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_coowner_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "boss"
# The owner's own name in the list is dropped; the spaces are trimmed.
os.environ["ELUSION_CO_OWNERS"] = " AllMind , Boss"
os.environ.pop("ELUSION_GAMEDATA", None)
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + suffix):
        os.remove(DB_PATH + suffix)
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(_spec)
sys.modules["elusion_app"] = app_module
_spec.loader.exec_module(app_module)
client = app_module.app.test_client()

passed = failed = 0
failures = []


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print("  ok   %s" % label)
    else:
        failed += 1
        failures.append(label)
        print("  FAIL %s   %s" % (label, detail))


def section(t):
    print("\n%s\n%s" % (t, "-" * len(t)))


PW = "hunter2hunter2"
_ip = [40]


def register(name):
    _ip[0] += 1
    r = client.post("/api/auth/register", json={"username": name, "password": PW},
                    environ_base={"REMOTE_ADDR": "198.51.100.%d" % _ip[0]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["token"]


def bearer(token):
    return {"Authorization": "Bearer " + token}


def raw(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        out = conn.execute(sql, args).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()


def one(sql, args=()):
    rows = raw(sql, args)
    return rows[0][0] if rows else None


def uid(name):
    return one("SELECT id FROM users WHERE username = ?", (name,))


def make(token, slot, class_id):
    r = client.put("/api/save", headers=bearer(token),
                   json={"slot": slot, "class_id": class_id, "name": class_id})
    assert r.status_code == 200, r.get_json()


def session(token):
    return client.get("/api/auth/session", headers=bearer(token))


def switch(token, on):
    return client.post("/api/server/coowners", headers=bearer(token), json={"on": on})


owner_t = register("boss")
make(owner_t, 0, "warrior")
co_t = register("AllMind")
make(co_t, 1, "mage")
dev_t = register("devver")
player_t = register("newbie")
make(player_t, 0, "tank")
raw("UPDATE users SET role = 'dev' WHERE username IN ('AllMind', 'devver')")
OWNER, CO, NEWBIE = uid("boss"), uid("AllMind"), uid("newbie")

# =========================================================================
section("CO-1 NAMED, SWITCHED OFF")
# =========================================================================
check("the list is the names the .env gives, trimmed, the owner's own dropped",
      app_module.CO_OWNER_USERNAMES == ("AllMind",), app_module.CO_OWNER_USERNAMES)
body = session(co_t).get_json()
check("off, which is how a server starts, AllMind is the dev his row says",
      body.get("role") == "dev" and body.get("is_owner") is False, body)
check("  and an owner route is the same bare 404 as for any dev",
      client.post("/api/staff/grant", headers=bearer(co_t),
                  json={"slot": 1, "item_id": "goldcoin", "quantity": 1}).status_code == 404
      and client.get("/api/server/coowners", headers=bearer(co_t)).status_code == 404)
check("the ladder has the rank between dev and owner",
      app_module.ROLES == ("player", "mod", "dev", "coowner", "owner"), app_module.ROLES)

# =========================================================================
section("CO-2 THE SWITCH IS THE OWNER'S ALONE")
# =========================================================================
for label, token in (("a player", player_t), ("a dev", dev_t), ("the named co-owner", co_t)):
    r = switch(token, True)
    check("%s cannot throw it - the same bare 404" % label, r.status_code == 404, r.status_code)
check("  and it is still off", session(co_t).get_json().get("role") == "dev")
check("a body without on is a 400", client.post("/api/server/coowners", headers=bearer(owner_t),
                                                json={"on": "yes"}).status_code == 400)
kept = app_module.CO_OWNER_USERNAMES
app_module.CO_OWNER_USERNAMES = ()
try:
    r = switch(owner_t, True)
finally:
    app_module.CO_OWNER_USERNAMES = kept
check("on, with nobody named in the .env, is a 409 that says where to name them",
      r.status_code == 409 and "ELUSION_CO_OWNERS" in r.get_json()["message"], r.get_json())
r = switch(owner_t, True)
check("the owner switches it on, and is told who it lets in",
      r.status_code == 200 and r.get_json()["on"] is True and r.get_json()["names"] == ["AllMind"]
      and r.get_json()["by"] == "boss", r.get_json())
check("  in the staff log", one("SELECT detail FROM staff_actions WHERE action = 'coowners'"
                                " ORDER BY id DESC LIMIT 1") == "on: AllMind")

# =========================================================================
section("CO-3 ON: THE OWNER'S POWERS")
# =========================================================================
body = session(co_t).get_json()
check("his heartbeat says coowner, and is_owner - the owner's powers - is true",
      body.get("role") == "coowner" and body.get("is_owner") is True, body)
check("  the owner is still the owner", session(owner_t).get_json().get("role") == "owner")
check("  a dev is still a dev", session(dev_t).get_json().get("is_owner") is False)
r = client.post("/api/staff/grant", headers=bearer(co_t), json={"slot": 1, "item_id": "goldcoin", "quantity": 2})
check("an owner route opens: a grant to his own bag", r.status_code == 200, r.get_json())
r = client.post("/api/staff/grant", headers=bearer(co_t),
                json={"username": "newbie", "item_id": "goldpile", "quantity": 3})
check("  and a give to a player", r.status_code == 200 and r.get_json()["username"] == "newbie", r.get_json())
r = client.post("/api/staff/gold", headers=bearer(co_t), json={"slot": 1, "amount": 100})
check("  and gold, and his own level",
      r.status_code == 200 and client.post("/api/staff/level", headers=bearer(co_t),
                                           json={"slot": 1, "level": 12}).status_code == 200)
check("  and reading the switch", client.get("/api/server/coowners", headers=bearer(co_t)).status_code == 200)
r = switch(co_t, False)
check("  but not throwing it - a co-owner cannot keep himself in, or put himself out",
      r.status_code == 404, r.status_code)
powers = client.get("/api/staff/powers", headers=bearer(co_t)).get_json()
check("the powers list knows him: a co-owner, who may make mods and devs",
      powers["you_are"] == "coowner" and powers["you_may_grant"] == ["player", "mod", "dev"], powers["you_are"])
rung = {r["rank"]: [x["path"] for x in r["routes"]] for r in powers["ladder"]}
check("  the owner's routes are listed on the co-owner's rung, the switch on the owner's",
      "/api/staff/grant" in rung["coowner"] and rung["owner"] == ["/api/server/coowners"], rung["owner"])
check("  and the rank cannot be granted", [r["grantable"] for r in powers["ladder"]
                                          if r["rank"] == "coowner"] == [False])
with app_module.app.test_request_context():
    exempt = app_module.maintenance_refusal("AllMind") is None
    raw("INSERT OR REPLACE INTO server_settings (key, value, updated_by, updated_at) VALUES (?, ?, 'boss', 0)",
        (app_module.MAINTENANCE_KEY,
         json.dumps({"on": True, "message": "closed", "back_at": None, "kick_at": 0, "by": "boss", "at": 0})))
with app_module.app.test_request_context():
    exempt_closed = app_module.maintenance_refusal("AllMind") is None
    player_refused = app_module.maintenance_refusal("newbie") is not None
raw("DELETE FROM server_settings WHERE key = ?",
    (app_module.MAINTENANCE_KEY,))
check("the maintenance switch does not lock him out, as it does not lock the owner out",
      exempt and exempt_closed and player_refused, [exempt, exempt_closed, player_refused])
check("a co-owner may moderate a dev", client.post("/api/staff/kick", headers=bearer(co_t),
                                                   json={"username": "devver"}).status_code == 200)
dev_t = register("devver2")

# =========================================================================
section("CO-4 THE OWNER STAYS ABOVE")
# =========================================================================
refused = {
    "kick": client.post("/api/staff/kick", headers=bearer(co_t), json={"username": "boss"}).status_code,
    "ban": client.post("/api/staff/ban", headers=bearer(co_t),
                       json={"username": "boss", "reason": "x", "days": 1}).status_code,
    "role": client.put("/api/staff/role", headers=bearer(co_t), json={"username": "boss", "role": "player"}).status_code,
    "give": client.post("/api/staff/grant", headers=bearer(co_t),
                        json={"username": "boss", "item_id": "goldcoin", "quantity": 1}).status_code,
    "level": client.post("/api/staff/level", headers=bearer(co_t), json={"username": "boss", "level": 3}).status_code,
    "history": client.get("/api/staff/snapshots?username=boss", headers=bearer(co_t)).status_code,
    "rollback": client.post("/api/staff/rollback", headers=bearer(co_t),
                            json={"username": "boss", "snapshot_id": 1}).status_code,
}
check("a co-owner cannot kick, ban or rank the owner - the moderation 404",
      refused["kick"] == refused["ban"] == refused["role"] == 404, refused)
check("  nor give to, level, read the history of or roll back the owner - told why",
      refused["give"] == refused["level"] == refused["history"] == refused["rollback"] == 403, refused)
check("  and the owner's character is as it was",
      one("SELECT level FROM saves WHERE user_id = ? AND slot = 0", (OWNER,)) == 1)
r = client.post("/api/staff/kick", headers=bearer(owner_t), json={"username": "AllMind"})
check("the owner can kick a co-owner", r.status_code == 200, r.get_json())
check("  and that ended his session", session(co_t).status_code == 401)
# No confirmed address and no mail on this server, so no emailed code step.
login = client.post("/api/auth/login", json={"username": "AllMind", "password": PW},
                    environ_base={"REMOTE_ADDR": "198.51.100.99"}).get_json()
co_t = login.get("token") or ""
check("  the login answer says so too", login.get("role") == "coowner" and login.get("is_owner") is True, login)
check("  and he can sign back in a co-owner", co_t != "" and session(co_t).get_json().get("role") == "coowner")

# =========================================================================
section("CO-5 OFF AGAIN")
# =========================================================================
r = switch(owner_t, False)
check("the owner switches it off", r.status_code == 200 and r.get_json()["on"] is False, r.get_json())
body = session(co_t).get_json()
check("  his next request is a dev's, with no signing out",
      body.get("role") == "dev" and body.get("is_owner") is False, body)
check("  and the owner's routes are shut to him",
      client.post("/api/staff/grant", headers=bearer(co_t),
                  json={"slot": 1, "item_id": "goldcoin", "quantity": 1}).status_code == 404)

# =========================================================================
section("CO-6 A PLAYER'S LEVEL")
# =========================================================================
snapshots_before = one("SELECT COUNT(*) FROM save_snapshots WHERE user_id = ? AND reason = 'before-level'", (NEWBIE,))
r = client.post("/api/staff/level", headers=bearer(owner_t), json={"username": "newbie", "level": 30})
body = r.get_json()
row = raw("SELECT level, xp, max_hp, hp FROM saves WHERE user_id = ? AND slot = 0", (NEWBIE,))[0]
check("the owner sets a player's level by name: the character they are playing",
      r.status_code == 200 and body["username"] == "newbie" and body["slot"] == 0 and body["level"] == 30
      and body["was"] == 1 and int(row["level"]) == 30 and int(row["hp"]) == int(row["max_hp"]) == body["max_hp"],
      body)
check("  a snapshot first, so the save history can put it back",
      one("SELECT COUNT(*) FROM save_snapshots WHERE user_id = ? AND reason = 'before-level'", (NEWBIE,))
      == snapshots_before + 1)
check("  their game signed out, so it loads the new level",
      body["sessions_ended"] == 1 and session(player_t).status_code == 401, body)
logged = raw("SELECT action, target_name, detail FROM staff_actions WHERE action = 'player_level'"
             " ORDER BY id DESC LIMIT 1")
check("  on their record, with the gives and rollbacks",
      logged and logged[0]["target_name"] == "newbie" and "level 1 -> 30" in logged[0]["detail"]
      and "player_level" in app_module.STAFF_ACTION_GROUPS["moderation"], [dict(x) for x in logged])
check("  and the owner's own character is untouched",
      one("SELECT level FROM saves WHERE user_id = ? AND slot = 0", (OWNER,)) == 1)
r = client.post("/api/staff/level", headers=bearer(owner_t), json={"username": "newbie", "level": 31})
check("a player who is signed out is set all the same, and nobody is signed out",
      r.status_code == 200 and r.get_json()["sessions_ended"] == 0, r.get_json())
check("an account that does not exist is a 404",
      client.post("/api/staff/level", headers=bearer(owner_t),
                  json={"username": "nobodyhere", "level": 5}).status_code == 404)
check("  and an account with no character", client.post("/api/staff/level", headers=bearer(owner_t),
                                                         json={"username": "devver2", "level": 5}).status_code == 404)
check("  and a level outside 1-99 is a 400 before anything is looked up",
      client.post("/api/staff/level", headers=bearer(owner_t),
                  json={"username": "newbie", "level": 100}).status_code == 400)
check("a dev cannot", client.post("/api/staff/level", headers=bearer(dev_t),
                                  json={"username": "newbie", "level": 5}).status_code == 404)
switch(owner_t, True)
r = client.post("/api/staff/level", headers=bearer(co_t), json={"username": "newbie", "level": 25})
check("a co-owner can, while the switch is on", r.status_code == 200 and r.get_json()["level"] == 25, r.get_json())
listing = client.get("/api/staff/snapshots?username=newbie", headers=bearer(owner_t)).get_json()
before = [s for s in listing["snapshots"] if s["reason"] == "before-level" and s["level"] == 1]
r = client.post("/api/staff/rollback", headers=bearer(owner_t),
                json={"username": "newbie", "snapshot_id": before[0]["id"]}) if before else None
check("the save history puts the first level back",
      r is not None and r.status_code == 200
      and one("SELECT level FROM saves WHERE user_id = ? AND slot = 0", (NEWBIE,)) == 1,
      r.get_json() if r is not None else listing)

# =========================================================================
section("CO-7 THE GIFTS LEDGER SAYS WHO GAVE IT")
# =========================================================================
client.post("/api/staff/grant", headers=bearer(owner_t),
            json={"username": "newbie", "item_id": "goldpile", "quantity": 10})
report = client.get("/api/staff/gifts", headers=bearer(owner_t)).get_json()
givers = {g["username"]: g for g in report["by_giver"]}
pile = app_module.gamedata.item_row("goldpile")["value"]
check("both givers, each with what they gave",
      set(givers) == {"boss", "AllMind"} and givers["boss"]["gold"] == 10 * pile
      and givers["AllMind"]["gold"] >= 3 * pile, report["by_giver"])
check("  his own grant is 'to yourself' - a giver to his own account",
      report["to_yourself"]["gifts"] >= 2 and report["to_players"]["gold"] == 13 * pile,
      [report["to_yourself"], report["to_players"]])
check("  and a co-owner reads the ledger too",
      client.get("/api/staff/gifts", headers=bearer(co_t)).status_code == 200)
out = subprocess.run([sys.executable, os.path.join(HERE, "giftwatch.py"), "--db", DB_PATH],
                     capture_output=True, text=True, timeout=60)
check("giftwatch.py says who gave it", out.returncode == 0 and "WHO GAVE IT" in out.stdout
      and "AllMind" in out.stdout, out.stdout[:600] + out.stderr[:300])

# =========================================================================
section("CO-8 NOTHING STORES THE RANK")
# =========================================================================
r = client.put("/api/staff/role", headers=bearer(owner_t), json={"username": "devver2", "role": "coowner"})
check("the role route refuses it, and says where it comes from",
      r.status_code == 400 and "coowner" in r.get_json()["message"], r.get_json())
try:
    raw("UPDATE users SET role = 'coowner' WHERE username = 'devver2'")
    stored = True
except sqlite3.IntegrityError:
    stored = False
check("the column's CHECK refuses it", stored is False)
check("a row reading 'coowner' anyway is a player",
      app_module.role_for({"username": "devver2", "role": "coowner"}) == "player")
out = subprocess.run([sys.executable, os.path.join(HERE, "set_role.py"), "devver2", "coowner"],
                     capture_output=True, text=True, timeout=60, env=dict(os.environ, ELUSION_DB=DB_PATH))
check("set_role.py refuses it", out.returncode != 0 and "ELUSION_CO_OWNERS" in (out.stdout + out.stderr),
      out.stdout + out.stderr)
switch(owner_t, False)
check("a co-owner is a co-owner only by name AND switch: off, the name alone is nothing",
      app_module.role_for({"username": "AllMind", "role": "player"}) == "player")

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
for f in failures:
    print("  - " + f)
sys.exit(1 if failed else 0)
