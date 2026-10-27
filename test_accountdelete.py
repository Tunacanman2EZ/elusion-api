"""Deleting your own account (game 0.90.0): accountdelete/, and the two tables
that had no foreign keys (chat/schema.py).

  AD-1  services.py: the stand-in's name can never be signed up with; the
        name typed, any case
  AD-2  the refusals: no token, the wrong name, no password, the wrong password
        (a failed login, counted), the owner, an open trade, a guild's leader
        with others in it - and nothing changed by any of them
  AD-3  the deletion: every row that was theirs gone; their gold and lusions
        burned through the ledgers (the supply still adds up); the guild and the
        party left as leaving leaves them; their chat lines off every screen
        and their pictures gone; other people's trades, the staff log and the
        reports kept with the stand-in's name; signed out; the name free again
  AD-4  ignores and chat_image_holders hold their foreign keys, and a table
        made before them is rebuilt once, keeping its rows

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_accountdelete.py
"""
import importlib.util
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_accountdelete_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "delowner"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()

from accountdelete import data as del_data, services  # noqa: E402
import chat.schema as chat_schema  # noqa: E402

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
    return res.get_json()["token"]


def auth(token):
    return {"Authorization": "Bearer %s" % token}


def sql(statement, args=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def one(statement, args=()):
    return sql(statement, args)[0][0]


def uid(name):
    return int(one("SELECT id FROM users WHERE username = ?", (name,)))


def character(token, slot, class_id):
    res = client.put("/api/save", headers=auth(token), json={"slot": slot, "class_id": class_id, "name": class_id})
    assert res.status_code == 200, res.get_json()


def mint(user_id, slot, gold):
    """Gold the honest way the suites do it: the purse and its ledger row together."""
    sql("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = ?", (gold, user_id, slot))
    sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, ?, ?, 'test', '')",
        (int(time.time()), user_id, slot, gold))


def supply():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return app_module.gold_supply(conn)
    finally:
        conn.close()


def delete(token, confirm, password="password123"):
    return client.post("/api/account/delete", headers=auth(token) if token else {},
                       json={"confirm": confirm, "password": password})


# =============================================================================
section("AD-1  services.py - the stand-in and the name typed")
name = services.stand_in_name(12)
check("an emptied account is 'deleted player #12': unique, and outside USERNAME_PATTERN (nobody can take it)",
      name == "deleted player #12" and not app_module.USERNAME_PATTERN.match(name), name)
check("  and is known for one", services.is_stand_in(name) and not services.is_stand_in("deleted_player"))
check("the name typed matches in any case, spaces round it forgiven; anything else does not",
      services.confirmed("  TuNa ", "tuna") and not services.confirmed("tun", "tuna")
      and not services.confirmed(None, "tuna") and not services.confirmed("", ""))
check("a guild's leader with others in it is held back; alone, or not its leader, is not",
      services.guild_blocks("leader", 2) and not services.guild_blocks("leader", 1)
      and not services.guild_blocks("officer", 5))

# =============================================================================
section("AD-2  the refusals - and nothing changes")
victim = register("delvictim")
character(victim, 0, "warrior")
character(victim, 1, "mage")
vid = uid("delvictim")
mint(vid, 0, 500)
mint(vid, 1, 70)
other = register("delother")
character(other, 0, "tank")
oid = uid("delother")
owner = register("delowner")

check("no token is a 401", delete(None, "delvictim").status_code == 401)
res = delete(victim, "delvictm")
check("the wrong name typed is a 400 that says what to type",
      res.status_code == 400 and "name" in (res.get_json() or {}).get("message", ""), res.get_json())
check("no password is a 400", delete(victim, "delvictim", "").status_code == 400)
before = int(one("SELECT COUNT(*) FROM login_attempts WHERE username = 'delvictim' AND reason = 'bad-password-on-delete'"))
res = delete(victim, "delvictim", "wrong-password")
check("the wrong password is a 401", res.status_code == 401, res.get_json())
check("  counted as a failed login", int(one(
    "SELECT COUNT(*) FROM login_attempts WHERE username = 'delvictim' AND reason = 'bad-password-on-delete'")) == before + 1)
check("  and the session is still good (a 401 here is not a sign-out)",
      client.get("/api/save", headers=auth(victim)).status_code == 200)
res = delete(owner, "delowner")
check("the owner's account cannot be deleted (403)", res.status_code == 403, res.get_json())
now = int(time.time())
sql("INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, state, created_at, updated_at)"
    " VALUES ('t-open', ?, 0, ?, 0, 'open', ?, ?)", (vid, oid, now, now))
