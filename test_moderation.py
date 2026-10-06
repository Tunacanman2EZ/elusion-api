"""Moderation record tests: keeping tabs on a server that is not small.

The staff panel is how mods work when the owner is not on, so this is the
suite for what they read: the account list, the moderation log, and the notes
and warnings staff leave each other.

  M-1  the account list is PAGED - every account is reachable exactly once by
       walking it, and an account registering mid-walk pushes nobody off a page
  M-2  the search is a substring, any case, and LIKE's wildcards are escaped -
       `_` is a legal username character
  M-3  the filters (online / banned / staff) are the server's own rules, not
       approximations of them
  M-4  each row carries the account's record - bans, kicks, warnings - so a
       repeat offender stands out before anybody opens them
  M-5  notes and warnings: reach-gated like every sanction, one line, bounded,
       and a refused one leaves no row
  M-6  the log: newest first, paged by id, filtered by player / staff / kind,
       and "about this player" means the account, not anything sharing the name
  M-7  notes are STAFF-ONLY in the strict sense: read under can_act_on(), so a
       mod never reads what was written about another mod, or about themselves
  M-8  every action the server logs is a kind the log can be filtered by
  M-9  built for scale: the queries these routes run use their indexes, read
       from SQLite's own plan for the very statement the route executes
  M-10 the powers list says in words what each rank may do: every route has a
       sentence, and every limit in the notes is read from its constant

Runs against a THROWAWAY database in the temp folder, like every other suite,
so it never touches elusion.db. Run: python test_moderation.py
"""

import ast
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_moderation_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "boss"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()

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


def db():
    # Opened and closed per use: a connection left open holds the scratch file,
    # and on Windows that is what makes teardown fail.
    return sqlite3.connect(DB_PATH)


def sql(statement, args=()):
    conn = db()
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def register(name):
    """A real account through the real route, returning its auth header.

    EVERY ACCOUNT MADE THIS WAY IS MADE BEFORE THE FIRST BAN. Registration is
    refused from an address a banned account has used (the evasion rule), and
    the test client is always 127.0.0.1 - see test_revocation.py.
    """
    res = client.post("/api/auth/register", json={"username": name, "password": "password123"})
    token = (res.get_json() or {}).get("token")
    assert token, "could not register %s: %s" % (name, res.get_json())
    return {"Authorization": "Bearer " + token}


def rank(name, role):
    # The rank is a JOIN, not a claim in the token (test_revocation.py R-3), so
    # a header issued before this keeps working and carries the new rank.
    sql("UPDATE users SET role = ? WHERE username = ?", (role, name))


def seed_players(names):
    """Accounts written straight into the table, for volume.

    The register route spends a deliberate scrypt on every password; a page
    walk needs hundreds of accounts and none of them ever logs in.
    """
    now = int(time.time())
    conn = db()
    conn.executemany(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, 'x', ?)",
        [(n, now) for n in names],
    )
    conn.commit()
    conn.close()


# NULL-SAFE ON PURPOSE. A refused or crashed request comes back as an empty
# page, so the checks below fail by name instead of a KeyError ending the
# suite early - which would report the checks it never reached as nothing at
# all rather than as failures.
def get_users(headers, **params):
    res = client.get("/api/staff/users", headers=headers, query_string=params)
    body = res.get_json(silent=True) or {}
    body.setdefault("accounts", [])
    return res.status_code, body


def get_log(headers, **params):
    res = client.get("/api/staff/actions", headers=headers, query_string=params)
    body = res.get_json(silent=True) or {}
    body.setdefault("actions", [])
    return res.status_code, body


def last(rows, key):
    return rows[-1][key] if rows else None


def first_of(rows, key):
    return rows[0][key] if rows else None


def walk_users(headers, **params):
    """Every page of the list, following next_after. Returns (names, pages)."""
    names = []
    pages = 0
    after = ""
    while True:
        code, body = get_users(headers, after=after, **params)
        if code != 200:
            return None, pages
        pages += 1
        names += [a["username"] for a in body["accounts"]]
        if not body.get("more"):
            return names, pages
        after = body.get("next_after") or ""
        if not after:
            return None, pages
        if pages > 100:
            return None, pages


def walk_log(headers, **params):
    ids = []
    before = 0
    pages = 0
    while True:
        code, body = get_log(headers, before=before, **params)
        if code != 200:
            return None
        pages += 1
        ids += [e["id"] for e in body["actions"]]
        if not body.get("more"):
            return ids
        before = body.get("next_before") or 0
        if not before:
            return None
        if pages > 100:
            return None


def note(headers, username, text, kind=None):
    payload = {"username": username, "text": text}
    if kind is not None:
        payload["kind"] = kind
    res = client.post("/api/staff/note", headers=headers, json=payload)
    return res.status_code, (res.get_json() or {})


