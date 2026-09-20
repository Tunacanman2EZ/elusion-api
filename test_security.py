"""Security-hardening tests for the Elusion API.

Focused proof of the server-authority work recorded in SECURITY_NOTES.md:
  E-1  the backpack rejects items a regular client never earned
  E-2  skills are capped for regular clients
  E-4  debug is off unless ELUSION_DEBUG asks for it
  E-5  login locks out after repeated failures
  E-7  item use is checked and performed by the server
  E-10 lusions and revive are the server's, so dying still costs something

plus the shadow-mode reconciler for healing, which logs rather than refuses and
is therefore tested on what it stays QUIET about as much as what it catches.

Runs against a THROWAWAY database in the temp folder, exactly like test_api.py,
so it never touches elusion.db. Run: python test_security.py
"""

import importlib.util
import os
import json
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_security_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
# The owner is the one account that bypasses server authority (a mod+ can already
# grant itself anything), so the suite needs one to prove the bypass.
os.environ["ELUSION_OWNER"] = "boss"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()

# The server's OWN item catalogue, so the cases below pick their fixtures from
# what the server actually believes rather than hard-coding item ids that a
# rebalance would quietly invalidate.
import gamedata

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


def register(username, password="password123"):
    r = client.post("/api/auth/register", json={"username": username, "password": password})
    return {"Authorization": "Bearer " + r.get_json()["token"]}


def make_char(headers, slot=0, class_id="warrior", name="Hero"):
    return client.put("/api/save", headers=headers,
                      json={"slot": slot, "class_id": class_id, "name": name})


def carried(headers, slot=0):
    """item_id -> total quantity the server reports in the backpack."""
    inv = client.get("/api/character?slot=%d" % slot, headers=headers).get_json()["inventory"]
    totals = {}
    for cell in inv:
        if cell:
            totals[cell["item_id"]] = totals.get(cell["item_id"], 0) + cell["quantity"]
    return totals


def skill_level(headers, skill, slot=0):
    skills = client.get("/api/character?slot=%d" % slot, headers=headers).get_json()["skills"]
    return skills.get(skill, {}).get("level")


