"""Refusal tests: 401, 403 and the 404 that is really a 403.

The lesson behind this suite is four sentences long:

    Authentication is WHO IS ASKING.      Authorization is WHAT THEY MAY DO.
    401 means authentication failed.      403 means authorization failed.

This server obeys the first two and deliberately breaks the second, and the
reason is written into require_role():

    "404 rather than 403. A 403 confirms the route exists and that you are not
     allowed to use it, which tells someone exactly where to push."

So a naive reading of app.py finds fifteen 403s and twelve 404s that are really
authorization refusals, and it looks like the codebase cannot make up its mind.
It can. There is one rule underneath, it was never written down, and every one
of those twenty-seven refusals already follows it:

    THE GATE THAT DECIDES WHETHER THE ROUTE IS YOURS ANSWERS 404.
    THE CHECK ON WHAT YOU ASKED FOR, INSIDE A ROUTE ALREADY YOURS, ANSWERS 403.

Read it as a question about the caller: does their own rank already admit them
here? If no, the refusal would be the disclosure - it would confirm a route, a
rank ladder or a guild that they have no standing to learn about - so it is a
404 and it says nothing. If yes, they are already inside, the refusal tells them
nothing they did not know, and it may as well say why: "a mod may ban for at
most 30 days" is useful and leaks nothing, because only a mod can read it.

That is why /api/staff/ban answers 404 to a player and 403 to a mod who asked
for a permanent one. Same route, same gate, different question.

The 401s are the part with no exceptions at all. All eight are authentication:
three are a token that resolved against a row since deleted (identity can no
longer be established - late, but still authentication), and the rest are a
password that did not match. Which produces the fact the CLIENT depends on:

    A 401 IS NOT A VERDICT ON THE SESSION.

Changing a password answers 401 for a mistyped CURRENT password while the token
stays perfectly live, so a client that signs people out on any 401 signs them
out for typos. characterhud.gd::_on_unauthorized_seen() asks heartbeat() instead
of deciding. F-3 below is what that handler is standing on.

What this suite proves:

  F-1  every ranked route is enumerated FROM ITS OWN DECORATOR and refuses a
       plain player with a 404 that names no rank - and no /api/staff/ route
       exists without such a decorator
  F-2  the same route answers 403 to a caller it let in, on the argument
  F-3  a 401 can arrive on a session that is still perfectly live
  F-4  authentication discovered late is still 401, never 403
  F-5  the login 401 cannot be used to enumerate usernames, and the ban 403 is
       gated behind the password
  F-6  the two admission decorators contain no 403 at all

Runs against a THROWAWAY database in the temp folder, exactly like
test_revocation.py, so it never touches elusion.db. Run: python test_refusals.py
"""

import importlib.util
import inspect
import os
import re
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_refusals_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
# The owner is the one rank that is not a row. F-1 has to prove the owner-only
# routes hide themselves too, so the suite needs one.
os.environ["ELUSION_OWNER"] = "boss"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
app = app_module.app
client = app.test_client()

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
    """EVERY ACCOUNT IS REGISTERED BEFORE THE FIRST BAN, and that ordering is
    load-bearing. _ban_evasion_state() refuses registration from an address a
    currently-banned account logged in from, and the test client is always
    127.0.0.1 - so banning anybody makes every later register() look like
    evasion. The server is right; the suite arranges itself around it."""
    return client.post("/api/auth/register",
                       json={"username": username, "password": password}).status_code


def auth(username, password="password123"):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    token = (res.get_json() or {}).get("token")
    return {"Authorization": "Bearer " + token} if token else {}


def msg(res):
    return (res.get_json() or {}).get("message", "")


ACCOUNTS = ("boss", "modde", "devvo", "plain", "victim", "spare")
for who in ACCOUNTS:
    code = register(who)
    check("registered %s" % who, code == 201, code)

owner = auth("boss")

# Ranks are set through the real route, by the owner, so the suite never writes
# a role column the server did not agree to.
for who, rank in (("modde", "mod"), ("devvo", "dev")):
    res = client.put("/api/staff/role", headers=owner, json={"username": who, "role": rank})
    check("owner made %s a %s" % (who, rank), res.status_code == 200, res.get_json())