# =============================================================================
# THE CAST. Every account that goes through /api/auth/register does it here,
# before anything is banned.
# =============================================================================

OWNER = register("boss")
DEV = register("thedev");     rank("thedev", "dev")
MOD = register("themod");     rank("themod", "mod")
MOD2 = register("othermod");  rank("othermod", "mod")
PLAYER = register("rowdy")
LIVE = register("online_one")
QUIET = register("gone_quiet")

# The names that test the search: an underscore (a LIKE wildcard), its
# look-alike, mixed case, and a name that is also going to be a guild's.
seed_players(["a_b", "axb", "Zed_7", "zeta", "Shared"])
# And volume: enough that the list cannot be one page at any legal limit.
BULK = ["p%04d" % i for i in range(1, 431)]
seed_players(BULK)

ALL_NAMES = sorted((r[0] for r in sql("SELECT username FROM users")), key=str.casefold)


# =============================================================================
section("M-1 the account list is paged")
# =============================================================================

code, body = get_users(PLAYER)
check("a player cannot reach it", code == 404, code)

code, body = get_users(MOD)
check("a mod gets a page", code == 200, body)
check("the default page is STAFF_PAGE_DEFAULT accounts, not the whole server",
      len(body.get("accounts", [])) == app_module.STAFF_PAGE_DEFAULT, len(body.get("accounts", [])))
check("and says there is more", body.get("more") is True, body.get("more"))
check("next_after is the last name on the page",
      last(body["accounts"], "username") is not None
      and body.get("next_after") == last(body["accounts"], "username"), body.get("next_after"))
check("matched counts the whole server, not the page",
      body.get("matched") == len(ALL_NAMES), (body.get("matched"), len(ALL_NAMES)))
check("the page is in name order, any case",
      [a["username"] for a in body["accounts"]] == ALL_NAMES[:len(body["accounts"])],
      [a["username"] for a in body["accounts"]][:5])

names, pages = walk_users(MOD)
check("walking every page visits every account", names is not None and sorted(names, key=str.casefold) == ALL_NAMES,
      (len(names or []), len(ALL_NAMES)))
check("exactly once", names is not None and len(names) == len(set(names)), pages)
check("in order across page boundaries", names == ALL_NAMES, (names or [])[:3])
code, page2 = get_users(MOD, after=body.get("next_after") or "")
check("matched is the same on page two - the whole match, not what is left",
      page2.get("matched") == len(ALL_NAMES), page2.get("matched"))