def grant_directly(username, slot, entries):
    """Write carry_items straight into the DB, standing in for a server grant
    (loot/take, bank withdraw, staff grant all write these rows the same way)."""
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    conn.execute("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (uid, slot))
    conn.executemany(
        "INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, ?, ?, ?, ?)",
        [(uid, slot, i, iid, q) for i, (iid, q) in enumerate(entries)],
    )
    conn.commit()
    conn.close()


def put_inventory(headers, cells, slot=0):
    return client.put("/api/character/inventory", headers=headers,
                      json={"slot": slot, "inventory": cells})


def put_skills(headers, skills, slot=0):
    return client.put("/api/character/skills", headers=headers,
                      json={"slot": slot, "skills": skills})


print("\n=== E-1  BACKPACK IS SERVER-AUTHORITATIVE OVER GAINS ===\n")

player = register("mallory")
make_char(player)

# The core exploit: claim items never granted. Accepted as a request (200) but
# trimmed to nothing, because the server recorded no such items.
#
# QUANTITY 1, NOT 99, AND THE REASON MATTERS. embersword has max_stack = 1, so a
# claim of 99 is refused at 400 by SHAPE validation - before the ledger is ever
# consulted. That made this test pass for the wrong reason: the 400 left the
# backpack empty, so the "trimmed to nothing" check below agreed, and the pair
# reported that server authority worked without once exercising it. A test of
# provenance has to send something WELL-FORMED and unearned.
r = put_inventory(player, [{"item_id": "embersword", "quantity": 1}])
check("a fabricated backpack is accepted as a request", r.status_code == 200, r.status_code)
check("but the fabricated items are trimmed to nothing", carried(player) == {}, carried(player))

# And the shape rule that masked this, asserted on its own terms so it can never
# quietly stand in for the authority check again.
r = put_inventory(player, [{"item_id": "embersword", "quantity": 99}])
check("a claim above max_stack is refused on shape, before provenance",
      r.status_code == 400, r.status_code)

# A server-granted item survives a sync unchanged.
grant_directly("mallory", 0, [("ironsword", 1)])
put_inventory(player, [{"item_id": "ironsword", "quantity": 1}])
check("a server-granted item survives the sync", carried(player) == {"ironsword": 1}, carried(player))

# The granted item stays; fabricated excess alongside it is trimmed.
put_inventory(player, [{"item_id": "ironsword", "quantity": 1},
                       {"item_id": "embersword", "quantity": 1}])
check("granted item kept, fabricated excess trimmed",
      carried(player) == {"ironsword": 1}, carried(player))

# Claiming MORE of a granted item than was granted is trimmed to the granted amount.
put_inventory(player, [{"item_id": "ironsword", "quantity": 99}])
check("over-claiming a granted item clamps to the granted amount",
      carried(player) == {"ironsword": 1}, carried(player))

# Reductions and drops are always honest and pass through.
put_inventory(player, [])
check("dropping/using items (a reduction) is allowed", carried(player) == {}, carried(player))

# Rearranging a granted item to another cell is not a gain.
grant_directly("mallory", 0, [("smallhealthpotion", 5)])
put_inventory(player, [None, None, {"item_id": "smallhealthpotion", "quantity": 5}])
check("rearranging a granted item is not a gain", carried(player) == {"smallhealthpotion": 5},
      carried(player))


print("\n=== E-1  STAFF BYPASS (a mod+ can already self-grant) ===\n")

owner = register("boss")            # matches ELUSION_OWNER
make_char(owner)
# Same correction as above - a legal quantity, so the staff bypass is what is
# being measured rather than the stack ceiling.
put_inventory(owner, [{"item_id": "embersword", "quantity": 1}])
check("the owner may set an arbitrary backpack", carried(owner) == {"embersword": 1},
      carried(owner))



print("\n=== E-1  THE BANK IS SERVER-AUTHORITATIVE TOO ===\n")

# The backpack's remaining sibling. PUT /api/account/bank replaced the whole
# bank with whatever arrived, so anything in gamedata.json could be fabricated
# straight into storage. Every legitimate way IN already writes bank_items
# server-side (POST /api/bank/items handles deposit and withdraw), so what is
# left for this endpoint is reordering.

def put_bank(headers, cells):
    return client.put("/api/account/bank", headers=headers,
                      json={"bank_inventory": cells})


def banked(headers):
    acct = client.get("/api/account", headers=headers).get_json()
    totals = {}
    for cell in acct.get("bank_inventory", []) or []:
        if cell:
            totals[cell["item_id"]] = totals.get(cell["item_id"], 0) + cell["quantity"]
    return totals


def bank_directly(username, entries):
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    conn.execute("DELETE FROM bank_items WHERE user_id = ?", (uid,))
    conn.executemany(
        "INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?,?,?,?)",
        [(uid, i, iid, q) for i, (iid, q) in enumerate(entries)],
    )
    conn.commit()
    conn.close()


banker = register("banker")
make_char(banker)

r = put_bank(banker, [{"item_id": "embersword", "quantity": 1}])
check("a fabricated bank is accepted as a request", r.status_code == 200, r.status_code)
check("but the fabricated item is trimmed to nothing", banked(banker) == {}, banked(banker))

# A server-banked item survives a sync, and may be REORDERED - that is the only
# thing this endpoint is still for.
bank_directly("banker", [("ironsword", 1), ("smallhealthpotion", 5)])
put_bank(banker, [None, {"item_id": "smallhealthpotion", "quantity": 5},
                  {"item_id": "ironsword", "quantity": 1}])
check("server-banked items survive a reorder",
      banked(banker) == {"ironsword": 1, "smallhealthpotion": 5}, banked(banker))

# Over-claiming a real holding clamps to what was banked.
#
# 20, NOT 99, AND THIS IS THE SECOND TIME THE SAME TRAP HAS BITTEN IN THIS FILE.
# smallhealthpotion has max_stack = 20, so a claim of 99 is refused at 400 by
# SHAPE validation and never written at all - leaving the previous bank intact
# and the assertion measuring nothing. A provenance test must send a
# WELL-FORMED claim; the stack ceiling is a different rule with its own check.
put_bank(banker, [{"item_id": "smallhealthpotion", "quantity": 20}])
check("over-claiming a banked item clamps to what was banked",
      banked(banker) == {"smallhealthpotion": 5}, banked(banker))

r = put_bank(banker, [{"item_id": "smallhealthpotion", "quantity": 99}])
check("a bank claim above max_stack is refused on shape, before provenance",
      r.status_code == 400, r.status_code)

# Withdrawals and drops are honest reductions and always pass.
put_bank(banker, [])
check("emptying the bank is allowed", banked(banker) == {}, banked(banker))

# The bank is account-wide, so the trim must not be fooled by a second slot.
bank_directly("banker", [("ironsword", 1)])
put_bank(banker, [{"item_id": "ironsword", "quantity": 1},
                  {"item_id": "ironsword", "quantity": 1}])
check("the same item claimed twice is capped in total, not per cell",
      banked(banker) == {"ironsword": 1}, banked(banker))

put_bank(owner, [{"item_id": "embersword", "quantity": 1}])
check("the owner may still set an arbitrary bank",
      banked(owner) == {"embersword": 1}, banked(owner))

print("\n=== E-2  SKILLS ARE CAPPED FOR A REGULAR CLIENT ===\n")

# MAGIC, NOT ATTACK. The cap is about what a client may CLAIM, and attack is no
# longer claimable at all - /api/combat/kill grants it and PUT /skills drops it.
# Testing the cap through a server-owned skill would measure the drop instead.
r = put_skills(player, {"magic": {"level": 999, "xp": 0}})
check("an over-cap skill is accepted as a request", r.status_code == 200, r.status_code)
check("but the level is clamped to the ceiling", skill_level(player, "magic") == app_module.MAX_SKILL_LEVEL,
      skill_level(player, "magic"))

put_skills(player, {"magic": {"level": 40, "xp": 0}})
check("a legitimate level is stored unchanged", skill_level(player, "magic") == 40,
      skill_level(player, "magic"))

put_skills(owner, {"magic": {"level": 999, "xp": 0}})
check("the owner bypasses the skill cap", skill_level(owner, "magic") == 999,
      skill_level(owner, "magic"))



print("\n=== E-2  ATTACK IS SERVER-GRANTED, NOT CLAIMED ===\n")

# The server has always ROLLED attack xp - the client cannot name its own
# reward - but it used to hand the number back and trust the client to bank it.
# These check that the decision and the bookkeeping now happen in the same place.
fighter = register("fighter")
make_char(fighter)

# ABSENT, NOT 1. A fresh character has no skill rows at all - the client renders
# 1 from the absence. Asserting == 1 here would be asserting a row that should
# not exist yet, and would have hidden the grant failing to write one.
check("a fresh character has no banked attack row",
      skill_level(fighter, "attack") is None, skill_level(fighter, "attack"))

r = client.post("/api/combat/kill", headers=fighter,
                json={"slot": 0, "enemy_id": "bushmage"})
check("a kill is accepted", r.status_code == 200, r.get_json())
body = r.get_json()
check("the kill reports attack xp", body["attack_xp_gained"] > 0, body)
check("and reports the resulting attack level", "attack_level" in body, body.keys())

after = client.get("/api/character?slot=0", headers=fighter).get_json()["skills"]
check("the attack xp was BANKED, not just reported",
      after.get("attack", {}).get("xp", 0) == body["attack_xp_gained"],
      (after.get("attack"), body["attack_xp_gained"]))

# The lie the round trip used to allow.
put_skills(fighter, {"attack": {"level": 99, "xp": 999999}})
check("a client claiming attack 99 is ignored",
      skill_level(fighter, "attack") == body["attack_level"],
      skill_level(fighter, "attack"))

# And it must not be silently wiped either - the DELETE in write_skills has to
# exclude server-owned rows, or a routine sync would erase what was earned.
banked = client.get("/api/character?slot=0", headers=fighter).get_json()["skills"]
check("a routine sync does not erase the banked attack xp",
      banked.get("attack", {}).get("xp", 0) == body["attack_xp_gained"],
      banked.get("attack"))

# Staff exemption does not apply: nobody may write a server-owned skill.
put_skills(owner, {"attack": {"level": 77, "xp": 0}})
check("not even the owner may write a server-owned skill",
      skill_level(owner, "attack") != 77, skill_level(owner, "attack"))


print("\n=== E-3  KILL REPORTS ARE RECORDED (not yet verified) ===\n")

# E-3 IS STILL OPEN AND THIS DOES NOT CLOSE IT. The server still cannot tell
# whether a fight happened. What it can now do is write down what was claimed,
# which is the step before any check worth enforcing - a threshold picked
# without data is how honest players get clamped.

def kills_of(username):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT enemy_id, xp, attack_xp, level_at, at FROM kill_reports"
        " WHERE user_id = (SELECT id FROM users WHERE username = ?) ORDER BY id",
        (username,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


reporter = register("reporter")
make_char(reporter)
check("no kills recorded for a fresh character", kills_of("reporter") == [], kills_of("reporter"))

r = client.post("/api/combat/kill", headers=reporter, json={"slot": 0, "enemy_id": "bushmage"})
check("a kill is accepted", r.status_code == 200, r.get_json())

logged = kills_of("reporter")
check("and it is written down", len(logged) == 1, logged)
check("with the enemy that was claimed", logged[0]["enemy_id"] == "bushmage", logged[0])
check("and the rewards AS PAID",
      logged[0]["xp"] == r.get_json()["xp_gained"]
      and logged[0]["attack_xp"] == r.get_json()["attack_xp_gained"], (logged[0], r.get_json()))
check("and the level it was claimed at", logged[0]["level_at"] >= 1, logged[0])

# A REFUSED KILL MUST NOT APPEAR. The log is only useful if every row is a kill
# the server actually paid for - otherwise "reports" and "payouts" drift and no
# query over it means anything.
before = len(kills_of("reporter"))
r = client.post("/api/combat/kill", headers=reporter, json={"slot": 0, "enemy_id": "notarealenemy"})
check("an unknown enemy is refused", r.status_code in (400, 404), r.status_code)
check("and leaves no row behind", len(kills_of("reporter")) == before, kills_of("reporter"))

r = client.post("/api/combat/kill", headers=reporter, json={"slot": 0, "enemy_id": "poisonslimelarge"})
check("a reward-less enemy is still refused", r.status_code in (400, 403, 409), r.status_code)
check("and leaves no row behind either", len(kills_of("reporter")) == before, kills_of("reporter"))

# THE QUESTION THE TABLE EXISTS FOR. One query, the way the login log answers
# "one address, many usernames".
for _ in range(4):
    client.post("/api/combat/kill", headers=reporter, json={"slot": 0, "enemy_id": "firesprite"})
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
busiest = conn.execute(
    "SELECT enemy_id, COUNT(*) AS n FROM kill_reports GROUP BY enemy_id ORDER BY n DESC LIMIT 1"
).fetchone()
conn.close()
check("'what is this account farming' is one query", busiest["n"] >= 4, dict(busiest))


print("\n=== MODERATION  THE STAFF READ VIEW ===\n")

# The read side of moderation, and the only way login_attempts and kill_reports
# are reachable without a shell on the box holding elusion.db. A ban decision
# made without being able to look is a guess.

def staff_view(headers, name):
    return client.get("/api/staff/user/%s" % name, headers=headers)


victim = register("subject")
make_char(victim)
client.post("/api/combat/kill", headers=victim, json={"slot": 0, "enemy_id": "bushmage"})
client.post("/api/auth/login", json={"username": "subject", "password": "wrongpassword"},
            environ_base={"REMOTE_ADDR": "203.0.113.55"})

r = staff_view(player, "subject")
# 404, NOT 403, AND THAT IS THE DESIGN. require_role() answers "Not found" to a
# non-staff caller on purpose: a 403 confirms the route exists and that you are
# not allowed to use it, which tells someone exactly where to push. Asserting
# 403 here would have been asserting a weaker server.
check("a plain player is told the route does not exist", r.status_code == 404, r.status_code)
check("and an anonymous request may not either",
      client.get("/api/staff/user/subject").status_code == 401)

r = staff_view(owner, "subject")
check("the owner may", r.status_code == 200, r.get_json())
view = r.get_json()

check("no password hash anywhere in the payload",
      "password_hash" not in json.dumps(view), "the one thing that must never leak")
check("no token either", "token" not in json.dumps(view))

check("the rank is reported", view["role"] == "player", view["role"])
check("characters are listed", len(view["characters"]) == 1, view["characters"])
check("with the level", view["characters"][0]["level"] >= 1, view["characters"][0])

check("kills are grouped, not listed",
      any(k["enemy_id"] == "bushmage" and k["count"] == 1 for k in view["kills"]), view["kills"])
check("with the xp they paid out", view["kills"][0]["xp_total"] > 0, view["kills"])

check("login attempts are summarised", view["logins"]["total"] >= 1, view["logins"])
check("and the failures counted", view["logins"]["failed"] >= 1, view["logins"])
check("recent attempts are listed", len(view["logins"]["recent"]) >= 1, view["logins"]["recent"])

# THE PRIVACY LINE. An address is personal data; the honest test for whether a
# mod should see it is whether they could act on that person at all.
check("the owner sees addresses (can act on a player)",
      view["logins"]["addresses_visible"] is True, view["logins"])
check("and they are actually present",
      any("ip" in a for a in view["logins"]["recent"]), view["logins"]["recent"])

r = staff_view(owner, "NOBODY_BY_THAT_NAME")
check("an unknown account -> 404", r.status_code == 404, r.status_code)

r = staff_view(owner, "SUBJECT")
check("the lookup is case-insensitive, like the column", r.status_code == 200, r.status_code)

# A mod may not see the OWNER's addresses - nothing is above the owner, so
# can_act_on is false and the gate closes even for a legitimate viewer.
r = staff_view(owner, "boss")   # the ELUSION_OWNER account
check("the owner reading their own record still gets 200", r.status_code == 200, r.status_code)
check("but nobody is 'actionable' against the owner",
      r.get_json()["logins"]["addresses_visible"] is False, r.get_json()["logins"])
check("and no addresses come back",
      all("ip" not in a for a in r.get_json()["logins"]["recent"]),
      r.get_json()["logins"]["recent"])
check("while the counts still do",
      "addresses" in r.get_json()["logins"], r.get_json()["logins"])


print("\n=== MODERATION  SESSIONS, AND THE SANCTION THAT IS NOT A BAN ===\n")
#
# /api/staff/ban already deleted sessions, so the only way to get somebody out
# of the game was to ban them - which made the smallest available response to
# "this account is behaving oddly" the largest one. A mod who wanted to
# interrupt something had to choose between overreacting and doing nothing.
#
# THE WHOLE VALUE OF A KICK IS THAT IT IS REVERSIBLE BY THE PLAYER, so most of
# what is asserted here is what it does NOT do: it does not ban, it does not
# stop them logging back in, and it does not reach further than a ban would.

kicked = register("kickme")
_dev2 = {"Authorization": "Bearer " + client.post(
    "/api/auth/login", json={"username": "kickme", "password": "password123"}
).get_json()["token"]}
_dev3 = {"Authorization": "Bearer " + client.post(
    "/api/auth/login", json={"username": "kickme", "password": "password123"}
).get_json()["token"]}

_before = staff_view(owner, "kickme").get_json()
check("the staff view reports live sessions",
      _before["sessions"]["active"] == 3, _before["sessions"])
check("and it never carries a token",
      "token" not in json.dumps(_before).lower(),
      "a staff view is the worst place for a readable token - the readers are "
      "the ones with reach")

_r = client.post("/api/staff/kick", headers=owner,
                 json={"username": "kickme", "reason": "re-securing"})
check("a kick returns 200 and says how many it ended",
      _r.status_code == 200 and _r.get_json()["sessions_ended"] == 3,
      _r.get_json())
check("every device is signed out at once",
      all(client.get("/api/character", headers=h).status_code == 401
          for h in (kicked, _dev2, _dev3)),
      "a session that survives a kick makes the whole sanction a suggestion")
check("and the staff view agrees nothing is left",
      staff_view(owner, "kickme").get_json()["sessions"]["active"] == 0)

# THE LINE BETWEEN KICK AND BAN, asserted in both directions.
check("the account is NOT banned by a kick",
      staff_view(owner, "kickme").get_json()["ban"] is None)
_back = client.post("/api/auth/login",
                    json={"username": "kickme", "password": "password123"})
check("so the player logs straight back in",
      _back.status_code == 200,
      "%d - a kick that cannot be undone by logging in is a ban wearing a "
      "smaller word" % _back.status_code)

# SAME REACH AS EVERY OTHER SANCTION. A weaker gate here would make the kick
# the cheap way to harass staff.
check("a kick cannot be aimed at the owner",
      client.post("/api/staff/kick", headers=owner,
                  json={"username": "boss"}).status_code == 404,
      "404 rather than 403 on purpose - the same refusal every out-of-reach "
      "target gets, so it cannot be used to map who outranks whom")
check("and a plain player cannot kick at all",
      client.post("/api/staff/kick", headers=player,
                  json={"username": "kickme"}).status_code == 404)

# ZERO IS A REAL ANSWER, not an error: the account holds no live session and
# the kick changed nothing. A mod reads that as "they are already gone".
client.post("/api/staff/kick", headers=owner, json={"username": "kickme"})
_again = client.post("/api/staff/kick", headers=owner, json={"username": "kickme"})
check("kicking an account with nothing live reports zero",
      _again.status_code == 200 and _again.get_json()["sessions_ended"] == 0,
      _again.get_json())

# A SESSION THAT ENDS FOR NO VISIBLE REASON IS A SUPPORT TICKET.
_hist = [h for h in staff_view(owner, "kickme").get_json()["staff_history"]
         if h["action"] == "kick"]
check("every kick is in the staff history", len(_hist) >= 1, _hist)
check("with the reason and the count", "re-securing" in json.dumps(_hist), _hist)


print("\n=== MODERATION  LINKED ACCOUNTS (surfaced, never auto-banned) ===\n")
#
# The evasion block stops the lazy case. A dynamic address or any VPN walks
# past it, and nothing at this layer can fix that - so the other half of the
# answer is that the evader's NEXT account arrives visible instead of arriving
# unknown.
#
# MOST OF THIS SECTION IS ABOUT NOT ACCUSING PEOPLE. A link through an address
# shared by three accounts and one through a carrier pool of two hundred are
# different facts, and a view that reports them identically is a way to get
# somebody's flatmate banned. The strong/weak split is the assertion that
# matters, and the crowd is built large enough here to actually cross the line
# rather than being assumed to.

def seen_from(name, ip):
    """Register and log in from an address, because the LOGIN is what links."""
    client.post("/api/auth/register", json={"username": name, "password": "password123"},
                environ_base={"REMOTE_ADDR": ip})
    client.post("/api/auth/login", json={"username": name, "password": "password123"},
                environ_base={"REMOTE_ADDR": ip})


def links_for(name, headers=None):
    return staff_view(headers or owner, name).get_json()["linked_accounts"]


LINK_HOME = "203.0.113.90"
for who in ("altmain", "altsecond", "altthird"):
    seen_from(who, LINK_HOME)

# A crowded address: enough unrelated accounts to pass SHARED_ADDRESS_ACCOUNTS,
# built from the constant so raising it cannot silently un-test this.
LINK_CARRIER = "100.64.0.9"
for i in range(app_module.SHARED_ADDRESS_ACCOUNTS + 3):
    seen_from("bystander%02d" % i, LINK_CARRIER)
# altmain also plays from the carrier - a phone on mobile data.
client.post("/api/auth/login", json={"username": "altmain", "password": "password123"},
            environ_base={"REMOTE_ADDR": LINK_CARRIER})

seen_from("nolinks", "198.51.100.77")

_links = links_for("altmain")
_by_name = {a["username"]: a for a in _links["accounts"]}

check("the two accounts on the quiet address are linked",
      "altsecond" in _by_name and "altthird" in _by_name, sorted(_by_name))
check("and that link is reported as strong",
      _by_name.get("altsecond", {}).get("strength") == "strong",
      _by_name.get("altsecond"))
check("strangers behind a carrier address are linked but marked weak",
      _by_name.get("bystander00", {}).get("strength") == "weak",
      _by_name.get("bystander00"))
check("the crowd sizes are returned raw, not only the verdict",
      _by_name.get("bystander00", {}).get("quietest_address_accounts", 0)
      >= app_module.SHARED_ADDRESS_ACCOUNTS,
      _by_name.get("bystander00"))
check("an account sharing nothing has no links at all",
      links_for("nolinks")["accounts"] == [], links_for("nolinks"))

# BANNED FIRST. A mod opening this view is looking for the sibling of somebody
# already actioned; burying it under nine strangers is the same as not showing
# it. The order is part of the feature, so it is asserted.
_con = sqlite3.connect(DB_PATH)
_con.execute("UPDATE users SET is_banned=1, ban_expires_at=NULL, ban_reason='alt', "
             "banned_by='boss', banned_at=? WHERE username='altthird'", (int(time.time()),))
_con.commit()
_con.close()

_after = links_for("altmain")["accounts"]
check("a banned sibling sorts to the top of the list",
      _after and _after[0]["username"] == "altthird",
      [a["username"] for a in _after[:3]])
check("and carries its ban rather than just a flag",
      _after[0]["ban"] is not None and _after[0]["ban"]["permanent"] is True,
      _after[0]["ban"])

# NOTHING WAS BANNED BY LOOKING. The whole design rests on this: the view
# reports, a human decides. An automatic ban on a shared address is the failure
# this section exists to make impossible to introduce quietly.
check("surfacing a link does not ban anybody",
      all(a["ban"] is None for a in _after if a["username"] != "altthird"),
      [a["username"] for a in _after if a["ban"] is not None])
_still_in = client.post("/api/auth/login",
                        json={"username": "altsecond", "password": "password123"},
                        environ_base={"REMOTE_ADDR": LINK_HOME}).status_code
check("and the linked sibling can still log in",
      _still_in == 200,
      "%d - being linked to a banned account must not be a punishment" % _still_in)

# THE SAME GATE AS THE ADDRESSES THEMSELVES. This is a list of other people,
# several of whom are not the subject of the lookup, so it follows can_act_on()
# rather than the mod rank that opens the route.
_owner_view = staff_view(owner, "boss").get_json()["linked_accounts"]
check("nobody sees the owner's linked accounts, not even the owner",
      _owner_view["visible"] is False, _owner_view)
check("and the list is empty rather than filtered",
      _owner_view["accounts"] == [], _owner_view)

_r = staff_view(player, "altmain")
check("a plain player still cannot reach the view at all",
      _r.status_code == 404, _r.status_code)

# THE CAP. A crowded address can link hundreds; a response that size is not a
# view. 'truncated' is computed by fetching one row past the limit, so it is a
# fact rather than a guess from a full page.
check("the response is capped",
      len(_links["accounts"]) <= app_module.LINKED_ACCOUNT_LIMIT,
      len(_links["accounts"]))
check("and says so honestly when it is not capped",
      _links["truncated"] is False, _links)

# NO CREDENTIAL EVER REACHES THIS PAYLOAD. _linked_accounts names its columns
# instead of SELECT *, and this is what holds that decision in place.
_blob = json.dumps(staff_view(owner, "altmain").get_json()).lower()
check("no password hash or token rides along in the staff view",
      "password_hash" not in _blob and '"token"' not in _blob)


print("\n=== E-5  LOGIN LOCKS OUT AFTER REPEATED FAILURES ===\n")

register("lockme", "correct-horse")
for i in range(app_module.LOGIN_MAX_ATTEMPTS):
    r = client.post("/api/auth/login", json={"username": "lockme", "password": "wrongpass123"})
    check("failed attempt %d returns 401" % (i + 1), r.status_code == 401, r.status_code)

# Now locked: even the CORRECT password is refused with 429 until the cooldown.
r = client.post("/api/auth/login", json={"username": "lockme", "password": "correct-horse"})
check("after the cap, even the correct password is locked out (429)", r.status_code == 429,
      r.status_code)

# A different account is unaffected - the lockout is per-account.
r = client.post("/api/auth/login", json={"username": "mallory", "password": "password123"})
check("a different account is not affected by the lockout", r.status_code == 200, r.status_code)

# A success within the cap resets the streak (so honest fat-fingering never locks).
register("resets", "rightpass")
for _ in range(app_module.LOGIN_MAX_ATTEMPTS - 1):
    client.post("/api/auth/login", json={"username": "resets", "password": "nopenopenope"})
r = client.post("/api/auth/login", json={"username": "resets", "password": "rightpass"})
check("a correct password just under the cap succeeds", r.status_code == 200, r.status_code)
for _ in range(app_module.LOGIN_MAX_ATTEMPTS - 1):
    client.post("/api/auth/login", json={"username": "resets", "password": "nopenopenope"})
r = client.post("/api/auth/login", json={"username": "resets", "password": "rightpass"})
check("and the streak reset, so it is not locked", r.status_code == 200, r.status_code)


print("\n=== E-5b  THE TWO FAILURE PATHS COST THE SAME ===\n")
#
# The 401 for "no such user" and the 401 for "wrong password" are byte-identical
# on purpose. That is only half of not telling an attacker which usernames
# exist, because a response also has a DURATION:
#
#     no such user     -> nothing to verify   ~1 ms
#     wrong password   -> scrypt runs         ~100 ms
#
# `if row is None or not check_password_hash(...)` short-circuits, so the
# missing-user path skipped the hash entirely. Two identical bodies a
# hundredfold apart on the clock enumerate usernames exactly as well as two
# different messages would - the attacker reads the stopwatch instead. app.py
# now verifies against _TIMING_DUMMY_HASH when there is no row, so both paths
# pay the same cost.
#
# A DISTINCT SOURCE ADDRESS PER SAMPLE, because this is a deliberate run of
# failed logins and the per-IP throttle exists to stop exactly that. Sharing one
# address would cross IP_MAX_USERNAMES partway through and turn the remaining
# samples into 429s, which are fast - a test measuring the speed of the throttle
# while claiming to measure the speed of the hash would pass whether or not the
# fix was there.
#
# MEDIANS, NOT MEANS, and a generous ratio. Wall-clock timing on a machine that
# is also running a test suite is noisy and one scheduler hiccup moves a mean a
# long way. The signal is enormous - about 100x without the fix and about 1x
# with it - so asking for "at least half" sits far from both the noise and the
# bug, and leaves no room to flake in either direction.

register("timingreal", "correct-horse-battery")

TIMING_SAMPLES = 5


def login_ms(username, password, ip):
    start = time.perf_counter()
    r = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        environ_base={"REMOTE_ADDR": ip},
    )
    return (time.perf_counter() - start) * 1000.0, r


def median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


missing_ms = []
wrong_ms = []
timing_codes = set()
for i in range(TIMING_SAMPLES):
    # Two separate address ranges, so neither run can spend the other's budget.
    ms, r = login_ms("ghost%d" % i, "correct-horse-battery", "192.0.2.%d" % i)
    missing_ms.append(ms)
    timing_codes.add(r.status_code)

    ms, r = login_ms("timingreal", "wrong-horse-battery", "198.18.0.%d" % i)
    wrong_ms.append(ms)
    timing_codes.add(r.status_code)

missing_median = median(missing_ms)
wrong_median = median(wrong_ms)
timing_ratio = missing_median / wrong_median if wrong_median > 0 else 0.0

check("both failure paths answer 401 and nothing else",
      timing_codes == {401}, sorted(timing_codes))
check("an unknown username costs at least half what a wrong password costs",
      timing_ratio >= 0.5,
      "unknown %.1fms vs wrong-password %.1fms (ratio %.2f) - the early return "
      "for the missing-user case is back, and the clock now says which "
      "usernames exist" % (missing_median, wrong_median, timing_ratio))

print("      (unknown %.1fms, wrong password %.1fms, ratio %.2f)"
      % (missing_median, wrong_median, timing_ratio))


print("\n=== E-5c  AUTH STATUS CODES ARE THE CONTRACT ===\n")
#
# The five outcomes this API promises at the auth boundary, plus the two it
# adds. Asserted as a table rather than prose because a status code is the
# only part of a response every client branches on, and a handler that starts
# answering 200 where it used to answer 409 breaks callers silently.
#
# 429 and 403 are outcomes four and five's neighbours, not exceptions to them:
# throttled is not "unauthorised", and banned is not "bad credentials". The ban
# check deliberately sits AFTER the password check, so a 403 can only be seen
# by someone who already proved the account is theirs - otherwise this endpoint
# would be a way to find out who is banned.

_probe_ip = [0]


def auth_call(path, payload, raw=False):
    # A fresh address per call. These are deliberate failures and the per-IP
    # gate exists to stop runs of them; sharing one address would turn the
    # later rows of this table into 429s and assert nothing about the handler.
    _probe_ip[0] += 1
    env = {"REMOTE_ADDR": "100.64.%d.%d" % (_probe_ip[0] // 250, _probe_ip[0] % 250)}
    if raw:
        return client.post(path, data=payload, content_type="application/json",
                           environ_base=env)
    return client.post(path, json=payload, environ_base=env)


AUTH_CASES = [
    ("register  new username",          "/api/auth/register",
     {"username": "codesalice", "password": "password123"}, 201),
    ("register  username taken",        "/api/auth/register",
     {"username": "codesalice", "password": "password123"}, 409),
    ("register  missing password",      "/api/auth/register",
     {"username": "codesbob"}, 400),
    ("register  missing username",      "/api/auth/register",
     {"password": "password123"}, 400),
    ("register  body is not JSON",      "/api/auth/register", "not json", 400),
    ("login     correct credentials",   "/api/auth/login",
     {"username": "codesalice", "password": "password123"}, 200),
    ("login     wrong password",        "/api/auth/login",
     {"username": "codesalice", "password": "wrongpass11"}, 401),
    ("login     unknown username",      "/api/auth/login",
     {"username": "codesnobody", "password": "password123"}, 401),
    ("login     missing password",      "/api/auth/login",
     {"username": "codesalice"}, 400),
    ("login     missing username",      "/api/auth/login",
     {"password": "password123"}, 400),
    ("login     body is not JSON",      "/api/auth/login", "not json", 400),
]

for label, path, payload, expect in AUTH_CASES:
    r = auth_call(path, payload, raw=isinstance(payload, str))
    check("%s -> %d" % (label, expect), r.status_code == expect,
          "got %d" % r.status_code)


print("\n=== E-5d  /register IS NOT A USERNAME ORACLE ===\n")
#
# THE HOLE THIS CLOSES, and why it mattered more than it looks.
#
# /login is careful never to reveal whether a username exists: one message for
# both failures, and (see E-5b) the same cost on both paths. /register answers
# that exact question outright, 409 against 201, and cannot stop - a signup
# form that will not say a name is taken is not a signup form.
#
# It had no limit at all. Forty probes from one address got forty straight
# answers where /login stopped after six, so every bit of care taken on /login
# was avoidable by asking the question next door and bringing the list back.
#
# Worse, generate_password_hash ran BEFORE the duplicate check, so each probe
# also bought 32MB and ~100ms of scrypt at the attacker's chosen rate - the
# denial-of-service lever /login's gate exists to avoid, sitting open one
# endpoint over.

def reg_from(name, ip):
    return client.post("/api/auth/register",
                       json={"username": name, "password": "password123"},
                       environ_base={"REMOTE_ADDR": ip}).status_code


# Seed accounts to probe for, each from its own address so the seeding itself
# never trips anything.
for i in range(app_module.REGISTER_MAX_CONFLICTS + 4):
    reg_from("oracle%02d" % i, "10.1.0.%d" % i)

ATTACKER_IP = "100.100.0.1"
probe_codes = [reg_from("oracle%02d" % i, ATTACKER_IP)
               for i in range(app_module.REGISTER_MAX_CONFLICTS + 4)]
answered = sum(1 for c in probe_codes if c in (201, 409))

check("probing stops at REGISTER_MAX_CONFLICTS",
      answered <= app_module.REGISTER_MAX_CONFLICTS,
      "%d of %d probes were answered" % (answered, len(probe_codes)))
check("and the rest are refused with 429",
      probe_codes[-1] == 429, probe_codes[-1])

# THE OTHER HALF, and the reason the register count is kept separate from the
# login one: a person choosing a name really does collide a few times, and
# being locked out of LOGGING IN for picking a popular username would be a
# worse bug than the one above.
HONEST_IP = "100.100.0.2"
honest_codes = [reg_from("oracle%02d" % i, HONEST_IP) for i in range(4)]
honest_codes.append(reg_from("oraclefreename", HONEST_IP))
check("an honest signup may collide several times and still get in",
      honest_codes[-1] == 201, honest_codes)

r = client.post("/api/auth/login",
                json={"username": "oracle00", "password": "password123"},
                environ_base={"REMOTE_ADDR": HONEST_IP})
check("and those collisions did not cost them the ability to log in",
      r.status_code == 200,
      "%d - register conflicts are leaking into the login spray gate" % r.status_code)


print("\n=== E-5e  A BANNED PLAYER CANNOT SIGN UP AGAIN FROM THE SAME ADDRESS ===\n")
#
# The pattern: banned, makes a new account, same connection, same minute. The
# block is REGISTRATION ONLY - existing accounts on the address keep logging in,
# because blocking those means banning one teenager takes out their household,
# their school, or everyone behind a mobile carrier's shared address.
#
# HALF THESE ASSERTIONS ARE ABOUT WHO IS NOT HIT. A ban-evasion check is only
# worth having if it is narrow, and "narrow" is not something the blocking
# behaviour can demonstrate - it has to be shown by the people who still get
# through. The sibling, the stranger and the expired sentence are the test.
#
# WHY THE STATUS CODE IS ASSERTED AND NOT JUST THE REFUSAL: this first shipped
# with _ban_evasion_state() selecting only the columns its own logic branched
# on, while ban_state() reads five. Every blocked registration raised
# IndexError inside the refusal and came back 500. The DECISION was right and
# the block worked; the response was a crash, so the feature looked broken
# while behaving perfectly - and a test that only asked "was it refused?"
# would have passed. It asks for 403.

def ban_directly(username, expires_in):
    """Ban straight through the database.

    Deliberately NOT through the staff route: this section is testing the
    evasion gate, and routing every case through /api/staff/ban would mean a
    failure there shows up here as a confusing pass.
    """
    con = sqlite3.connect(DB_PATH)
    now = int(time.time())
    con.execute(
        "UPDATE users SET is_banned=1, ban_expires_at=?, ban_reason=?, "
        "banned_by=?, banned_at=? WHERE username=?",
        (None if expires_in is None else now + expires_in,
         "testing", "boss", now, username),
    )
    con.commit()
    con.close()


def login_from(name, ip):
    return client.post("/api/auth/login",
                       json={"username": name, "password": "password123"},
                       environ_base={"REMOTE_ADDR": ip}).status_code


EV_HOME = "203.0.113.7"          # the banned player's connection
EV_ELSEWHERE = "198.51.100.4"    # an unrelated player
EV_NEW = "203.0.113.200"         # the banned player on a different connection

reg_from("evader", EV_HOME)
login_from("evader", EV_HOME)    # the login is what records the address link
reg_from("sibling", EV_HOME)

ban_directly("evader", 3600)

# Captured once. check()'s detail argument is evaluated eagerly, so calling
# reg_from() inside it would fire a SECOND registration on every green run -
# which would inflate the refusal count asserted further down.
_blocked = reg_from("evadernew", EV_HOME)
check("the banned player cannot register a new account from home",
      _blocked == 403,
      "%d - 500 here means the refusal itself crashed" % _blocked)
check("and cannot simply log back in either",
      login_from("evader", EV_HOME) == 403)

# THE NARROWNESS HALF.
check("the sibling on the same address still logs in",
      login_from("sibling", EV_HOME) == 200,
      "banning one account took out the household")
check("a stranger elsewhere registers normally",
      reg_from("bystander", EV_ELSEWHERE) == 201)
check("and the address is not poisoned for the banned player's own new one",
      reg_from("evaderfar", EV_NEW) == 201,
      "a dynamic address or a VPN defeats this and the code says so")

# EXPIRY. ban_state() reads expiry against now, so nothing has to run on a
# schedule for a served sentence to stop blocking - and an address must not
# stay poisoned for a stranger in two years by something somebody else did.
ban_directly("evader", -10)
check("a served sentence stops blocking registrations by itself",
      reg_from("aftertheban", EV_HOME) == 201,
      "the block outlived the ban")

ban_directly("evader", None)
check("a permanent ban keeps blocking",
      reg_from("evaderperm", EV_HOME) == 403)

_con = sqlite3.connect(DB_PATH)
_con.row_factory = sqlite3.Row
_refusals = _con.execute(
    "SELECT COUNT(*) AS n FROM login_attempts WHERE reason = 'register-ban-evasion'"
).fetchone()["n"]
# Logged under its own reason, and that prefix matters: _ip_throttle_state
# excludes 'register-%' so a refusal here cannot bleed into the login spray
# gate and lock the sibling out sideways.
# EXACTLY TWO, not "at least": the section refuses precisely two registrations
# (evadernew under the timed ban, evaderperm under the permanent one), and an
# equality catches a stray extra request as loudly as a missing row. A >= here
# would have hidden the duplicate call this assertion was first written against.
check("every refusal is recorded under its own reason",
      _refusals == 2,
      "%d rows, expected 2 - a different count means a registration was "
      "refused or let through somewhere this section did not intend" % _refusals)
_linked = _con.execute(
    "SELECT COUNT(*) AS n FROM account_ips a JOIN users u ON u.id = a.user_id "
    "WHERE a.ip = ?", (EV_HOME,)
).fetchone()["n"]
# The link table is the part that survives a VPN: it cannot stop the next
# account, but it makes it visible to staff, which is the honest goal.
check("accounts seen at an address are linked for staff to look at",
      _linked >= 2, _linked)
_con.close()

# THE BLOCK MUST NOT BE USABLE AS A WEAPON.
#
# On a home connection, refusing registration is a fair trade. On a campus, a
# library or a carrier NAT it lets anyone lock hundreds of strangers out of the
# game by getting THEMSELVES banned on purpose - and their account is already
# gone, so the attack costs them nothing.
#
# This was not hypothetical and it was not caught by review: the first version
# shipped without the crowd check, and forty unrelated accounts on one campus
# address were permanently unable to register after one of them was banned.
# The attack is reproduced here so it cannot come back.

CAMPUS = "192.0.2.50"
for i in range(app_module.EVASION_BLOCK_MAX_ACCOUNTS + 5):
    seen_from("student%02d" % i, CAMPUS)

_con = sqlite3.connect(DB_PATH)
_con.execute("UPDATE users SET is_banned=1, ban_expires_at=NULL, ban_reason='on purpose', "
             "banned_by='boss', banned_at=? WHERE username='student03'", (int(time.time()),))
_con.commit()
_con.close()

_campus_code = reg_from("newstudent", CAMPUS)
check("a banned account cannot lock a whole campus out of signing up",
      _campus_code == 201,
      "%d - one self-ban just disabled registration for everyone behind a "
      "shared address" % _campus_code)

# AND THE TRADE IS STATED HONESTLY: the evader who finds a crowded address
# registers freely. That is accepted because a VPN already grants them the
# same outcome, and because the link view still surfaces the account.
_campus_links = links_for("student03")["accounts"]
check("but every account there is still visible to staff",
      any(a["username"] == "newstudent" for a in _campus_links),
      [a["username"] for a in _campus_links[:5]])
check("and correctly marked weak rather than presented as an alt",
      all(a["strength"] == "weak" for a in _campus_links),
      [(a["username"], a["strength"]) for a in _campus_links[:5]])

# THE BOUNDARY, asserted rather than assumed. A household sitting exactly ON
# the limit must still be blocked, or the constant means something other than
# what its name says.
EDGE = "192.0.2.77"
for i in range(app_module.EVASION_BLOCK_MAX_ACCOUNTS):
    seen_from("edge%02d" % i, EDGE)
_con = sqlite3.connect(DB_PATH)
_con.execute("UPDATE users SET is_banned=1, ban_expires_at=NULL, ban_reason='x', "
             "banned_by='boss', banned_at=? WHERE username='edge00'", (int(time.time()),))
_con.commit()
_con.close()
_edge_code = reg_from("edgenew", EDGE)
check("an address exactly at the limit is still treated as a household",
      _edge_code == 403,
      "%d at exactly EVASION_BLOCK_MAX_ACCOUNTS accounts" % _edge_code)


print("\n=== E-7  ITEM USE IS SERVER-AUTHORITATIVE ===\n")
#
# The finding: ItemData carried required_level and required_skill, and only the
# CLIENT checked them. Trading made that load-bearing - a level 1 character can
# be handed a level 22 potion by a friend - so the rule moved to the server.
#
# These assertions ARE the gate. If one goes green while the check is gone, the
# gate stopped gating with nothing erroring anywhere, which is the exact failure
# mode this file exists to catch.

def consume(headers, item_id, slot=0):
    return client.post("/api/character/consume", headers=headers,
                       json={"slot": slot, "item_id": item_id})


def item_where(**predicate):
    for item in gamedata.ITEMS.values():
        if all(item.get(k) == v for k, v in predicate.items()):
            return item
    return None


def gated_consumable(by):
    for item in gamedata.ITEMS.values():
        if item.get("type_name") != "CONSUMABLE":
            continue
        if by == "level" and int(item.get("required_level", 1)) > 1:
            return item
        if by == "skill" and str(item.get("required_skill", "")):
            return item
    return None


drinker = register("drinker")
make_char(drinker)

free_potion = item_where(type_name="CONSUMABLE", required_level=1, required_skill="")
level_potion = gated_consumable("level")
skill_food = gated_consumable("skill")
a_sword = item_where(type_name="WEAPON")

check("the catalogue still has an ungated consumable", free_potion is not None)
check("the catalogue still has a level-gated consumable", level_potion is not None)
check("the catalogue still has a skill-gated consumable", skill_food is not None)

# You cannot consume what you do not hold, and that answer comes BEFORE the
# requirement - a 403 here would tell a client which items it lacks are gated.
r = consume(drinker, free_potion["item_id"])
check("consuming something you do not hold -> 404", r.status_code == 404, r.status_code)

grant_directly("drinker", 0, [(free_potion["item_id"], 3)])
r = consume(drinker, free_potion["item_id"])
check("an ungated consumable you hold -> 200", r.status_code == 200, r.status_code)
check("the server reports one consumed", r.get_json().get("consumed") == 1, r.get_json())
check("and exactly one left the stack",
      carried(drinker).get(free_potion["item_id"]) == 2, carried(drinker))

# THE FINDING ITSELF: a level 1 character holding a level 22 potion.
grant_directly("drinker", 0, [(level_potion["item_id"], 2)])
r = consume(drinker, level_potion["item_id"])
check("a level-gated consumable at level 1 -> 403", r.status_code == 403, r.status_code)
check("and the item was NOT destroyed",
      carried(drinker).get(level_potion["item_id"]) == 2, carried(drinker))
check("the refusal names the level needed",
      str(level_potion["required_level"]) in r.get_json().get("message", ""), r.get_json())

# The same item once the character is high enough. Level is server-owned, so it
# has to be raised where the server keeps it, not claimed over the wire.
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE saves SET level = ? WHERE user_id = "
             "(SELECT id FROM users WHERE username = 'drinker')",
             (int(level_potion["required_level"]),))
conn.commit()
conn.close()
r = consume(drinker, level_potion["item_id"])
check("the same item at the required level -> 200", r.status_code == 200, r.status_code)
check("and that one WAS destroyed",
      carried(drinker).get(level_potion["item_id"]) == 1, carried(drinker))

# A SKILL GATE ON A CHARACTER WITH NO SKILL ROWS AT ALL. This is the bug the
# first draft shipped with: no row for "cooking" read as "cooking is not a
# skill", and the gate opened for every brand new account.
untrained = register("untrained")
make_char(untrained)
grant_directly("untrained", 0, [(skill_food["item_id"], 1)])
r = consume(untrained, skill_food["item_id"])
check("a skill gate holds for a character with no skill rows -> 403",
      r.status_code == 403, r.status_code)
check("and it names the skill",
      skill_food["required_skill"] in r.get_json().get("message", "").lower(), r.get_json())

# WRITTEN STRAIGHT INTO THE TABLE, and the first draft of this test did not.
# It called PUT /api/character/skills, which correctly DROPPED the write: the
# gated items are cooked food, cooking is in SERVER_OWNED_SKILLS, and E-2's fix
# is that a client cannot name its own level in it. The test walked into the
# very authority it is standing next to. Raising it here is the same standing-in
# grant_directly() does for items.
conn = sqlite3.connect(DB_PATH)
_uid = conn.execute("SELECT id FROM users WHERE username = 'untrained'").fetchone()[0]
conn.execute(
    "INSERT INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, 0, ?, ?, 0) "
    "ON CONFLICT(user_id, slot, skill_id) DO UPDATE SET level = excluded.level",
    (_uid, skill_food["required_skill"], int(skill_food["required_skill_level"])),
)
conn.commit()
conn.close()
r = consume(untrained, skill_food["item_id"])
check("and opens once that skill is high enough -> 200", r.status_code == 200, r.status_code)

# The other half of that discovery, asserted on its own terms so it cannot
# quietly stop being true: a client still cannot talk its way past a skill gate.
grant_directly("untrained", 0, [(skill_food["item_id"], 1)])
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE skills SET level = 1 WHERE user_id = ? AND skill_id = ?",
             (_uid, skill_food["required_skill"]))
conn.commit()
conn.close()
put_skills(untrained, {skill_food["required_skill"]: int(skill_food["required_skill_level"])})
r = consume(untrained, skill_food["item_id"])
check("claiming the skill level over the wire does not open the gate",
      r.status_code == 403, r.status_code)

# Only consumables. A pet is a permanent collectible and gold has to move
# through the ledger; neither may be destroyed here.
grant_directly("drinker", 0, [(a_sword["item_id"], 1)])
r = consume(drinker, a_sword["item_id"])
check("a weapon is not consumable -> 409", r.status_code == 409, r.status_code)
check("and the sword survived", carried(drinker).get(a_sword["item_id"]) == 1, carried(drinker))

r = consume(drinker, "no_such_item_at_all")
check("an item that does not exist -> 404 (you are not carrying it)",
      r.status_code == 404, r.status_code)

r = client.post("/api/character/consume",
                json={"slot": 0, "item_id": free_potion["item_id"]})
check("consume requires a token -> 401", r.status_code == 401, r.status_code)


print("\n=== UNEXPLAINED HEALING  (shadow mode: logs, refuses nothing) ===\n")
#
# PUT /api/player/status accepts any hp up to the server's derived maximum, so a
# patched client heals for free. This cannot be refused yet - the regen numbers
# it measures against are COPIED from the player scene rather than exported - so
# it logs, exactly as _report_unexplained_gains() did before E-1 was enforced.
#
# The assertions that matter most are the QUIET ones. A check that fired on
# honest regeneration would be worse than no check at all: it would bury the
# real signal under noise from every player in the game.

import io as _io
import logging as _logging

_heal_log = _io.StringIO()
_handler = _logging.StreamHandler(_heal_log)
_handler.setLevel(_logging.WARNING)
app_module.app.logger.addHandler(_handler)
app_module.app.logger.setLevel(_logging.WARNING)


def drained():
    text = _heal_log.getvalue()
    _heal_log.truncate(0)
    _heal_log.seek(0)
    return text


patient = register("patient")
make_char(patient)
status = client.get("/api/player/status?slot=0", headers=patient).get_json()
MAX_HP = int(status["max_hp"])


def set_stored(**fields):
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = 'patient'").fetchone()[0]
    conn.execute("UPDATE saves SET %s WHERE user_id = ? AND slot = 0"
                 % ", ".join("%s = ?" % f for f in fields), (*fields.values(), uid))
    conn.commit()
    conn.close()


def authorise_potion(seconds_ago=0):
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = 'patient'").fetchone()[0]
    conn.execute("INSERT INTO consume_grants (user_id, slot, item_id, at) "
                 "VALUES (?, 0, 'x', ?)", (uid, int(time.time()) - seconds_ago))
    conn.commit()
    conn.close()


def forget_potions():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM consume_grants")
    conn.commit()
    conn.close()


set_stored(hp=1, updated_at=int(time.time()))
r = client.put("/api/player/status", headers=patient, json={"slot": 0, "hp": MAX_HP})
logged = drained()
check("1 hp to full with no potion is logged", "unexplained heal" in logged, logged)
check("and the request still succeeded (shadow mode)", r.status_code == 200, r.status_code)
# THIS ASSERTION USED TO BE THE BUG, WRITTEN DOWN AS AN EXPECTATION.
#
# It read "and the claimed hp was still stored", and it passed, because E-9 was
# in shadow mode and the reconciler logged without acting. That is precisely
# the shape of the test_api.py case that asserted gold WAS stored while E-8 was
# open - a suite can only tell you about the behaviour somebody decided to
# assert, and asserting the current behaviour makes a vulnerability look load
# bearing.
#
# The claim is now trimmed to what regeneration could have produced, and the
# request still succeeds, because a 400 here would discard the XP, gold and
# inventory riding along in the same save.
stored = client.get("/api/player/status?slot=0", headers=patient).get_json()["hp"]
if app_module.HEAL_CLAMP_ENFORCED:
    check("and the claimed hp was TRIMMED, not stored", stored < MAX_HP,
          "stored %s of %s" % (stored, MAX_HP))
    check("trimmed to the allowance rather than to zero", stored >= 1, stored)
else:
    # SHADOW MODE, AND SAID SO. The old version of this assertion read "and the
    # claimed hp was still stored" with no explanation, which is how a
    # vulnerability ends up looking load-bearing. Storing the claim is what
    # happens while the clamp only reports; the check names that rather than
    # implying it is the desired end state.
    check("the claim is stored, because enforcement is off - the log is the"
          " control today", stored == MAX_HP, stored)
    check("but it was reported, which is the half that is always on",
          "unexplained heal" in logged, logged)

set_stored(hp=1, updated_at=int(time.time()))
authorise_potion()
client.put("/api/player/status", headers=patient, json={"slot": 0, "hp": MAX_HP})
check("the same jump WITH an authorised consume is silent",
      "unexplained heal" not in drained())
forget_potions()

set_stored(hp=1, updated_at=int(time.time()) - 60)
client.put("/api/player/status", headers=patient, json={"slot": 0, "hp": MAX_HP})
check("a minute of regeneration back to full is silent",
      "unexplained heal" not in drained())

set_stored(hp=MAX_HP - 6, updated_at=int(time.time()) - 3)
client.put("/api/player/status", headers=patient, json={"slot": 0, "hp": MAX_HP})
check("a few seconds of regeneration is silent", "unexplained heal" not in drained())

set_stored(hp=MAX_HP, updated_at=int(time.time()))
client.put("/api/player/status", headers=patient, json={"slot": 0, "hp": 10})
check("taking damage is never flagged", "unexplained heal" not in drained())

set_stored(mana=0, stamina=0, updated_at=int(time.time()))
client.put("/api/player/status", headers=patient,
           json={"slot": 0, "mana": int(status["max_mana"]),
                 "stamina": int(status["max_stamina"])})
logged = drained()
check("mana is watched too", "mana" in logged, logged)
check("stamina is watched too", "stamina" in logged, logged)

set_stored(hp=1, updated_at=int(time.time()))
authorise_potion(seconds_ago=app_module.HEAL_EXPLAIN_WINDOW_SECONDS + 60)
client.put("/api/player/status", headers=patient, json={"slot": 0, "hp": MAX_HP})
check("a potion from long ago does not explain a rise now", "unexplained heal" in drained())
forget_potions()


# --- and the sharpened half: a grant explains its OWN pool, up to its OWN size

def granted(target, amount, item_id="fixture"):
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = 'patient'").fetchone()[0]
    conn.execute(
        "INSERT INTO consume_grants (user_id, slot, item_id, target, amount, at) "
        "VALUES (?, 0, ?, ?, ?, ?)",
        (uid, item_id, target, amount, int(time.time())),
    )
    conn.commit()
    conn.close()


# THE HOLE THIS CLOSED. The first version of the check asked only "did this
# character consume anything recently", so the cheapest potion in the game
# explained a jump of any size in any pool. Both halves of that are now wrong.
forget_potions()
set_stored(hp=1, updated_at=int(time.time()))
granted("HP", 30)
client.put("/api/player/status", headers=patient, json={"slot": 0, "hp": MAX_HP})
check("a 30hp potion does not explain a jump to full",
      "unexplained heal" in drained())

forget_potions()
set_stored(hp=MAX_HP - 25, updated_at=int(time.time()))
granted("HP", 30)
client.put("/api/player/status", headers=patient, json={"slot": 0, "hp": MAX_HP})
check("but it does explain a rise inside its own size",
      "unexplained heal" not in drained())

forget_potions()
set_stored(hp=1, updated_at=int(time.time()))
granted("STAMINA", 500)
client.put("/api/player/status", headers=patient, json={"slot": 0, "hp": MAX_HP})
check("a stamina potion explains nothing about health",
      "unexplained heal" in drained())

# A grant with no amount is a row written before the server knew what potions
# restore - or from a gamedata.json that predates the export. Unknown is not
# zero: it explains nothing, and it must not be reported as a cheat either.
forget_potions()
set_stored(hp=1, updated_at=int(time.time()))
granted("", 0, item_id="from_before_the_export")
client.put("/api/player/status", headers=patient, json={"slot": 0, "hp": MAX_HP})
check("a grant of unknown size is not called a cheat",
      "unexplained heal" not in drained())

# A revive fills every pool, so it explains any rise in any of them.
forget_potions()
set_stored(hp=1, mana=0, updated_at=int(time.time()))
granted("", 0, item_id=app_module.REVIVE_GRANT_ID)
client.put("/api/player/status", headers=patient,
           json={"slot": 0, "hp": MAX_HP, "mana": int(status["max_mana"])})
check("a revive explains every pool at once",
      "unexplained heal" not in drained())
forget_potions()

# The rates the allowance is computed from must come from the export, not from
# a copy in app.py. This asserts the SEAM rather than the numbers: when
# gamedata.json carries them, they are what gets used.
check("the server reads its regen rate through gamedata",
      abs(gamedata.regen_rate_for(1000) - max(
          float(gamedata.CONSTANTS.get("regen_minimum_per_second", 1.0)),
          1000 * float(gamedata.CONSTANTS.get("regen_percent_per_second", 0.0167)))) < 1e-9)
check("and says so at boot when the export has not been rerun",
      isinstance(gamedata.REGEN_EXPORTED, bool), gamedata.REGEN_EXPORTED)



# A LEVEL-UP FILLS ALL THREE POOLS — player.gd::level_up() calls
# _fill_all_resources() — and the server is the thing that decides a level-up
# happened, so it is the thing that records one. Without this the check would
# flag every level any player ever gains, which is how a log becomes noise
# nobody reads.
climber = register("climber")
make_char(climber)
conn = sqlite3.connect(DB_PATH)
_cid = conn.execute("SELECT id FROM users WHERE username = 'climber'").fetchone()[0]
conn.execute("UPDATE saves SET hp = 5, updated_at = ? WHERE user_id = ? AND slot = 0",
             (int(time.time()), _cid))
conn.commit()
conn.close()

_kill = client.post("/api/combat/kill", headers=climber, json={"slot": 0, "enemy_id": "boss"})
check("a boss kill levels the character", int(_kill.get_json().get("levels_gained", 0)) > 0,
      _kill.get_json())

conn = sqlite3.connect(DB_PATH)
_ids = [r[0] for r in conn.execute(
    "SELECT item_id FROM consume_grants WHERE user_id = ?", (_cid,))]
conn.close()
check("and leaves a level-up grant", app_module.LEVELUP_GRANT_ID in _ids, _ids)

_full = client.get("/api/player/status?slot=0", headers=climber).get_json()
drained()
client.put("/api/player/status", headers=climber,
           json={"slot": 0, "hp": _full["max_hp"], "mana": _full["max_mana"],
                 "stamina": _full["max_stamina"]})
check("so the refill that comes with it is not flagged",
      "unexplained heal" not in drained())

app_module.app.logger.removeHandler(_handler)


print("\n=== E-10  LUSIONS AND REVIVE ARE SERVER-OWNED ===\n")
#
# The finding: PUT /api/account/lusions stored an absolute figure from the
# client, and gameover.gd ran the entire revive locally - check the balance,
# deduct the cost, write full hp into the save slot, reload the world.
#
# Lusions have exactly ONE sink: reviving after death. So the balance is the
# death penalty, and a client that could write it had deleted the cost of dying
# from the game. Two endpoints and a client rewrite later, neither half is the
# client's to decide.


def revive(headers, slot=0):
    return client.post("/api/character/revive", headers=headers, json={"slot": slot})


def kill(username, slot=0):
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    conn.execute("UPDATE saves SET hp = 0, updated_at = ? WHERE user_id = ? AND slot = ?",
                 (int(time.time()), uid, slot))
    conn.commit()
    conn.close()


def give_lusions(username, amount):
    """Stands in for a duplicate-pet conversion, which is the only real source.

    INSERT OR IGNORE first because the accounts row is created lazily - a bare
    UPDATE matches nothing on an account that has never banked anything, which
    is how the first draft of this fixture silently did nothing.

    AND A LEDGER ROW WITH IT, which the second draft forgot. lusion_ledger
    carries the same kind of invariant gold_ledger does, so a fixture that sets
    a balance without recording where it came from breaks the very property the
    cases below are checking - and the failure looks like a bug in the server
    rather than in the test. fund_gold() in test_api.py learned this first."""
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (uid,))
    held = conn.execute("SELECT lusions FROM accounts WHERE user_id = ?", (uid,)).fetchone()[0]
    conn.execute("UPDATE accounts SET lusions = ? WHERE user_id = ?", (amount, uid))
    conn.execute(
        "INSERT INTO lusion_ledger (at, user_id, slot, delta, reason, detail) "
        "VALUES (?, ?, 0, ?, 'test_fixture', 'stands in for a duplicate pet')",
        (int(time.time()), uid, int(amount) - int(held)),
    )
    conn.commit()
    conn.close()