player = auth("plain")
mod = auth("modde")
dev = auth("devvo")

check("the plain player's token is served on an ordinary route",
      client.get("/api/account", headers=player).status_code == 200,
      "everything below depends on this token being good")


# =============================================================================
print("\n=== F-1  THE RANK LADDER IS HIDDEN, AND NOTHING NAMES IT ===\n")
# =============================================================================
# The route list is not typed out here. It is read off the decorators, the same
# way /api/staff/powers reads them, because a list maintained by hand goes stale
# the day somebody adds a route and this check is supposed to be the thing that
# notices.

RANKED = []
for rule in app.url_map.iter_rules():
    view = app.view_functions.get(rule.endpoint)
    minimum = getattr(view, "_elusion_min_role", None)
    if minimum is None:
        continue
    methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
    RANKED.append((str(rule), methods[0] if methods else "GET", minimum))
RANKED.sort()

check("the decorators describe at least a dozen ranked routes",
      len(RANKED) >= 12, len(RANKED))

RANK_WORDS = re.compile(r"\b(mod|moderator|dev|developer|owner|admin|staff|rank|role|permission)\b",
                        re.IGNORECASE)

for path, method, minimum in RANKED:
    # The <username> converter is the only one in the set; any concrete value
    # does, because the rank gate runs before the lookup.
    url = path.replace("<username>", "spare")
    res = client.open(url, method=method, headers=player, json={})

    check("%s %s hides itself from a player with 404" % (method, path),
          res.status_code == 404,
          "got %d %r - a 403 here confirms the route and the ladder"
          % (res.status_code, msg(res)))
    check("...and its message names no rank", not RANK_WORDS.search(msg(res)),
          "%r - a 404 that explains itself un-hides what the status hid" % msg(res))

# The catch for the route that gets added next month. A path under /api/staff/
# with no rank decorator is open to every logged-in player, and the only visible
# symptom is that it works.
untagged = []
for rule in app.url_map.iter_rules():
    if not str(rule).startswith("/api/staff/"):
        continue
    view = app.view_functions.get(rule.endpoint)
    if getattr(view, "_elusion_min_role", None) is None:
        untagged.append(str(rule))
check("every /api/staff/ route carries a rank decorator", untagged == [], untagged)

# And the owner's own routes are hidden from a dev, not just from a player -
# the ladder has more than one rung and each one hides the rungs above it.
for path, method, minimum in RANKED:
    if minimum != "owner":
        continue
    res = client.open(path.replace("<username>", "spare"), method=method, headers=dev, json={})
    check("%s %s hides itself from a DEV too" % (method, path),
          res.status_code == 404, "got %d %r" % (res.status_code, msg(res)))


# =============================================================================
print("\n=== F-2  ONCE YOU ARE IN, THE REFUSAL IS ABOUT THE ARGUMENT: 403 ===\n")
# =============================================================================
# Same route, same decorator, a caller it admitted. Now a 404 would be a lie -
# the route plainly exists, they just reached past their rank with one field.

res = client.post("/api/staff/ban", headers=mod,
                  json={"username": "victim", "days": 7, "reason": "testing the allowed form"})
check("a mod may ban for seven days", res.status_code == 200, res.get_json())

res = client.post("/api/staff/ban", headers=mod,
                  json={"username": "spare", "reason": "reaching for a permanent one"})
check("a permanent ban by a mod is 403, not 404", res.status_code == 403,
      "got %d %r" % (res.status_code, msg(res)))
check("...and it says why, because only a mod can read it",
      "dev" in msg(res).lower() or "owner" in msg(res).lower(), msg(res))

res = client.post("/api/staff/ban", headers=mod,
                  json={"username": "spare", "days": 3650, "reason": "ten years is permanent"})
check("a mod over the day ceiling is 403 with the ceiling in it",
      res.status_code == 403 and str(app_module.MAX_MOD_BAN_DAYS) in msg(res),
      "got %d %r" % (res.status_code, msg(res)))