names, pages = walk_users(MOD, limit=app_module.STAFF_PAGE_MAX)
check("the largest page walks the same set", names == ALL_NAMES, pages)
check("in fewer pages", pages == -(-len(ALL_NAMES) // app_module.STAFF_PAGE_MAX), pages)

# KEYSET, NOT OFFSET. Somebody registers while a mod is on page one, with a
# name that sorts BEFORE the cursor. An offset walk would now hand the last
# name of page one over again at the top of page two.
code, first = get_users(MOD, limit=20)
seed_players(["aaa_midwalk"])
code, second = get_users(MOD, limit=20, after=first.get("next_after") or "")
check("a name added before the cursor does not repeat anybody on the next page",
      not set(a["username"] for a in first["accounts"]) & set(a["username"] for a in second["accounts"]),
      second["accounts"][:2])
check("and does not skip anybody either",
      first_of(second["accounts"], "username") == ALL_NAMES[20],
      (first_of(second["accounts"], "username"), ALL_NAMES[20]))
sql("DELETE FROM users WHERE username = 'aaa_midwalk'")

for label, params in [
    ("a limit of 0", {"limit": 0}),
    ("a limit past the ceiling", {"limit": app_module.STAFF_PAGE_MAX + 1}),
    ("a limit that is not a number", {"limit": "lots"}),
    ("an unknown filter", {"show": "everyone"}),
    ("a search longer than any name", {"q": "x" * 65}),
]:
    code, _ = get_users(MOD, **params)
    check("%s is a 400, not a guess" % label, code == 400, code)

code, body = get_users(MOD, q="nobody_is_called_this")
check("a search with no match is an empty page, not an error",
      code == 200 and body["accounts"] == [] and body.get("more") is False and body.get("matched") == 0, body)


# =============================================================================
section("M-2 the search")
# =============================================================================

def found(**params):
    code, body = get_users(MOD, **params)
    return [a["username"] for a in body.get("accounts", [])]

check("an underscore matches an underscore", "a_b" in found(q="a_b"), found(q="a_b"))
check("and nothing else - LIKE's wildcard is escaped", "axb" not in found(q="a_b"), found(q="a_b"))
check("a percent sign matches no username at all, not every one", found(q="%") == [], found(q="%")[:3])
check("any case", "Zed_7" in found(q="ZED"), found(q="ZED"))
check("any part of the name, not only the start", "Zed_7" in found(q="ed_"), found(q="ed_"))
code, body = get_users(MOD, q="p04")
check("matched follows the search",
      body.get("matched") == len([n for n in ALL_NAMES if "p04" in n.lower()]), body.get("matched"))


# =============================================================================
section("M-3 the filters are the server's own rules")
# =============================================================================

# ONLINE. online_one beats now; gone_quiet's heartbeat is aged past the window;
# rowdy's session is expired outright however recent its beat.
client.get("/api/auth/session", headers=LIVE)
client.get("/api/auth/session", headers=QUIET)
client.get("/api/auth/session", headers=PLAYER)
window = app_module.ONLINE_WINDOW_SECONDS
sql("UPDATE sessions SET last_seen_at = last_seen_at - ? WHERE user_id ="
    " (SELECT id FROM users WHERE username = 'gone_quiet')", (window + 5,))
sql("UPDATE sessions SET expires_at = 1 WHERE user_id ="
    " (SELECT id FROM users WHERE username = 'rowdy')")

code, body = get_users(OWNER, show="online")
online = [a["username"] for a in body.get("accounts", [])]
check("online lists a player whose client is beating", "online_one" in online, online)
check("and not one gone quiet past the window", "gone_quiet" not in online, online)
check("and not one whose session expired, however recent its beat", "rowdy" not in online, online)
check("every row the filter admits says it is online",
      all(a["online"] for a in body.get("accounts", [])), body.get("accounts"))
check("the online count is the filter's own count", body.get("online") == len(online) == body.get("matched"),
      (body.get("online"), len(online), body.get("matched")))
check("an account seeded without a session is not online", "p0001" not in online, online)
# rowdy's session is dead now; a fresh one for later sections.
PLAYER = {"Authorization": "Bearer " + client.post(
    "/api/auth/login", json={"username": "rowdy", "password": "password123"}).get_json()["token"]}

# BANNED. ban_state() decides: a lapsed ban is no ban, and nothing has to run
# to release anybody. These accounts never logged in, so banning them leaves
# no address behind for the evasion rule to trip over.
client.post("/api/staff/ban", headers=MOD, json={"username": "p0003", "reason": "t", "days": 1})
client.post("/api/staff/ban", headers=DEV, json={"username": "p0005", "reason": "forever"})
client.post("/api/staff/ban", headers=MOD, json={"username": "p0004", "reason": "served", "days": 1})
sql("UPDATE users SET ban_expires_at = ? WHERE username = 'p0004'", (int(time.time()) - 60,))
banned = found(show="banned", limit=200)
check("banned lists a timed ban", "p0003" in banned, banned)
check("and a permanent one", "p0005" in banned, banned)
check("and not one whose time is up, though its flag is still set", "p0004" not in banned, banned)
check("and every row it admits says it is banned",
      all(a["banned"] for a in get_users(MOD, show="banned")[1].get("accounts", [])))

# STAFF. role_for() decides, so the owner counts by name and a column that says
# 'owner' counts for nothing.
# The CHECK constraint forbids this on a fresh database, and only a fresh one:
# a database migrated from before ranks got the column by ALTER and has no
# CHECK at all (see role_for). So the forgery is written past it here.
_forge = db()
_forge.execute("PRAGMA ignore_check_constraints = ON")
_forge.execute("UPDATE users SET role = 'owner' WHERE username = 'p0002'")
_forge.commit(); _forge.close()
staff = found(show="staff", limit=200)
check("staff lists the mods and the dev", {"themod", "othermod", "thedev"} <= set(staff), staff)
check("and the owner, who has no row saying so", "boss" in staff, staff)
check("and not a player", "rowdy" not in staff, staff)
check("and not a row whose column was forged to 'owner'", "p0002" not in staff, staff)
sql("UPDATE users SET role = 'player' WHERE username = 'p0002'")
check("filters combine with the search", found(show="staff", q="mod") == ["othermod", "themod"],
      found(show="staff", q="mod"))


# =============================================================================
section("M-5 notes and warnings")
# =============================================================================

code, body = note(MOD, "rowdy", "spamming trade chat, told to stop")
check("a mod can note a player", code == 200, body)
entry = body.get("entry", {})
check("the entry says who, what and about whom",
      entry.get("by") == "themod" and entry.get("action") == "note" and entry.get("target") == "rowdy",
      entry)
check("and carries the id the log will list it under", isinstance(entry.get("id"), int) and entry["id"] > 0, entry)

code, body = note(MOD, "rowdy", "warned  for\n\nspam\tagain", kind="warn")
check("a warning is a note of its own kind", code == 200 and body["entry"]["action"] == "warn", body)
check("written as one line", body.get("entry", {}).get("detail") == "warned for spam again", body.get("entry"))

rows_before = sql("SELECT COUNT(*) FROM staff_actions")[0][0]
for label, (headers, target, text, kind, expected) in {
    "an empty note": (MOD, "rowdy", "   ", None, 400),
    "a note past the ceiling": (MOD, "rowdy", "x" * (app_module.STAFF_NOTE_MAX + 1), None, 400),
    "a kind that is not a note": (MOD, "rowdy", "hi", "ban", 400),
    "a note on another mod": (MOD, "othermod", "hmm", None, 404),
    "a note on a dev": (MOD, "thedev", "hmm", None, 404),
    "a note on the owner": (DEV, "boss", "hmm", None, 404),
    "a note on yourself": (MOD, "themod", "hmm", None, 404),
    "a note on nobody": (MOD, "ghost_account", "hmm", None, 404),
    "a player writing one": (PLAYER, "online_one", "hmm", None, 404),
}.items():
    code, _ = note(headers, target, text, kind)
    check("%s is a %d" % (label, expected), code == expected, code)
check("and not one of those left a line in the log",
      sql("SELECT COUNT(*) FROM staff_actions")[0][0] == rows_before,
      sql("SELECT COUNT(*) FROM staff_actions")[0][0] - rows_before)

res_mod = client.post("/api/staff/note", headers=MOD, json={"username": "othermod", "text": "x"})
res_ghost = client.post("/api/staff/note", headers=MOD, json={"username": "ghost_account", "text": "x"})
check("out of reach and nonexistent are the same answer",
      res_mod.get_data() == res_ghost.get_data(), (res_mod.get_json(), res_ghost.get_json()))

check("exactly at the ceiling is fine",
      note(MOD, "rowdy", "y" * app_module.STAFF_NOTE_MAX)[0] == 200)


# =============================================================================
section("M-4 the record on the list")
# =============================================================================

client.post("/api/staff/kick", headers=MOD, json={"username": "rowdy", "reason": "cool off"})
client.post("/api/staff/ban", headers=MOD, json={"username": "rowdy", "reason": "again", "days": 1})
client.post("/api/staff/unban", headers=MOD, json={"username": "rowdy"})
# The kick and the ban both ended rowdy's sessions, and an unban does not hand
# one back - log in again, like anybody lifted from a ban.
PLAYER = {"Authorization": "Bearer " + client.post(
    "/api/auth/login", json={"username": "rowdy", "password": "password123"}).get_json()["token"]}

def row_for(headers, name):
    code, body = get_users(headers, q=name)
    return next((a for a in body.get("accounts", []) if a["username"] == name), None)

record = (row_for(MOD, "rowdy") or {}).get("record", {})
check("the list row counts the bans", record.get("ban") == 1, record)
check("and the kicks", record.get("kick") == 1, record)
check("and the warnings", record.get("warn") == 1, record)
check("and the notes", record.get("note") == 2, record)
check("an account with no record has an empty one",
      (row_for(MOD, "zeta") or {}).get("record") == {}, row_for(MOD, "zeta"))

# THE COUNTS FOLLOW THE TEXT'S RULE. A dev notes a mod; another mod reading
# that mod's row must not learn that the notes exist.
note(DEV, "themod", "heavy-handed with timeouts, keep an eye")
note(DEV, "themod", "second chat this week", kind="warn")
theirs = (row_for(MOD2, "themod") or {}).get("record", {})
check("a mod sees no note count on another mod's row", "note" not in theirs, theirs)
check("nor a warning count", "warn" not in theirs, theirs)
check("the dev who could act on them does",
      (row_for(DEV, "themod") or {}).get("record", {}).get("note") == 1, row_for(DEV, "themod"))


# =============================================================================
section("M-6 the log")
# =============================================================================

code, body = get_log(PLAYER)
check("a player cannot read the log", code == 404, code)

code, body = get_log(OWNER)
check("staff can", code == 200, body)
ids = [e["id"] for e in body.get("actions", [])]
check("newest first", ids == sorted(ids, reverse=True) and len(ids) > 0, ids[:5])
check("each entry says when, who, what, to whom and why",
      all(set(e) >= {"id", "at", "by", "action", "target", "detail"} for e in body["actions"]),
      body["actions"][:1])
check("the kinds are the server's list, for the client's dropdown",
      body.get("kinds") == list(app_module.STAFF_ACTION_KINDS), body.get("kinds"))

# Volume in the log, so paging has something to page.
conn = db()
conn.executemany(
    "INSERT INTO staff_actions (actor_id, actor_name, action, target_id, target_name, detail, created_at)"
    " VALUES (NULL, 'themod', 'kick', (SELECT id FROM users WHERE username = ?), ?, 'bulk', ?)",
    [(n, n, int(time.time())) for n in BULK[:300]],
)
conn.commit(); conn.close()
everything = [r[0] for r in sql("SELECT id FROM staff_actions ORDER BY id DESC")]
walked = walk_log(OWNER)
check("walking the log by next_before visits every entry once, newest first",
      walked == everything, (len(walked or []), len(everything)))
code, body = get_log(OWNER, limit=7)
check("next_before is the last id on the page",
      body.get("next_before") == last(body["actions"], "id") and len(body["actions"]) == 7,
      body.get("next_before"))

# ABOUT THIS PLAYER.
code, body = get_log(MOD, player="ROWDY")
kinds = sorted(e["action"] for e in body.get("actions", []))
check("player= finds the account's record, any case",
      kinds == sorted(["note", "warn", "note", "kick", "ban", "unban"]), kinds)
check("and only that account's", all(e["target"] == "rowdy" for e in body["actions"]), body["actions"])
check("with a tally of the whole record",
      body.get("summary") == {"note": 2, "warn": 1, "kick": 1, "ban": 1, "unban": 1}, body.get("summary"))
code, body = get_log(MOD, player="rowdy", action="note", limit=1)
check("the tally is the whole record, not the page",
      len(body["actions"]) == 1 and body.get("summary", {}).get("note") == 2, body)
check("and the kind filter narrows the page", all(e["action"] == "note" for e in body["actions"]), body)

# A GUILD CAN SHARE A PLAYER'S NAME. Guild actions name the guild and carry no
# id, so "about Shared the player" must not include "Shared the guild".
sql("INSERT INTO staff_actions (actor_id, actor_name, action, target_id, target_name, detail, created_at)"
    " VALUES (NULL, 'thedev', 'guild_rename', NULL, 'Shared', '-> Other: rude', ?)", (int(time.time()),))
note(MOD, "Shared", "the player, not the guild")
code, body = get_log(MOD, player="shared")
check("a player's record does not include a guild with the same name",
      [e["action"] for e in body.get("actions", [])] == ["note"], body.get("actions"))

# AN ACCOUNT THAT IS GONE is still findable, by the name stored beside the id.
note(MOD, "zeta", "left before this was read")
sql("DELETE FROM users WHERE username = 'zeta'")
code, body = get_log(MOD, player="zeta")
check("a deleted account's record is still readable by name",
      [e["detail"] for e in body.get("actions", [])] == ["left before this was read"], body.get("actions"))
code, body = get_log(MOD, player="server")
check("and so are entries that never had an account", code == 200, body)

# BY THIS MEMBER OF STAFF.
code, body = get_log(OWNER, staff="TheDev")
check("staff= lists what one person did, any case",
      body.get("actions") and all(e["by"] == "thedev" for e in body["actions"]), body.get("actions"))

code, body = get_log(OWNER, action="ban")
check("action= lists one kind", body.get("actions") and all(e["action"] == "ban" for e in body["actions"]),
      body.get("actions"))

# THE LOG OPENS ON MODERATION. The server switches and the testing tools are
# kept out of it, and "Everything" is the other choice - nothing is hidden.
for kind, target, detail in [("grant", "thedev", "12 x jadesword"), ("teleport", "rowdy", "-> field"),
                             ("pvp", "server", "on"), ("maintenance", "server", "closed")]:
    sql("INSERT INTO staff_actions (actor_id, actor_name, action, target_id, target_name, detail, created_at)"
        " VALUES (NULL, 'thedev', ?, (SELECT id FROM users WHERE username = ?), ?, ?, ?)",
        (kind, target, target, detail, int(time.time())))
code, body = get_log(OWNER, action="moderation", limit=200)
seen = {e["action"] for e in body.get("actions", [])}
check("action=moderation leaves out grants, teleports and the server switches",
      code == 200 and seen and not seen & {"grant", "teleport", "pvp", "maintenance", "minbuild"}
      and seen <= set(app_module.STAFF_ACTION_GROUPS["moderation"]), seen)
code, body = get_log(MOD, player="rowdy", action="moderation")
check("  and keeps the bans, kicks, notes and warnings",
      {"ban", "kick", "note", "warn"} <= {e["action"] for e in body.get("actions", [])}, body.get("actions"))
check("  the groups are the server's, for the client's dropdown",
      body.get("groups", {}).get("moderation") == list(app_module.STAFF_ACTION_GROUPS["moderation"]),
      body.get("groups"))
check("  and every kind in a group is a kind the log knows",
      all(k in app_module.STAFF_ACTION_KINDS for g in app_module.STAFF_ACTION_GROUPS.values() for k in g))
code, body = get_log(OWNER, limit=200)
check("  Everything still has them", {"grant", "pvp"} <= {e["action"] for e in body.get("actions", [])})
code, body = get_log(MOD, player="rowdy", action="moderation")
check("a player's record on moderation leaves out the teleport and keeps the tally whole",
      "teleport" not in [e["action"] for e in body.get("actions", [])]
      and body.get("summary", {}).get("teleport") == 1 and body.get("summary", {}).get("ban") == 1,
      (body.get("actions"), body.get("summary")))

for label, params in [
    ("an action that is not a kind", {"action": "smite"}),
    ("a negative cursor", {"before": -1}),
    ("a cursor that is not a number", {"before": "last"}),
    ("a limit of 0", {"limit": 0}),
    ("a limit past the ceiling", {"limit": app_module.STAFF_PAGE_MAX + 1}),
]:
    code, _ = get_log(OWNER, **params)
    check("%s is a 400" % label, code == 400, code)


# =============================================================================
section("M-7 notes are staff-only, and staff means those who could act")
# =============================================================================

def notes_seen(headers, **params):
    code, body = get_log(headers, limit=200, **params)
    return [e["detail"] for e in body.get("actions", []) if e["action"] in ("note", "warn")]

about_mod = "heavy-handed with timeouts, keep an eye"
check("the dev who wrote it reads it", about_mod in notes_seen(DEV, player="themod"),
      notes_seen(DEV, player="themod"))
check("the owner reads it", about_mod in notes_seen(OWNER, player="themod"))
check("a note about a mod is not read by another mod", about_mod not in notes_seen(MOD2, player="themod"),
      notes_seen(MOD2, player="themod"))
check("nor in the unfiltered log", about_mod not in notes_seen(MOD2), "")
check("nor by the mod it is about", about_mod not in notes_seen(MOD, player="themod"),
      notes_seen(MOD, player="themod"))
code, body = get_log(MOD2, player="themod")
check("a mod's tally of another mod leaves the notes out",
      "note" not in body.get("summary", {}) and "warn" not in body.get("summary", {}), body.get("summary"))
check("but a player's notes are open to every mod",
      "spamming trade chat, told to stop" in notes_seen(MOD2, player="rowdy"))

# THE SIDE DOOR. /api/staff/user/<name> has always carried the staff history.
view = client.get("/api/staff/user/themod", headers=MOD2).get_json() or {}
check("the account view does not carry notes to a mod who cannot act",
      not any(h["action"] in ("note", "warn") for h in view.get("staff_history", [])),
      view.get("staff_history"))
view = client.get("/api/staff/user/themod", headers=OWNER).get_json() or {}
check("and does to the owner",
      any(h["detail"] == about_mod for h in view.get("staff_history", [])), view.get("staff_history"))

# PRIVACY IN THE QUERY, NOT AFTER IT. Bury the public entries under a pile of
# private ones: a page filtered after its LIMIT would come back short or empty.
conn = db()
conn.executemany(
    "INSERT INTO staff_actions (actor_id, actor_name, action, target_id, target_name, detail, created_at)"
    " VALUES (NULL, 'thedev', 'note', (SELECT id FROM users WHERE username = 'othermod'),"
    " 'othermod', 'private ' || ?, ?)", [(i, int(time.time())) for i in range(60)])
conn.commit(); conn.close()
code, body = get_log(MOD, limit=10)
check("a mod's page is a full page of what they may read",
      len(body.get("actions", [])) == 10 and not any(e["detail"].startswith("private") for e in body["actions"]),
      [e["detail"] for e in body.get("actions", [])])
check("with more to come", body.get("more") is True, body.get("more"))

# THE PLAYER NEVER SEES ANY OF IT. Everything a player can ask about themselves.
leaked = []
unanswered = []
for path in ("/api/auth/session", "/api/account", "/api/save", "/api/friends",
             "/api/players/online", "/api/server/broadcasts", "/api/chat",
             "/api/economy/kingdom"):
    res = client.get(path, headers=PLAYER)
    if res.status_code != 200:
        unanswered.append((path, res.status_code))
    text = res.get_data(as_text=True)
    if "told to stop" in text or "spam again" in text:
        leaked.append(path)
check("no route the player can reach carries a note about them", leaked == [], leaked)
# A route that refused the request proves nothing about what it would carry.
check("and every one of those actually answered", unanswered == [], unanswered)


# =============================================================================
section("M-8 every logged action is a kind the log knows")
# =============================================================================

with open(os.path.join(HERE, "app.py"), encoding="utf-8") as fh:
    tree = ast.parse(fh.read())

literal_kinds = set()
computed = []
for fn in ast.walk(tree):
    if not isinstance(fn, ast.FunctionDef):
        continue
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "log_staff_action" and len(node.args) >= 2):
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                literal_kinds.add(arg.value)
            else:
                computed.append(fn.name)

kinds = set(app_module.STAFF_ACTION_KINDS)
check("found the call sites", len(literal_kinds) >= 10, literal_kinds)
check("every literal action is in STAFF_ACTION_KINDS",
      literal_kinds <= kinds, sorted(literal_kinds - kinds))
check("the only computed action is the note route's, which is checked against the private kinds",
      computed == ["staff_note"], computed)
check("the private kinds are kinds", set(app_module.STAFF_PRIVATE_KINDS) <= kinds)
check("and no kind is listed that nothing writes",
      kinds == literal_kinds | set(app_module.STAFF_PRIVATE_KINDS),
      sorted(kinds - literal_kinds - set(app_module.STAFF_PRIVATE_KINDS)))


# =============================================================================
section("M-9 the queries use their indexes")
# =============================================================================
#
# NOT A STOPWATCH. A timing check is green on a fast machine and flaky on a
# slow one, and a few hundred rows is quick whatever the plan. What does not
# change with the machine is the plan: whether SQLite answers "the newest fifty
# about this player" by seeking an index or by reading the whole table and
# sorting it. That is the difference between a log that stays instant at a
# million rows and one that gets slower every day the game is up.
#
# Every statement is built by the SAME function the route calls.

def plan(statement, args):
    conn = db()
    try:
        return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + statement, args).fetchall()]
    finally:
        conn.close()