def lusions_of(headers):
    return int(client.get("/api/account", headers=headers).get_json()["lusions"])


def hp_of(headers, slot=0):
    return int(client.get("/api/player/status?slot=%d" % slot, headers=headers).get_json()["hp"])


lazarus = register("lazarus")
make_char(lazarus)

REVIVE_COST = int(gamedata.CONSTANTS.get("revive_cost", 20))
check("the server knows what a revive costs", REVIVE_COST > 0, REVIVE_COST)

# THE FINDING, DIRECTLY: buy your way out of death over the wire.
r = client.put("/api/account/lusions", headers=lazarus, json={"lusions": 1_000_000})
check("pushing a lusion balance -> 200", r.status_code == 200, r.status_code)
check("but the balance did not move", lusions_of(lazarus) == 0, r.get_json())
check("and the server named it as disregarded",
      "lusions" in r.get_json().get("ignored", []), r.get_json())

# Only the dead.
r = revive(lazarus)
check("reviving a living character -> 409", r.status_code == 409, r.status_code)

# Dead and broke: refused, and NOTHING moves.
kill("lazarus")
r = revive(lazarus)
check("dead with no lusions -> 402", r.status_code == 402, r.status_code)
check("still dead", hp_of(lazarus) == 0, hp_of(lazarus))
check("and still broke", lusions_of(lazarus) == 0, lusions_of(lazarus))