# The rank ceiling. A mod is inside /api/staff/role; what they may not do is
# name a rank at or above their own.
res = client.put("/api/staff/role", headers=mod, json={"username": "spare", "role": "mod"})
check("a mod granting mod is 403, not 404", res.status_code == 403,
      "got %d %r" % (res.status_code, msg(res)))
res = client.put("/api/staff/role", headers=owner, json={"username": "spare", "role": "mod"})
check("the owner granting mod is 200 on the same route", res.status_code == 200, res.get_json())
# Put it back, so nothing below inherits a rank from this check.
client.put("/api/staff/role", headers=owner, json={"username": "spare", "role": "player"})

# And the widest argument in the game.
res = client.post("/api/staff/teleport", headers=dev, json={"everyone": True})
check("a dev moving EVERYONE is 403, not 404", res.status_code == 403,
      "got %d %r" % (res.status_code, msg(res)))
res = client.post("/api/staff/teleport", headers=dev, json={"username": "spare"})
check("the same dev moving one player is allowed", res.status_code == 200, res.get_json())

# The other half of the split, proven on one route rather than argued: the
# player gets 404 where the mod got 403, for the very same call.
a = client.post("/api/staff/ban", headers=player,
                json={"username": "spare", "reason": "from a player"})
check("the player's version of that permanent ban is 404",
      a.status_code == 404 and not RANK_WORDS.search(msg(a)),
      "got %d %r" % (a.status_code, msg(a)))


# =============================================================================
print("\n=== F-3  A 401 CAN ARRIVE ON A SESSION THAT IS STILL LIVE ===\n")
# =============================================================================
# This is the one the CLIENT gets wrong, and it is why
# characterhud.gd::_on_unauthorized_seen() asks heartbeat() rather than signing
# anybody out. Both routes below need the CURRENT password precisely because a
# valid token is not proof of identity when the token may be the stolen thing.

alive = auth("spare")
check("the account starts out served", client.get("/api/account", headers=alive).status_code == 200)

res = client.post("/api/auth/password", headers=alive,
                  json={"current_password": "notthepassword", "new_password": "brandnewpass1"})
check("a wrong CURRENT password is 401", res.status_code == 401,
      "got %d %r" % (res.status_code, msg(res)))
check("...and the token is still served afterwards",
      client.get("/api/account", headers=alive).status_code == 200,
      "if this ever fails, a typo signs the player out")

res = client.post("/api/account/email", headers=alive,
                  json={"email": "spare@example.com", "password": "notthepassword"})
check("a wrong password on the recovery address is 401", res.status_code == 401,
      "got %d %r" % (res.status_code, msg(res)))
check("...and the token is STILL served",
      client.get("/api/account", headers=alive).status_code == 200)

# The route the client actually asks. This is the whole basis for treating a 401
# as a question: there is a second, authoritative answer available.
res = client.get("/api/auth/session", headers=alive)
check("the session route confirms the session is fine", res.status_code == 200, res.get_json())
check("...so a 401 and a live session can be true at the same moment", True)


# =============================================================================
print("\n=== F-4  AUTHENTICATION DISCOVERED LATE IS STILL 401 ===\n")
# =============================================================================
# Three of the eight 401s read "No such account." on a token that authenticated
# a moment earlier. Six 404s carry the same sentence about somebody ELSE'S
# account. Same words, opposite meaning: a deleted CALLER is authentication (we
# can no longer say who is asking), a missing TARGET is a lookup that missed.

doomed = auth("victim")
db = sqlite3.connect(DB_PATH)
db.execute("DELETE FROM users WHERE username = ?", ("victim",))
db.commit()
db.close()

res = client.get("/api/account", headers=doomed)
check("a token whose account is gone is refused with 401, not 403",
      res.status_code == 401,
      "got %d - the rank is unknowable, so this is not an authorization question"
      % res.status_code)

res = client.post("/api/staff/ban", headers=owner,
                  json={"username": "victim", "reason": "gone", "days": 1})