res = delete(victim, "delvictim")
check("an open trade naming them is a 409", res.status_code == 409 and "trade" in res.get_json()["message"], res.get_json())
sql("DELETE FROM trades WHERE trade_id = 't-open'")
sql("INSERT INTO guilds (name, folded, founded_by, created_at) VALUES ('Del Guild', 'del guild', 'delvictim', ?)", (now,))
gid = int(one("SELECT id FROM guilds WHERE name = 'Del Guild'"))
sql("INSERT INTO guild_members (user_id, guild_id, rank, joined_at) VALUES (?, ?, 'leader', ?), (?, ?, 'member', ?)",
    (vid, gid, now, oid, gid, now + 1))
res = delete(victim, "delvictim")
check("leading a guild with others in it is a 409 (hand it on first)",
      res.status_code == 409 and "guild" in res.get_json()["message"], res.get_json())
check("  and nothing was taken by any refusal",
      one("SELECT username FROM users WHERE id = ?", (vid,)) == "delvictim"
      and int(one("SELECT COUNT(*) FROM saves WHERE user_id = ?", (vid,))) == 2
      and int(one("SELECT SUM(gold) FROM saves WHERE user_id = ?", (vid,))) == 570)
sql("UPDATE guild_members SET rank = 'member' WHERE user_id = ?", (vid,))
sql("UPDATE guild_members SET rank = 'leader' WHERE user_id = ?", (oid,))

# =============================================================================
section("AD-3  the deletion")
third = register("delthird")
tid = uid("delthird")
fourth = register("delfourth")
fid = uid("delfourth")
# The bank and lusions, through their ledgers as the server keeps them.
sql("INSERT OR IGNORE INTO accounts (user_id, lusions, bank_gold) VALUES (?, 0, 0)", (vid,))
sql("UPDATE accounts SET bank_gold = 1000, lusions = 30 WHERE user_id = ?", (vid,))
sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, NULL, 1000, 'test', '')", (now, vid))
sql("INSERT INTO lusion_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, NULL, 30, 'test', '')", (now, vid))
sql("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, 0, 0, 'ironsword', 1)", (vid,))
sql("INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, 0, 'ironhelm', 1)", (vid,))
sql("INSERT OR REPLACE INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, 0, 'attack', 5, 10)", (vid,))
sql("INSERT INTO kill_tally (user_id, slot, enemy_id, kills, first_at, last_at) VALUES (?, 0, 'bat', 3, ?, ?)", (vid, now, now))
sql("INSERT INTO position_samples (at, user_id, slot, area, x, y) VALUES (?, ?, 0, 'field', 1, 2)", (now, vid))
sql("INSERT INTO combat_kills (at, area, enemy_id, user_id, slot, verdict) VALUES (?, 'field', 'bat', ?, 0, 'AGREED')", (now, vid))
sql("INSERT INTO friends (requester_id, addressee_id, state, created_at) VALUES (?, ?, 'accepted', ?)", (vid, oid, now))
sql("INSERT INTO friends (requester_id, addressee_id, state, created_at) VALUES (?, ?, 'pending', ?)", (tid, vid, now))
sql("INSERT INTO ignores (user_id, ignored_id, created_at) VALUES (?, ?, ?), (?, ?, ?)", (vid, tid, now, fid, vid, now))
# A party of three they lead, so the party goes on with a new leader.
sql("INSERT INTO parties (created_at) VALUES (?)", (now,))
pid = int(one("SELECT MAX(id) FROM parties"))
sql("INSERT INTO party_members (user_id, party_id, is_leader, joined_at) VALUES (?, ?, 1, ?), (?, ?, 0, ?), (?, ?, 0, ?)",
    (vid, pid, now, tid, pid, now + 1, fid, pid, now + 2))
# A line in world chat with a picture only they put up, and a line of
# another player's showing a picture both put up.
sql("INSERT INTO chat_images (id, width, height, bytes, created_at, data) VALUES ('img-mine', 1, 1, 1, ?, x'00'), "
    "('img-shared', 1, 1, 1, ?, x'00')", (now, now))
sql("INSERT INTO chat_image_holders (image_id, user_id, created_at) VALUES ('img-mine', ?, ?), ('img-shared', ?, ?),"
    " ('img-shared', ?, ?)", (vid, now, vid, now, oid, now))
sql("INSERT INTO chat_messages (user_id, username, role, body, created_at, channel, target_id, image_id)"
    " VALUES (?, 'delvictim', 'player', 'hello all', ?, 'world', 0, 'img-mine')", (vid, now))
mine_line = int(one("SELECT MAX(id) FROM chat_messages"))
sql("INSERT INTO chat_messages (user_id, username, role, body, created_at, channel, target_id, image_id)"
    " VALUES (?, 'delother', 'player', 'look', ?, 'world', 0, 'img-shared')", (oid, now))