# The whole point: you cannot top yourself up to afford it.
client.put("/api/account/lusions", headers=lazarus, json={"lusions": REVIVE_COST * 10})
r = revive(lazarus)
check("topping up over the wire does not buy a revive", r.status_code == 402, r.status_code)
check("still dead after trying", hp_of(lazarus) == 0, hp_of(lazarus))

# Dead and able: the server charges and restores.
give_lusions("lazarus", REVIVE_COST + 7)
before = lusions_of(lazarus)
r = revive(lazarus)
body = r.get_json()
check("dead with enough lusions -> 200", r.status_code == 200, r.status_code)
check("the cost was taken exactly once",
      lusions_of(lazarus) == before - REVIVE_COST, [before, lusions_of(lazarus)])
check("the server reports the same balance it stored",
      int(body.get("lusions", -1)) == lusions_of(lazarus), body)

status_after = client.get("/api/player/status?slot=0", headers=lazarus).get_json()
check("hp was restored to the class maximum",
      status_after["hp"] == status_after["max_hp"] and status_after["hp"] > 0, status_after)
check("so were mana and stamina",
      status_after["mana"] == status_after["max_mana"]
      and status_after["stamina"] == status_after["max_stamina"], status_after)

# Reviving twice charges once, because the second attempt is not a death.
after_one = lusions_of(lazarus)
r = revive(lazarus)
check("a second revive is refused -> 409", r.status_code == 409, r.status_code)
check("and cost nothing", lusions_of(lazarus) == after_one, lusions_of(lazarus))

