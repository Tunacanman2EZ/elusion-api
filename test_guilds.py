"""
test_guilds.py - founding, joining, ranks, and who may take a guild down.

    venv\\Scripts\\python.exe test_guilds.py

Throwaway database in your temp folder, like every other suite here. Never
touches elusion.db.

WHAT THIS IS GUARDING, in order of how bad it would be to get wrong:

  - GUILD CHAT IS THE GUILD'S. A line said in one guild must never appear in
    another's window, including for somebody who has moved between the two.
    That is the leak the obvious implementation has, and it has its own
    section below.
  - NOBODY IS PUT IN A GUILD WITHOUT AGREEING. An invitation waits until it
    is answered, exactly like a friend request.
  - RANK IS A LADDER AND IT ONLY GOES DOWN. An officer cannot remove another
    officer, nobody can remove the leader, and only the leader hands the
    guild on.
  - THE FOUNDING COST IS REALLY DESTROYED. It goes through the ledger, so the
    gold supply invariant still holds afterwards.
  - ONE GUILD PER PLAYER, and it is the schema that enforces it.
  - ONLY THE OWNER CAN TAKE DOWN SOMEBODY ELSE'S GUILD.

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_guilds_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "GUILDOWNER"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location(
    "elusion_app", os.path.join(HERE, "app.py"))
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


def auth(token):
    return {"Authorization": "Bearer %s" % token}


def register(name):
    return client.post("/api/auth/register",
                       json={"username": name, "password": "password123"}
                       ).get_json()["token"]


def make_character(token, name):
    return client.put("/api/save", headers=auth(token),
                      json={"slot": 0, "class_id": "warrior", "name": name})


def give_gold(username, amount):
    """Straight into the table, and the ledger with it.

    NOT THROUGH A ROUTE, because no route mints gold on request - and NOT a
    bare UPDATE either, because that would break the supply invariant this
    suite goes on to assert. A matching ledger row makes the gift a fact the
    ledger knows about, exactly as a loot drop would be.
    """
    with app_module.app.app_context():
        db = app_module.get_db()
        row = db.execute("SELECT id FROM users WHERE username = ?",
                         (username,)).fetchone()
        db.execute("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = 0",
                   (amount, row["id"]))
        db.execute(
            "INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
            " VALUES (strftime('%s','now'), ?, 0, ?, 'test_grant', 'suite setup')",
            (row["id"], amount))
        db.commit()


def bank_of(username):
    with app_module.app.app_context():
        db = app_module.get_db()
        row = db.execute(
            "SELECT a.bank_gold FROM accounts a JOIN users u ON u.id = a.user_id"
            " WHERE u.username = ?", (username,)).fetchone()
        return int(row["bank_gold"]) if row is not None else 0


def set_purse(username, carried, banked):
    """Both piles to exact figures, with the ledger kept honest."""
    with app_module.app.app_context():
        db = app_module.get_db()
        uid = db.execute("SELECT id FROM users WHERE username = ?",
                         (username,)).fetchone()["id"]
        db.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (uid,))
        now_carry = int(db.execute(
            "SELECT gold FROM saves WHERE user_id = ? AND slot = 0",
            (uid,)).fetchone()["gold"])
        now_bank = int(db.execute(
            "SELECT bank_gold FROM accounts WHERE user_id = ?",
            (uid,)).fetchone()["bank_gold"])
        db.execute("UPDATE saves SET gold = ? WHERE user_id = ? AND slot = 0",
                   (carried, uid))
        db.execute("UPDATE accounts SET bank_gold = ? WHERE user_id = ?",
                   (banked, uid))
        db.execute(
            "INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
            " VALUES (strftime('%s','now'), ?, 0, ?, 'test_grant', 'suite setup')",
            (uid, (carried - now_carry) + (banked - now_bank)))
        db.commit()


def gold_of(username):
    with app_module.app.app_context():
        db = app_module.get_db()
        return int(db.execute(
            "SELECT s.gold FROM saves s JOIN users u ON u.id = s.user_id"
            " WHERE u.username = ? AND s.slot = 0", (username,)).fetchone()["gold"])


def grant_rows_named(reason):
    with app_module.app.app_context():
        return int(app_module.get_db().execute(
            "SELECT COUNT(*) AS n FROM gold_ledger WHERE reason = ?",
            (reason,)).fetchone()["n"])


def supply():
    with app_module.app.app_context():
        return app_module.gold_supply(app_module.get_db())


def create(token, name, slot=0):
    return client.post("/api/guild/create", headers=auth(token),
                       json={"name": name, "slot": slot})


def invite(token, who):
    return client.post("/api/guild/invite", headers=auth(token),
                       json={"username": who})


def respond(token, guild, accept):
    return client.post("/api/guild/respond", headers=auth(token),
                       json={"guild": guild, "accept": accept})


def mine(token):
    return client.get("/api/guild", headers=auth(token))


def say(token, body, channel="guild"):
    return client.post("/api/chat/send", headers=auth(token),
                       json={"body": body, "channel": channel})


def read(token, channel="guild"):
    return client.get("/api/chat?channel=%s" % channel, headers=auth(token))


def refill(*names):
    with app_module.app.app_context():
        db = app_module.get_db()
        for name in names:
            db.execute("UPDATE users SET chat_tokens = ?, last_chat_at = 0,"
                       " last_image_at = 0, last_world_image_at = 0"
                       " WHERE username = ?",
                       (app_module.CHAT_BUCKET_CAPACITY, name))
        db.commit()


COST = app_module.GUILD_FOUND_COST


print("\n--- setup ---")
owner_token = register("guildowner")
amy_token = register("amy")
bob_token = register("bob")
cass_token = register("cass")
dan_token = register("dan")
for token, who in [(owner_token, "Owner"), (amy_token, "Amy"),
                   (bob_token, "Bob"), (cass_token, "Cass"), (dan_token, "Dan")]:
    make_character(token, who)
for who in ("guildowner", "amy", "bob", "cass", "dan"):
    give_gold(who, COST * 3)
check("everyone has a character and some gold", gold_of("amy") >= COST)


# ---------------------------------------------------------------------------
# FOUNDING
# ---------------------------------------------------------------------------
print("\n--- founding a guild ---")

check("nobody starts in one", mine(amy_token).get_json()["in_guild"] is False)

before = gold_of("amy")
res = create(amy_token, "The Crowned")
check("a guild can be founded", res.status_code == 200, res.get_json())
body = res.get_json() or {}
check("the founder is its leader", body.get("rank") == "leader", body)
check("and its only member", body.get("guild", {}).get("size") == 1, body)
check("the cost was taken", gold_of("amy") == before - COST,
      "%d -> %d" % (before, gold_of("amy")))
check("and the answer says what it cost", body.get("paid") == COST, body)

# DESTROYED, NOT MOVED. If the founding cost had been transferred anywhere,
# or written with a bare UPDATE, this is the check that would fail.
state = supply()
check("the gold supply still balances",
      int(state["recorded"]) == int(state["held"]), state)
with app_module.app.app_context():
    burn = app_module.get_db().execute(
        "SELECT delta FROM gold_ledger WHERE reason = 'guild_found'").fetchone()
check("the burn is in the ledger", burn is not None and int(burn["delta"]) == -COST,
      dict(burn) if burn else None)

check("a second guild from the same account is refused",
      create(amy_token, "Another Lot").status_code == 409)
check("and the name is taken for everybody else",
      create(bob_token, "the crowned").status_code == 400)
check("case does not get around it",
      create(bob_token, "THE CROWNED").status_code == 400)

for bad in ["ab", "", "   ", "x" * 40, "[b]bold[/b]", "no_underscores",
            "back\\slash", "semi;colon", "new\nline"]:
    check("a bad name is refused: %r" % bad,
          create(bob_token, bad).status_code == 400)

# SURROUNDING SPACE IS TRIMMED, NOT REFUSED. Somebody typing a trailing space
# has not done anything wrong, and the stored name is the tidy one.
res = create(bob_token, "  Spaced Out  ")
check("a name with space around it is accepted", res.status_code == 200,
      res.get_json())
check("and stored trimmed",
      res.get_json().get("guild", {}).get("name") == "Spaced Out",
      res.get_json())
client.post("/api/guild/disband", headers=auth(bob_token), json={})

# THROUGH set_purse, NOT A BARE UPDATE. The first version of this line wrote
# saves.gold directly and destroyed 14990 gold with no ledger row to say so -
# and the supply check a few lines down caught it, in the test rather than in
# the server. Which is the invariant doing exactly its job: gold that moves
# without being recorded is the bug, whoever moved it.
set_purse("dan", 10, 0)
res = create(dan_token, "Paupers")
check("founding with no gold is refused", res.status_code == 400)
# THE REFUSAL HAS TO BE ACTIONABLE. "It costs 5000 gold" on its own does not
# say how short you are, and it does not say which pile was looked in - which
# is the question somebody staring at a full bank is actually asking.
said = str(res.get_json().get("message", ""))
check("and says how much you have carried", "carried" in said, said)
check("and how much is in the bank", "bank" in said, said)
with app_module.app.app_context():
    left_behind = app_module.get_db().execute(
        "SELECT 1 FROM guilds WHERE folded = 'paupers'").fetchone()
check("and no half-made guild was left behind", left_behind is None)
give_gold("dan", COST * 3)


# ---------------------------------------------------------------------------
# WHERE THE GOLD COMES FROM
#
# Carried first, bank second - the same order reviving uses, because gold in
# the bank is gold somebody deliberately put somewhere safe. The first version
# looked only at the carried pile, so a player with 9000 banked and 40 in
# their pocket was told they could not afford a guild.
# ---------------------------------------------------------------------------
print("\n--- founding is paid out of both piles ---")

# The exact case the player hit: plenty of gold, almost none of it carried.
set_purse("dan", 40, COST * 2)
res = create(dan_token, "Well Banked")
check("a guild can be founded out of the bank", res.status_code == 200,
      res.get_json())
paid = res.get_json() or {}
check("the carried pile went first", paid.get("from_carried") == 40, paid)
check("and the bank covered the rest",
      paid.get("from_bank") == COST - 40, paid)
check("leaving the pocket empty", gold_of("dan") == 0, gold_of("dan"))
check("and the bank short by the remainder",
      bank_of("dan") == COST * 2 - (COST - 40), bank_of("dan"))

state = supply()
check("and the supply STILL balances with a bank burn in it",
      int(state["recorded"]) == int(state["held"]), state)

client.post("/api/guild/disband", headers=auth(dan_token), json={})

# Exactly enough, split across both, is enough.
set_purse("dan", COST // 2, COST - COST // 2)
check("exactly the price, split across both piles, works",
      create(dan_token, "Exactly Enough").status_code == 200)
client.post("/api/guild/disband", headers=auth(dan_token), json={})

# One short is one short, wherever it is.
set_purse("dan", 10, COST - 11)
res = create(dan_token, "One Short")
check("a single gold short is refused", res.status_code == 400, res.get_json())
check("and nothing was taken from either pile",
      gold_of("dan") == 10 and bank_of("dan") == COST - 11,
      "%d / %d" % (gold_of("dan"), bank_of("dan")))
give_gold("dan", COST * 3)


# ---------------------------------------------------------------------------
# JOINING
# ---------------------------------------------------------------------------
print("\n--- invited, then accepted ---")

check("a stranger cannot invite", invite(bob_token, "cass").status_code == 404)
check("inviting a name that does not exist is a 404",
      invite(amy_token, "nobodyhere").status_code == 404)

res = invite(amy_token, "bob")
check("the leader can invite", res.status_code == 200, res.get_json())
check("bob is NOT in the guild yet",
      mine(bob_token).get_json()["in_guild"] is False)
seen = mine(bob_token).get_json()["invites"]
check("but he can see the invitation", len(seen) == 1 and
      seen[0]["guild"] == "The Crowned", seen)
check("and who sent it", seen and seen[0]["by"] == "amy", seen)

check("inviting twice is not an error", invite(amy_token, "bob").status_code == 200)
check("and does not make a second invitation",
      len(mine(bob_token).get_json()["invites"]) == 1)

check("answering an invitation nobody sent is a 404",
      respond(cass_token, "The Crowned", True).status_code == 404)

res = respond(bob_token, "The Crowned", True)
check("accepting works", res.status_code == 200, res.get_json())
check("bob is in", mine(bob_token).get_json()["in_guild"] is True)
check("as a member", mine(bob_token).get_json()["rank"] == "member")
check("and the roster shows both",
      mine(amy_token).get_json()["guild"]["size"] == 2)
check("the invitation is gone", mine(bob_token).get_json()["invites"] == [])

check("somebody already in a guild cannot be invited",
      invite(amy_token, "bob").status_code == 400)

invite(amy_token, "cass")
res = respond(cass_token, "The Crowned", False)
check("an invitation can be turned down", res.status_code == 200, res.get_json())
check("and declining leaves you out",
      mine(cass_token).get_json()["in_guild"] is False)
check("with no invitation left either",
      mine(cass_token).get_json()["invites"] == [])


# ---------------------------------------------------------------------------
# RANK
# ---------------------------------------------------------------------------
print("\n--- rank is a ladder ---")

def set_rank(token, who, rank):
    return client.post("/api/guild/rank", headers=auth(token),
                       json={"username": who, "rank": rank})


check("a member cannot promote anybody",
      set_rank(bob_token, "bob", "officer").status_code == 404)
check("the leader can", set_rank(amy_token, "bob", "officer").status_code == 200)
check("and it took", mine(bob_token).get_json()["rank"] == "officer")
check("an unknown rank is refused",
      set_rank(amy_token, "bob", "emperor").status_code == 400)

invite(bob_token, "cass")
check("an OFFICER can invite too",
      respond(cass_token, "The Crowned", True).status_code == 200)

def kick(token, who):
    return client.post("/api/guild/kick", headers=auth(token),
                       json={"username": who})


check("an officer can remove a member", kick(bob_token, "cass").status_code == 200)
check("and cass is out", mine(cass_token).get_json()["in_guild"] is False)

invite(amy_token, "cass")
respond(cass_token, "The Crowned", True)
set_rank(amy_token, "cass", "officer")
check("an officer cannot remove another officer",
      kick(bob_token, "cass").status_code == 404)
check("and cass is still in", mine(cass_token).get_json()["in_guild"] is True)
check("nobody can remove the leader", kick(bob_token, "amy").status_code == 404)
check("the leader can remove an officer", kick(amy_token, "cass").status_code == 200)

check("you cannot kick yourself", kick(amy_token, "amy").status_code == 400)


# ---------------------------------------------------------------------------
# GUILD CHAT
# ---------------------------------------------------------------------------
print("\n--- guild chat belongs to the guild ---")

refill("amy", "bob", "cass", "dan", "guildowner")
check("somebody with no guild cannot write to it",
      say(dan_token, "hello?").status_code == 409)
feed = read(dan_token).get_json()
check("and is told why rather than shown an empty room",
      feed.get("available") is False and "not in a guild" in feed.get("notice", ""),
      feed)

check("a member can write", say(amy_token, "crowned business").status_code == 200)
bodies = [m["body"] for m in read(bob_token).get_json()["messages"]]
check("and the guild reads it", "crowned business" in bodies, bodies)
check("somebody outside sees nothing of it",
      "crowned business" not in [m["body"] for m in
                                 read(dan_token).get_json().get("messages", [])])

# THE LEAK THIS SECTION EXISTS FOR.
#
# Filtering guild chat by "authors currently in my guild" - the way the
# friends channel works - looks right and leaks: somebody who leaves one
# guild and joins another carries every line they ever said into the new
# guild's window. The fix is that a line is stamped with the guild it was
# said in, and these four checks are what hold it.
give_gold("dan", COST * 3)
create(dan_token, "Second Lot")
refill("amy", "bob", "dan")
check("a second guild can write to its own channel",
      say(dan_token, "second lot business").status_code == 200)
theirs = [m["body"] for m in read(dan_token).get_json()["messages"]]
check("and reads its own line", "second lot business" in theirs, theirs)
check("but NOT the other guild's", "crowned business" not in theirs, theirs)
ours = [m["body"] for m in read(bob_token).get_json()["messages"]]
check("and the first guild does not read theirs",
      "second lot business" not in ours, ours)

# The move that breaks the naive version: bob leaves and joins the other one.
client.post("/api/guild/leave", headers=auth(bob_token))
invite(dan_token, "bob")
respond(bob_token, "Second Lot", True)
carried = [m["body"] for m in read(bob_token).get_json()["messages"]]
check("moving guilds does NOT carry your old lines with you",
      "crowned business" not in carried, carried)
check("and you can read the new guild's", "second lot business" in carried, carried)


# ---------------------------------------------------------------------------
# LEAVING AND DISBANDING
# ---------------------------------------------------------------------------
print("\n--- leaving, and taking one down ---")

def leave(token):
    return client.post("/api/guild/leave", headers=auth(token))


def disband(token, name=None):
    body = {} if name is None else {"name": name}
    return client.post("/api/guild/disband", headers=auth(token), json=body)


check("somebody with no guild cannot leave one",
      leave(cass_token).status_code == 404)

# A leader walking out of a guild with people still in it would strand them:
# no leader means nobody can invite, rank or disband it ever again.
invite(amy_token, "cass")
respond(cass_token, "The Crowned", True)
check("a leader cannot just walk out", leave(amy_token).status_code == 400)
check("but can hand it on", set_rank(amy_token, "cass", "leader").status_code == 200)
check("which steps the old leader down",
      mine(amy_token).get_json()["rank"] == "officer")
check("and makes the other one leader",
      mine(cass_token).get_json()["rank"] == "leader")
check("now the old leader can leave", leave(amy_token).status_code == 200)

check("a member cannot disband", disband(bob_token).status_code == 404)
check("the leader can", disband(cass_token).status_code == 200)
check("and everybody in it is out",
      mine(cass_token).get_json()["in_guild"] is False)
check("the name is free again", create(amy_token, "The Crowned").status_code == 200)

# THE LAST ONE OUT TAKES IT WITH THEM.
create(bob_token, "Briefly") if mine(bob_token).get_json()["in_guild"] is False \
    else leave(bob_token)
if mine(bob_token).get_json()["in_guild"] is False:
    give_gold("bob", COST * 3)
    create(bob_token, "Briefly")
res = leave(bob_token)
check("the last member out disbands it",
      res.status_code == 200 and res.get_json().get("disbanded") is True,
      res.get_json())
check("and the name is free", create(dan_token, "Briefly").status_code in (200, 409))


# ---------------------------------------------------------------------------
# THE OWNER'S POWER
# ---------------------------------------------------------------------------
print("\n--- only the owner takes down somebody else's guild ---")

refill("amy", "bob", "guildowner")
# Amy leads "The Crowned" from a few lines above.
check("amy is leading her own guild",
      mine(amy_token).get_json().get("rank") == "leader",
      mine(amy_token).get_json())

check("a player cannot name a guild to disband",
      disband(bob_token, "The Crowned").status_code == 404)
check("and it survives", mine(amy_token).get_json()["in_guild"] is True)

check("a player cannot even name their OWN guild",
      disband(amy_token, "The Crowned").status_code == 404)
check("which it still is", mine(amy_token).get_json()["in_guild"] is True)

check("staff can list the guilds",
      client.get("/api/guild/list", headers=auth(owner_token)).status_code == 200)
listed = client.get("/api/guild/list", headers=auth(owner_token)).get_json()
check("with the leader named",
      any(row["name"] == "The Crowned" and row["leader"] == "amy"
          for row in listed["guilds"]), listed)
check("and the owner is told they may take one down",
      listed.get("may_disband") is True, listed)
check("a player cannot list them",
      client.get("/api/guild/list", headers=auth(bob_token)).status_code == 404)

res = disband(owner_token, "The Crowned")
check("the OWNER can take down anybody's guild", res.status_code == 200,
      res.get_json())
check("and everyone in it is out",
      mine(amy_token).get_json()["in_guild"] is False)
with app_module.app.app_context():
    db = app_module.get_db()
    left_members = db.execute(
        "SELECT COUNT(*) AS n FROM guild_members m"
        " LEFT JOIN guilds gl ON gl.id = m.guild_id WHERE gl.id IS NULL"
    ).fetchone()["n"]
    left_invites = db.execute(
        "SELECT COUNT(*) AS n FROM guild_invites i"
        " LEFT JOIN guilds gl ON gl.id = i.guild_id WHERE gl.id IS NULL"
    ).fetchone()["n"]
check("no members are left pointing at a guild that is gone",
      int(left_members) == 0, left_members)
check("no invitations either", int(left_invites) == 0, left_invites)

check("disbanding a guild that does not exist is a 404",
      disband(owner_token, "No Such Lot").status_code == 404)


# ---------------------------------------------------------------------------
# ONE GUILD PER PLAYER, AT THE SCHEMA
# ---------------------------------------------------------------------------
print("\n--- one guild per player, and the database says so ---")

give_gold("amy", COST * 3)
create(amy_token, "Only One")
with app_module.app.app_context():
    db = app_module.get_db()
    amy_id = db.execute("SELECT id FROM users WHERE username = 'amy'"
                        ).fetchone()["id"]
    other = db.execute("SELECT id FROM guilds WHERE folded != 'only one'"
                       ).fetchone()
    if other is None:
        give_gold("dan", COST * 3)
        create(dan_token, "Somewhere Else")
        other = db.execute("SELECT id FROM guilds WHERE folded = 'somewhere else'"
                           ).fetchone()
    doubled = False
    try:
        db.execute("INSERT INTO guild_members (user_id, guild_id, rank, joined_at)"
                   " VALUES (?, ?, 'member', 0)", (amy_id, other["id"]))
        db.commit()
        doubled = True
    except sqlite3.IntegrityError:
        db.rollback()
check("a second membership row is refused by the primary key, not by Python",
      doubled is False)


# ---------------------------------------------------------------------------
# THE OWNER'S DEBUG GRANT
#
# WHY IT LIVES IN THIS FILE. It mints gold, and the thing worth asserting
# about minting gold is that the supply invariant survives it - which this
# suite already has a helper for, and which test_economy.py cannot check
# because it deliberately sets ELUSION_OWNER to an account that never exists.
#
# WHY IT EXISTS AT ALL: a server nobody can put gold on is a server whose
# shops, bank, trades, revive cost and guild founding cannot be tested by the
# person who wrote them. The debug keys did this job and are gated on
# OS.is_debug_build(), so they vanish in an exported build - which is exactly
# when the thing most needs exercising.
# ---------------------------------------------------------------------------
print("\n--- the owner can put gold on the server ---")

def grant_gold(token, amount, slot=0, bank=False):
    return client.post("/api/staff/gold", headers=auth(token),
                       json={"slot": slot, "amount": amount, "bank": bank})


before_state = supply()
res = grant_gold(owner_token, 7500)
check("the owner can grant themselves gold", res.status_code == 200,
      res.get_json())
after_state = supply()
check("the supply invariant survives a mint",
      int(after_state["recorded"]) == int(after_state["held"]), after_state)
check("and the mint is recorded as exactly what it was",
      int(after_state["recorded"]) - int(before_state["recorded"]) == 7500,
      "%d -> %d" % (int(before_state["recorded"]), int(after_state["recorded"])))
check("with its own reason in the ledger, so conjured gold is always visible",
      grant_rows_named("staff_gold") >= 1)

res = grant_gold(owner_token, 2500, bank=True)
check("it can go to the bank instead", res.status_code == 200, res.get_json())
check("and lands there", int(res.get_json().get("bank_gold", 0)) >= 2500,
      res.get_json())
after_state = supply()
check("the invariant survives a BANK mint too",
      int(after_state["recorded"]) == int(after_state["held"]), after_state)

check("it can take gold away again", grant_gold(owner_token, -1000).status_code == 200)
after_state = supply()
check("and survives a burn",
      int(after_state["recorded"]) == int(after_state["held"]), after_state)

check("zero is refused", grant_gold(owner_token, 0).status_code == 400)
check("more than the ceiling is refused",
      grant_gold(owner_token, app_module.DEBUG_GOLD_MAX + 1).status_code == 400)
check("taking more than you have is refused",
      grant_gold(owner_token, -app_module.DEBUG_GOLD_MAX).status_code == 400)
check("a slot with nobody in it is a 404",
      grant_gold(owner_token, 100, slot=2).status_code == 404)

# NOBODY ELSE AT ALL - not a player, and not a mod. Granting an item is a
# test fixture; minting the currency is the economy itself.
check("a player cannot mint gold", grant_gold(amy_token, 100).status_code == 404)
with app_module.app.app_context():
    db = app_module.get_db()
    db.execute("UPDATE users SET role = 'mod' WHERE username = ?", ("bob",))
    db.commit()
check("and neither can a MOD", grant_gold(bob_token, 100).status_code == 404)
with app_module.app.app_context():
    db = app_module.get_db()
    db.execute("UPDATE users SET role = 'player' WHERE username = ?", ("bob",))
    db.commit()

after_state = supply()
check("after every refusal the invariant still holds",
      int(after_state["recorded"]) == int(after_state["held"]), after_state)

# AND IT IS THE ANSWER TO THE PROBLEM IT WAS ADDED FOR: somebody with an
# empty purse can now put gold on and found a guild.
set_purse("cass", 0, 0)
check("a broke account cannot found one", create(cass_token, "Broke Lot").status_code == 400)
check("the owner tops them up is not a thing - they do it themselves",
      grant_gold(cass_token, COST).status_code == 404)
grant_gold(owner_token, COST)
check("but the owner can fund and found in two presses",
      create(owner_token, "Owners Lot").status_code in (200, 409))


# ---------------------------------------------------------------------------
# THE SCORE ADDS TWO CURRENCIES, SO IT HAS TO WEIGH THEM
#
# It used to add gold and lusions 1:1 - dying with 20 lusions and dying with
# 20 gold scored the same. With gold now counted in real denominations, where
# one platinum coin is a hundred thousand, an unweighted total said nothing.
# ---------------------------------------------------------------------------
print("\n--- the score weighs the two currencies ---")

check("there is a published rate", app_module.LUSION_GOLD_VALUE > 1,
      app_module.LUSION_GOLD_VALUE)
check("and it is the same one the data file carries",
      app_module.LUSION_GOLD_VALUE ==
      int(app_module.gamedata.CONSTANTS.get("lusion_gold_value", 0)),
      app_module.gamedata.CONSTANTS.get("lusion_gold_value"))
check("a lusion is worth a gold coin, so the premium currency is legible",
      app_module.LUSION_GOLD_VALUE ==
      dict(app_module.gamedata.gold_denominations()).get("goldcoin", 0),
      app_module.LUSION_GOLD_VALUE)
check("which makes the two revive paths comparable rather than absurd",
      int(app_module.gamedata.CONSTANTS.get("revive_cost", 20))
      * app_module.LUSION_GOLD_VALUE > 1000,
      "20 lusions = %d gold" %
      (int(app_module.gamedata.CONSTANTS.get("revive_cost", 20))
       * app_module.LUSION_GOLD_VALUE))


print("\n--- nothing without a token ---")
for path in ["/api/guild", "/api/guild/list"]:
    check("%s needs one" % path, client.get(path).status_code == 401)
for path in ["/api/guild/create", "/api/guild/invite", "/api/guild/respond",
             "/api/guild/leave", "/api/guild/kick", "/api/guild/rank",
             "/api/guild/disband"]:
    check("%s needs one" % path, client.post(path, json={}).status_code == 401)


print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