def full_scan(lines, alias):
    # "SCAN sa" with no index is a full read; "SCAN sa USING INDEX" walks an
    # index in order and stops at the LIMIT, which is what a newest-first log
    # with no filter should do.
    return any(line.startswith("SCAN %s" % alias) and "INDEX" not in line for line in lines)

def sorts(lines):
    return any("TEMP B-TREE FOR ORDER BY" in line for line in lines)

owner_row = {"id": 1, "username": "boss", "role": "player"}
with app_module.app.test_request_context():
    for label, kwargs, index in [
        ("about one player", {"player": "rowdy", "before": 999}, "idx_staff_actions_target_id"),
        ("about a name with no account", {"player": "zeta"}, "idx_staff_actions_target_nc"),
        ("by one member of staff", {"staff": "thedev"}, "idx_staff_actions_actor_id"),
        ("by a name with no account", {"staff": "somebody_gone"}, "idx_staff_actions_actor_nc"),
        ("one kind", {"action": "ban"}, "idx_staff_actions_action"),
    ]:
        kind = kwargs.pop("action", "")
        page_sql, page_params, _, _ = app_module._staff_log_query(
            owner_row, kind=kind, limit=50, **kwargs)
        lines = plan(page_sql, page_params)
        check("the log %s seeks %s" % (label, index), any(index in line for line in lines), lines)
        check("the log %s does not read the whole table" % label, not full_scan(lines, "sa"), lines)
        check("the log %s does not sort" % label, not sorts(lines), lines)

    page_sql, page_params, _, _ = app_module._staff_log_query(owner_row, limit=50)
    lines = plan(page_sql, page_params)
    check("the unfiltered log walks newest-first without sorting", not sorts(lines), lines)

    now = int(time.time())
    page_sql, page_params, _, _ = app_module._staff_users_query("", "all", "p0100", 50, now)
    lines = plan(page_sql, page_params)
    check("the next page of the list is an index seek past the cursor",
          any("sqlite_autoindex_users" in line and "username>?" in line for line in lines), lines)
    check("and is not sorted after reading", not sorts(lines), lines)

    page_sql, page_params, _, _ = app_module._staff_users_query("", "online", "", 50, now)
    lines = plan(page_sql, page_params)
    check("the online filter finds the recent heartbeats by index",
          any("idx_sessions_seen" in line for line in lines), lines)