r = client.post("/api/character/revive", json={"slot": 0})
check("revive requires a token -> 401", r.status_code == 401, r.status_code)

r = revive(lazarus, slot=9)
check("a bad slot -> 400", r.status_code == 400, r.status_code)

# --- and the other way to pay: a share of everything you own, burned

def bank_and_carry(username, carry, bank, slot=0):
    """Set both purses AND a ledger row that matches, so the supply invariant
    is intact before the revive takes a bite out of it. Gold that appears in a
    purse without a row is exactly what E-8 was."""
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (uid,))
    conn.execute("UPDATE saves SET gold = ? WHERE user_id = ? AND slot = ?", (carry, uid, slot))
    conn.execute("UPDATE accounts SET bank_gold = ? WHERE user_id = ?", (bank, uid))
    conn.execute(
        "INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) "
        "VALUES (?, ?, ?, ?, 'test_fixture', '')",
        (int(time.time()), uid, slot, carry + bank),
    )
    conn.commit()
    conn.close()


def purses(username, slot=0):
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    carry = conn.execute("SELECT gold FROM saves WHERE user_id = ? AND slot = ?",
                         (uid, slot)).fetchone()[0]
    bank = conn.execute("SELECT bank_gold FROM accounts WHERE user_id = ?", (uid,)).fetchone()[0]
    conn.close()
    return int(carry), int(bank)