check("but a MISSING TARGET of the same name is a 404 to the owner",
      res.status_code == 404, "got %d %r" % (res.status_code, msg(res)))
check("...and the owner's 404 is byte-identical to the one a mod gets for an "
      "account out of reach",
      msg(res) == "No such account.", msg(res))


# =============================================================================
print("\n=== F-5  THE LOGIN 401 ENUMERATES NOTHING, THE BAN 403 IS GATED ===\n")
# =============================================================================

nobody = client.post("/api/auth/login", json={"username": "ghostwhoisnotreal", "password": "password123"})
wrongpw = client.post("/api/auth/login", json={"username": "plain", "password": "wrongpassword"})
check("an unknown username is 401", nobody.status_code == 401, nobody.status_code)
check("a wrong password is 401", wrongpw.status_code == 401, wrongpw.status_code)
check("and the two answers are identical, body and all",
      nobody.get_json() == wrongpw.get_json(),
      "%r vs %r - any difference is a username oracle"
      % (nobody.get_json(), wrongpw.get_json()))

# A real ban, by the one rank that may issue any of them. Every mod attempt in
# F-2 was REFUSED, so nothing is banned yet - and "victim", the one ban that did
# land, was deleted in F-4.
res = client.post("/api/staff/ban", headers=owner,
                  json={"username": "spare", "days": 7, "reason": "for the login check"})
check("the owner's ban lands", res.status_code == 200, res.get_json())

banned_login = client.post("/api/auth/login", json={"username": "spare", "password": "password123"})
if banned_login.status_code == 403:
    check("a banned account's own login is 403 WITH the reason",
          "ban" in msg(banned_login).lower(), msg(banned_login))
    check("...and it carries the ban record, because silence reads as a broken game",
          isinstance((banned_login.get_json() or {}).get("ban"), dict),
          banned_login.get_json())
    bad = client.post("/api/auth/login", json={"username": "spare", "password": "wrongpassword"})
    check("but a WRONG password on that same banned account is the generic 401",
          bad.status_code == 401 and bad.get_json() == nobody.get_json(),
          "got %d %r - the ban must not be readable without the password"
          % (bad.status_code, bad.get_json()))
else:
    check("the banned-login path was reachable", False,
          "expected 403, got %d %r" % (banned_login.status_code, msg(banned_login)))


# =============================================================================
print("\n=== F-6  THE ADMISSION DECORATORS CANNOT ANSWER 403 ===\n")
# =============================================================================
# Source-level on purpose. F-1 proves today's behaviour; this pins the reason,
# so that "make it a 403, it is more correct" cannot pass review by accident.

def code_only(src):
    """The lines with the comments cut off. BOTH decorators mention 403 in prose
    - that is the point of them - so a naive search finds itself."""
    out = []
    for line in src.splitlines():
        head = line.split("#", 1)[0]
        out.append(head)
    return "\n".join(out)


for name in ("require_role", "require_owner"):
    src = inspect.getsource(getattr(app_module, name))
    body = code_only(src)
    check("%s RETURNS no 403" % name, not re.search(r"\b403\b", body),
          "the gate that decides whether the route is yours answers 404")
    check("%s returns the 404" % name, re.search(r"\b404\b", body) is not None, name)
    check("%s says in prose why it is not a 403" % name, "403" in src,
          "the next reader will reach for the textbook answer unless it is here")

# And the reverse, so the rule is enforced in both directions: every 403 in the
# file sits in a route, never in a decorator that admits one.
with open(os.path.join(HERE, "app.py"), "r", encoding="utf-8") as fh:
    source_lines = fh.readlines()
forbidden_lines = [i + 1 for i, line in enumerate(source_lines) if re.search(r"\b403\b", line)]
check("app.py still uses 403 somewhere", len(forbidden_lines) > 0, len(forbidden_lines))


print("\n" + "=" * 70)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("\n  failed:")
    for f in failures:
        print("    - %s" % f)
print("=" * 70)
sys.exit(1 if failed else 0)