lines = plan(app_module.STAFF_ONLINE_COUNT_SQL, (0, 0))
check("so does the online count", any("idx_sessions_seen" in line for line in lines), lines)


# =============================================================================
print("\n=== M-10  THE POWERS LIST SAYS IT IN WORDS ===\n")
# =============================================================================
# The Powers window showed "POST /api/staff/ban" for every power and never the
# sentence beside it, and its notes still sent the owner to "the in-game item
# menu" a day after the menu moved into the GM panel. The routes are derived
# from the decorators and cannot go stale; these checks are for the words.

def powers(headers):
    res = client.get("/api/staff/powers", headers=headers)
    body = res.get_json(silent=True) or {}
    return {rank.get("rank"): rank for rank in body.get("ladder", [])}


ladder = powers(OWNER)
every_route = [route for rank in ladder.values() for route in rank.get("routes", [])]
check("the powers list has routes to describe", len(every_route) > 20, len(every_route))
undescribed = [r.get("path") for r in every_route
               if not r.get("what") or r.get("what", "").startswith("---")]
check("every route on it says what it does in a sentence", undescribed == [], undescribed)
supply = [r for r in every_route if r.get("path") == "/api/economy/supply"]
check("  including the gold supply, whose summary was below its --- and showed as \"---\"",
      supply and supply[0].get("what", "").startswith("Gold supply"), supply)