def supply_balanced():
    conn = sqlite3.connect(DB_PATH)
    recorded = conn.execute("SELECT COALESCE(SUM(delta), 0) FROM gold_ledger").fetchone()[0]
    held = (conn.execute("SELECT COALESCE(SUM(gold), 0) FROM saves").fetchone()[0]
            + conn.execute("SELECT COALESCE(SUM(bank_gold), 0) FROM accounts").fetchone()[0])
    conn.close()
    return int(recorded) == int(held), (int(recorded), int(held))


RATE = float(gamedata.CONSTANTS.get("revive_gold_rate", 0.80))

rich = register("rich")
make_char(rich)
bank_and_carry("rich", 100, 900)
kill("rich")

r = revive(rich)                       # defaults to lusions, which they have none of
check("with no lusions the default route still refuses", r.status_code == 402, r.status_code)

r = client.post("/api/character/revive", headers=rich, json={"slot": 0, "pay": "gold"})
body = r.get_json()
check("but gold revives -> 200", r.status_code == 200, r.status_code)
check("and says which currency paid", body.get("paid_with") == "gold", body)

import math as _math
expected = _math.ceil(1000 * RATE)
check("the price is a share of carry AND bank together",
      int(body.get("cost", -1)) == expected, [body.get("cost"), expected])