# A finished trade - the other player's record too.
sql("INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, state, created_at, updated_at)"
    " VALUES ('t-done', ?, 0, ?, 0, 'done', ?, ?)", (vid, oid, now, now))
# What is kept with the name taken out.
sql("INSERT INTO staff_actions (actor_id, actor_name, action, target_id, target_name, detail, created_at)"
    " VALUES (?, 'delowner', 'warn', ?, 'delvictim', 'be nice', ?)", (uid("delowner"), vid, now))
sql("INSERT INTO chat_reports (message_id, reporter_id, reporter_name, reported_id, reported_name, channel, body,"
    " reason, created_at) VALUES (999, ?, 'delvictim', ?, 'delother', 'world', 'rude', 'spam', ?)", (vid, oid, now))
sql("INSERT INTO owner_gifts (at, actor_id, actor_name, user_id, username, slot, kind, quantity, source)"
    " VALUES (?, 1, 'delowner', ?, 'delvictim', 0, 'item', 1, 'give')", (now, vid))
sql("INSERT INTO guild_events (guild_id, at, kind, actor, target, detail) VALUES (?, ?, 'joined', 'DelVictim', '', '')",
    (gid, now))
held_before = supply()

res = delete(victim, "DELVICTIM")
body = res.get_json() or {}
check("the account is deleted (200), with what it took said back", res.status_code == 200 and body.get("deleted")
      and body.get("gold_burned") == 1570 and body.get("lusions_burned") == 30, body)

stand_in = services.stand_in_name(vid)
rows = sql("SELECT username, password_hash, email, deleted_at, role FROM users WHERE id = ?", (vid,))
check("THE ROW IS KEPT (a bare DELETE would cascade through other people's trades and the ledgers)",
      len(rows) == 1, rows)
row = rows[0] if rows else ("", "", "", 0, "")
check("  keeping only its id and a stand-in name - no password, no address, marked deleted",
      row[0] == stand_in and row[1] == "!" and row[2] == "" and row[3] > 0 and row[4] == "player", row)
left = {t: int(one("SELECT COUNT(*) FROM %s WHERE user_id = ?" % t, (vid,)))
        for t in del_data.OWN_TABLES + ("saves", "accounts", "chat_messages")}
check("every row that was theirs is gone (%d tables)" % len(left), not any(left.values()),
      {t: n for t, n in left.items() if n})
check("  friends, ignores and party invitations both ways",
      int(one("SELECT COUNT(*) FROM friends WHERE ? IN (requester_id, addressee_id)", (vid,))) == 0
      and int(one("SELECT COUNT(*) FROM ignores WHERE ? IN (user_id, ignored_id)", (vid,))) == 0)
check("  their sign-in record too", int(one("SELECT COUNT(*) FROM login_attempts WHERE username = 'delvictim'")) == 0)
held = supply()
check("THE SUPPLY STILL ADDS UP: what the ledger recorded is what purses and banks hold",
      held["recorded"] == held["held"] if "held" in held else held.get("drift", 0) == 0, held)
check("  their gold and lusions burned as 'account-deleted'",
      int(one("SELECT -SUM(delta) FROM gold_ledger WHERE user_id = ? AND reason = ?", (vid, services.DELETE_REASON))) == 1570
      and int(one("SELECT -SUM(delta) FROM lusion_ledger WHERE user_id = ? AND reason = ?", (vid, services.DELETE_REASON))) == 30)
check("their guild goes on without them, its history saying a deleted player left",
      int(one("SELECT COUNT(*) FROM guild_members WHERE guild_id = ?", (gid,))) == 1
      and int(one("SELECT COUNT(*) FROM guild_events WHERE guild_id = ? AND kind = 'left' AND actor = ?", (gid, stand_in))) == 1
      and int(one("SELECT COUNT(*) FROM guild_events WHERE actor = 'DelVictim' COLLATE NOCASE")) == 0)
check("the party they led goes on, with exactly one new leader - the longest there",
      sql("SELECT user_id FROM party_members WHERE party_id = ? AND is_leader = 1", (pid,)) == [(tid,)]
      and int(one("SELECT COUNT(*) FROM party_members WHERE party_id = ?", (pid,))) == 2)
check("their chat line is taken off every screen (a deletion the feed carries)",
      int(one("SELECT COUNT(*) FROM chat_deletions WHERE message_id = ?", (mine_line,))) == 1)
check("  the picture only they put up is gone; the one somebody else also put up stays",
      int(one("SELECT COUNT(*) FROM chat_images WHERE id = 'img-mine'")) == 0
      and int(one("SELECT COUNT(*) FROM chat_images WHERE id = 'img-shared'")) == 1
      and body.get("pictures_removed") == 1)
hist = client.get("/api/trade/history", headers=auth(other)).get_json() or {}
trades_seen = hist.get("trades") or hist.get("history") or []
check("the other player's trade history keeps the trade, with the stand-in's name",
      any(t.get("with") == stand_in for t in trades_seen), hist)