notes = {name: rank.get("notes", []) for name, rank in ladder.items()}
words = app_module._days_words
check("a limit is said as a person says it",
      [words(24 * 60), words(30 * 24 * 60), words(60), words(90)]
      == ["a day", "30 days", "an hour", "90 minutes"])
check("the mod's longest ban is the constant's",
      any(n.startswith("Ban for at most %s" % words(app_module.MAX_MOD_BAN_DAYS * 24 * 60))
          for n in notes.get("mod", [])), notes.get("mod"))
check("and so is the mod's longest mute",
      any(n.startswith("Mute for at most %s" % words(app_module.MUTE_MAX_MINUTES_MOD))
          for n in notes.get("mod", [])), notes.get("mod"))
check("and the dev's",
      any(n.startswith("Mute for up to %s" % words(app_module.MUTE_MAX_MINUTES))
          for n in notes.get("dev", [])), notes.get("dev"))

was = app_module.MUTE_MAX_MINUTES_MOD
app_module.MUTE_MAX_MINUTES_MOD = 90
try:
    moved = powers(OWNER).get("mod", {}).get("notes", [])
finally:
    app_module.MUTE_MAX_MINUTES_MOD = was
check("  read from the constant, not retyped: change it and the note follows",
      "Mute for at most 90 minutes - longer is refused." in moved, moved)