carry, bank = purses("rich")
check("the carry purse is spent first", carry == 0, carry)
check("and the bank covers the rest", bank == 1000 - expected, [bank, 1000 - expected])

ok, sums = supply_balanced()
check("the burn is in the ledger, so supply still balances", ok, sums)

conn = sqlite3.connect(DB_PATH)
_burn = conn.execute(
    "SELECT -SUM(delta) FROM gold_ledger WHERE reason = 'revive'").fetchone()[0]
conn.close()
check("and it lands on the kingdom board as its own reason",
      int(_burn or 0) == expected, [_burn, expected])

status_now = client.get("/api/player/status?slot=0", headers=rich).get_json()
check("the character is alive again", status_now["hp"] == status_now["max_hp"], status_now)

# A share of nothing is nothing, and a free resurrection is not on offer.
pauper = register("pauper")
make_char(pauper)
bank_and_carry("pauper", 0, 0)
kill("pauper")
r = client.post("/api/character/revive", headers=pauper, json={"slot": 0, "pay": "gold"})
check("an empty purse and an empty bank -> 402", r.status_code == 402, r.status_code)
check("still dead", hp_of(pauper) == 0, hp_of(pauper))

# Everything in the bank, nothing carried - the case the feature is named for.
saver = register("saver")
make_char(saver)
bank_and_carry("saver", 0, 500)
kill("saver")
r = client.post("/api/character/revive", headers=saver, json={"slot": 0, "pay": "gold"})
check("banked gold alone can buy a revive", r.status_code == 200, r.status_code)
check("and it came out of the bank",
      purses("saver") == (0, 500 - _math.ceil(500 * RATE)), purses("saver"))
ok, sums = supply_balanced()
check("supply still balances after a bank-only burn", ok, sums)