check("the staff log keeps what was done to them, under the stand-in",
      int(one("SELECT COUNT(*) FROM staff_actions WHERE target_id = ? AND target_name = ? AND action = 'warn'", (vid, stand_in))) == 1
      and int(one("SELECT COUNT(*) FROM staff_actions WHERE target_name = 'delvictim'")) == 0)
check("  and one line that the account was deleted, by its owner",
      int(one("SELECT COUNT(*) FROM staff_actions WHERE action = 'account_deleted' AND target_id = ?", (vid,))) == 1)
check("  'account_deleted' is a kind the log knows, in the moderation group",
      "account_deleted" in app_module.STAFF_ACTION_KINDS
      and "account_deleted" in app_module.STAFF_ACTION_GROUPS["moderation"])
check("the report they made is kept, with the stand-in as reporter; the gifts ledger too",
      sql("SELECT reporter_name FROM chat_reports WHERE message_id = 999") == [(stand_in,)]
      and sql("SELECT username FROM owner_gifts WHERE user_id = ?", (vid,)) == [(stand_in,)])
check("they are signed out: the old token is a 401", client.get("/api/save", headers=auth(victim)).status_code == 401)
res = client.post("/api/auth/login", json={"username": "delvictim", "password": "password123"})
check("  the old name and password open nothing", res.status_code == 401, res.status_code)
again = register("delvictim")
check("the name is free again: a new account, a new id",
      bool(again) and uid("delvictim") != vid)
res = client.post("/api/auth/login", json={"username": stand_in, "password": "!"})
check("nobody can sign in as the stand-in", res.status_code in (400, 401), res.status_code)
check("the routes lock has it", any(re.search(r"/api/account/delete\b", line)
                                   for line in open(os.path.join(HERE, "routes.lock"), encoding="utf-8")))

# =============================================================================
section("AD-4  the foreign keys ignores and chat_image_holders had not")
conn = sqlite3.connect(DB_PATH)
fk = {t: {(r[2], r[3]) for r in conn.execute("PRAGMA foreign_key_list(%s)" % t)}
      for t in ("ignores", "chat_image_holders")}
conn.close()
check("ignores: both sides to users", fk["ignores"] == {("users", "user_id"), ("users", "ignored_id")}, fk)
check("chat_image_holders: to the picture and to the account",
      fk["chat_image_holders"] == {("chat_images", "image_id"), ("users", "user_id")}, fk)
old = sqlite3.connect(":memory:")
old.executescript("""
    CREATE TABLE users (id INTEGER PRIMARY KEY);
    CREATE TABLE chat_images (id TEXT PRIMARY KEY);
    INSERT INTO users VALUES (1), (2);
    INSERT INTO chat_images VALUES ('a');
    CREATE TABLE ignores (user_id INTEGER NOT NULL, ignored_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
                          PRIMARY KEY (user_id, ignored_id));
    INSERT INTO ignores VALUES (1, 2, 5), (1, 99, 5);
    CREATE TABLE chat_image_holders (image_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                                     created_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (image_id, user_id));
    INSERT INTO chat_image_holders VALUES ('a', 1, 0), ('gone', 1, 0);
""")
rebuilt = [chat_schema._with_foreign_keys(old, t, chat_schema.REBUILT[t], keep) for t, keep in (
    ("ignores", "SELECT user_id, ignored_id, created_at FROM ignores WHERE user_id IN (SELECT id FROM users)"
                " AND ignored_id IN (SELECT id FROM users)"),
    ("chat_image_holders", "SELECT image_id, user_id, created_at FROM chat_image_holders"
                           " WHERE user_id IN (SELECT id FROM users) AND image_id IN (SELECT id FROM chat_images)"))]
check("a table made before them is rebuilt with them, once", rebuilt == [True, True]
      and len(old.execute("PRAGMA foreign_key_list(ignores)").fetchall()) == 2
      and not chat_schema._with_foreign_keys(old, "ignores", chat_schema.REBUILT["ignores"], "SELECT 1"))
check("  keeping every row whose ends exist, and only those",
      old.execute("SELECT * FROM ignores").fetchall() == [(1, 2, 5)]
      and old.execute("SELECT image_id FROM chat_image_holders").fetchall() == [("a",)])
old.commit()    # the pragma is a no-op inside a transaction
old.execute("PRAGMA foreign_keys = ON")
old.execute("DELETE FROM users WHERE id = 2")
check("  and an account that goes takes its rows with it now",
      old.execute("SELECT COUNT(*) FROM ignores").fetchone()[0] == 0)

print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for f in failures:
        print("  - " + f)
sys.exit(1 if failed else 0)