# What signing in is like for staff depends on this server's switch and mail.
real_codes, real_mail = app_module.STAFF_LOGIN_CODES, app_module.mail_can_send
try:
    app_module.STAFF_LOGIN_CODES = True
    app_module.mail_can_send = lambda: True
    with_mail = app_module._staff_login_note()
    app_module.mail_can_send = lambda: False
    without_mail = app_module._staff_login_note()
    app_module.STAFF_LOGIN_CODES = False
    switched_off = app_module._staff_login_note()
finally:
    app_module.STAFF_LOGIN_CODES, app_module.mail_can_send = real_codes, real_mail
check("staff sign in with an emailed code, once per computer, on a server that sends mail",
      "every %d days" % app_module.TRUSTED_DEVICE_DAYS in with_mail, with_mail)
check("  and the list says so when this server cannot send it",
      "cannot send mail" in without_mail, without_mail)
check("  or has the step switched off", "ELUSION_STAFF_LOGIN_CODES is off" in switched_off,
      switched_off)
check("the mod's notes carry this server's answer",
      app_module._staff_login_note() in notes.get("mod", []), notes.get("mod"))

all_notes_list = [n for rank in notes.values() for n in rank]
all_notes = " ".join(all_notes_list)
# GAME 0.7.1 REMOVED THE DEBUG KEYS, and god mode went to the owner with them:
# the GM panel's switch is the only way in, and only the owner opens the panel.
check("nobody is told they have debug keys - the game has none",
      not any("debug item keys" in n or "A key in a debug build" in n for n in all_notes_list),
      all_notes_list)