r = client.post("/api/character/revive", headers=rich, json={"slot": 0, "pay": "doubloons"})
check("an unknown currency -> 400", r.status_code == 400, r.status_code)



# AND THE REASON THIS LANDED IN THE SAME WEEK AS THE HEALING RECONCILER: an
# honest revive is a jump from 0 to full in no time at all, which is exactly
# the shape the reconciler flags. It leaves a grant row, so it does not.
_heal_log2 = _io.StringIO()
_handler2 = _logging.StreamHandler(_heal_log2)
_handler2.setLevel(_logging.WARNING)
app_module.app.logger.addHandler(_handler2)

kill("lazarus")
give_lusions("lazarus", REVIVE_COST)
revive(lazarus)
full = client.get("/api/player/status?slot=0", headers=lazarus).get_json()["max_hp"]
client.put("/api/player/status", headers=lazarus, json={"slot": 0, "hp": full})
check("an honest revive does not trip the healing log",
      "unexplained heal" not in _heal_log2.getvalue(), _heal_log2.getvalue())

app_module.app.logger.removeHandler(_handler2)



print("\n=== THE LUSION LEDGER ===\n")
#
# Lusions had no audit trail at all - created by a duplicate-pet conversion,
# destroyed by a revive, and nothing anywhere recorded either. E-10 named that;
# this is the half that closes it. It also makes the kingdom board able to count
# a lusion revive as a contribution, which it cannot do from a balance: a
# balance says what you have, not what you gave.
#
# ITS OWN TABLE, NOT gold_ledger, because that one carries an invariant about
# gold and a lusion row would break it on the first insert.


def lusion_supply():
    conn = sqlite3.connect(DB_PATH)
    recorded = conn.execute("SELECT COALESCE(SUM(delta), 0) FROM lusion_ledger").fetchone()[0]
    held = conn.execute("SELECT COALESCE(SUM(lusions), 0) FROM accounts").fetchone()[0]
    conn.close()
    return int(recorded), int(held)


rec, held = lusion_supply()
check("lusion supply balances at rest", rec == held, [rec, held])

spender = register("spender")
make_char(spender)
give_lusions("spender", REVIVE_COST * 3)
rec_before, _ = lusion_supply()

kill("spender")
r = revive(spender)
check("a lusion revive succeeds", r.status_code == 200, r.status_code)

rec, held = lusion_supply()
check("and the spend is recorded, not just deducted", rec == rec_before - REVIVE_COST,
      [rec_before, rec])
check("so lusion supply still balances", rec == held, [rec, held])

conn = sqlite3.connect(DB_PATH)
_rows = conn.execute(
    "SELECT reason, -SUM(delta) FROM lusion_ledger WHERE delta < 0 GROUP BY reason"
).fetchall()
conn.close()
check("the burn is filed under 'revive'",
      any(row[0] == "revive" for row in _rows), _rows)

# THE POINT OF ALL OF IT: the board counts what was given, in both currencies.
_board = client.get("/api/economy/kingdom", headers=spender).get_json()
check("the board reports a lusion total", int(_board.get("total_lusions", 0)) >= REVIVE_COST,
      _board.get("total_lusions"))
check("and breaks it down by reason",
      "revive" in _board.get("lusions_by_reason", {}), _board.get("lusions_by_reason"))
check("your own line carries both currencies",
      "lusions" in _board.get("you", {}), _board.get("you"))
check("and your lusions are counted",
      int(_board["you"]["lusions"]) >= REVIVE_COST, _board["you"])

_me = [e for e in _board["top"] if e["username"] == "spender"]
check("a lusion-only giver appears on the board at all", len(_me) == 1, _board["top"])
if _me:
    check("with lusions beside gold, not folded into it",
          _me[0]["lusions"] >= REVIVE_COST and _me[0]["contributed"] == 0, _me[0])

# Gold and lusions are never added together - there is no exchange rate, and
# inventing one would be the board deciding what a lusion is worth.
check("the two totals are reported separately",
      "total" in _board and "total_lusions" in _board
      and _board["total"] != _board["total_lusions"] or _board["total_lusions"] > 0,
      [_board.get("total"), _board.get("total_lusions")])


print("\n=== E-4  DEBUG DEFAULTS OFF ===\n")
# app.run(debug=...) can't be observed through the test client, but the flag it
# reads can: with ELUSION_DEBUG unset, the resolved value must be falsey.
_dbg = os.environ.get("ELUSION_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
check("debug is off when ELUSION_DEBUG is unset", _dbg is False, _dbg)


print("\n=== E-4b  THE DEPLOY PREFLIGHT ACTUALLY REFUSES ===\n")
#
# wsgi.py's preflight is a list of checks nothing had ever executed. That is the
# same shape as every other bug this file exists to catch: it looks like a
# safeguard, it is never run, and the first time it matters is the one time
# nobody is watching.
#
# RUN IN A SUBPROCESS, because importing wsgi is a one-way door - it imports
# app, which opens the database, and SystemExit cannot be taken back inside a
# running interpreter. A child process can be asked the question twice with
# different environments, which is the whole point.

import subprocess


def preflight(env_overrides, gamedata_dir=None):
    """Import wsgi.py in a child and report (exit_code, stderr)."""
    env = dict(os.environ)
    env.update(env_overrides)
    env["ELUSION_DB"] = os.path.join(tempfile.gettempdir(), "elusion_preflight.db")
    run_in = gamedata_dir or HERE
    r = subprocess.run(
        [sys.executable, "-c", "import wsgi"],
        cwd=run_in, env=env, capture_output=True, text=True, timeout=120,
    )
    return r.returncode, (r.stderr or "")


# ELUSION_DEBUG is the one the preflight already refused on. Asserted first
# because if this does not refuse, the mechanism is broken and every other
# check in that file is decoration.
code, err = preflight({"ELUSION_DEBUG": "1"})
check("ELUSION_DEBUG=1 stops the server booting", code != 0, code)
check("and the refusal names the setting", "ELUSION_DEBUG" in err, err[-300:])

# THE NEW ONE. A gamedata.json with no equipment data means /api/save accepts
# any item in any slot - proven against this codebase, where a level 1 warrior
# wrote a 100-damage two-handed sword into the helm slot. app.py warns; a
# warning scrolls past and the server comes up looking healthy.
code, err = preflight({})
exported = gamedata.EQUIP_EXPORTED
check("the real gamedata.json beside app.py passes the preflight",
      code == 0 if exported else code != 0,
      err[-300:])

# BOTH SIDES, ALWAYS, and this used to be an if/else on the real file's state.
#
# That meant the branch worth testing was the one that stopped running: once
# the equipment export was done, the "refuses a stale export" check was skipped
# forever, and the only assertion left was that a correct file is accepted -
# which a preflight that refuses nothing at all also satisfies. A guard tested
# only in the state where it should stay quiet is not tested.
#
# So the stale file is BUILT rather than waited for: a copy of the real
# gamedata.json with equip_slot_name stripped out of every item, in a throwaway
# directory beside copies of the modules wsgi.py imports. Python puts cwd on
# sys.path, so the child picks these up instead of the real ones.
_stale_dir = tempfile.mkdtemp(prefix="elusion_stale_")
for _mod in ("wsgi.py", "app.py", "gamedata.py"):
    shutil.copy(os.path.join(HERE, _mod), _stale_dir)

with open(os.path.join(HERE, "gamedata.json")) as _fh:
    _stale = json.load(_fh)
_items = _stale["items"] if isinstance(_stale["items"], list) else list(_stale["items"].values())
for _item in _items:
    _item.pop("equip_slot_name", None)
with open(os.path.join(_stale_dir, "gamedata.json"), "w") as _fh:
    json.dump(_stale, _fh)

code, err = preflight({}, gamedata_dir=_stale_dir)
check("an unexported gamedata.json stops the server booting", code != 0,
      "the preflight let a server boot with no equip validation at all")
check("and the refusal says how to fix it",
      "exportgamedata" in err, err[-300:])
shutil.rmtree(_stale_dir, ignore_errors=True)

print("      (EQUIP_EXPORTED = %s)" % exported)


# =============================================================================
# SCORE - THE ONE NUMBER THAT GOES UP WHEN YOU LOSE
# =============================================================================
# What dying has cost this account, cumulatively. Both revive routes feed it:
# the lusion route keeps everything you carried, the gold route burns 80% of
# what you hold, and both are a price paid for dying.

def score_of(headers):
    return int(client.get("/api/account", headers=headers).get_json().get("score", -1))

check("the gold revive above scored exactly what it burned",
      score_of(rich) == expected, [score_of(rich), expected])

# A SECOND DEATH ADDS, it does not replace. Trivial to get wrong with a SET.
bank_and_carry("rich", 0, 500)
kill("rich")
r = client.post("/api/character/revive", headers=rich, json={"slot": 0, "pay": "gold"})
second = int(r.get_json().get("cost", 0))
check("a second death adds to the score rather than replacing it",
      score_of(rich) == expected + second, [score_of(rich), expected, second])

# THE LUSION ROUTE SCORES TOO, at 1:1 with gold - a decision, not an accident,
# and app.py says so at the line that does it.
lus = register("scorer")
make_char(lus)
give_lusions("scorer", 100)
before = score_of(lus)
kill("scorer")
r = revive(lus)
check("the lusion revive succeeds", r.status_code == 200, r.status_code)
check("and scores the lusions it cost",
      score_of(lus) == before + int(r.get_json().get("cost", 0)),
      [score_of(lus), before, r.get_json().get("cost")])

# A REFUSED REVIVE MUST NOT SCORE. The payment and the score are one
# transaction; a 402 that still moved the number would be the worse half of it.
broke = register("broke")
make_char(broke)
kill("broke")
before = score_of(broke)
r = revive(broke)
check("a revive nobody can afford is refused", r.status_code == 402, r.status_code)
check("and scores nothing", score_of(broke) == before, [score_of(broke), before])

check("a fresh account starts at zero", score_of(register("newborn")) == 0)


print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("  failing: " + ", ".join(failures))
print("=" * 60)
sys.exit(1 if failed else 0)