check("  and the owner is told so in words",
      any("no debug keys" in n for n in notes.get("owner", [])), notes.get("owner"))
check("god mode is the owner's, on the Testing tab",
      any("god mode" in n for n in notes.get("owner", [])), notes.get("owner"))
check("  and no lower rank is told it has it",
      not any("god mode" in n.lower() for rank in ("mod", "dev") for n in notes.get(rank, [])),
      [notes.get("mod"), notes.get("dev")])
# OWNER ONLY SINCE 6 OCT 2026: the grant moved from mod to owner, and the
# routes are read from the decorators, so the list follows by itself.
_where = {name: [r.get("path") for r in rank.get("routes", [])] for name, rank in ladder.items()}
check("the item grant is on the owner's list",
      "/api/staff/grant" in _where.get("owner", []), _where.get("owner"))
check("  and on nobody else's",
      not any("/api/staff/grant" in _where.get(name, []) for name in ("player", "mod", "dev")))
check("  and a mod is told plainly that creating items is not theirs",
      any("Cannot create items" in n for n in notes.get("mod", [])), notes.get("mod"))
check("the owner's tools are where they are now: the GM panel's Testing tab",
      any("GM panel" in n for n in notes.get("owner", []))
      and any(n.startswith("Testing tab") and "item catalogue" in n and "level" in n
              for n in notes.get("owner", [])), notes.get("owner"))
gone = [place for place in ("item menu", "owner panel") if place in all_notes.lower()]
check("  and no note sends anybody to a place the game no longer has", gone == [], gone)

seen = client.get("/api/staff/powers", headers=MOD2).get_json(silent=True) or {}
check("a mod reads the same list, and is told what they may grant",
      [r.get("rank") for r in seen.get("ladder", [])] == list(ladder.keys())
      and seen.get("you_are") == "mod" and seen.get("you_may_grant") == ["player"],
      seen.get("you_may_grant"))


print("\n" + "=" * 70)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("\n  failed:")
    for f in failures:
        print("    - %s" % f)
print("=" * 70)
sys.exit(1 if failed else 0)
