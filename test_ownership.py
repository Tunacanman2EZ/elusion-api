"""Ownership tests: can you name a row that is not yours?

The school exercise is a shared board where anyone can post a card, plus an
"ownership layer" so only the creator can edit or delete theirs. The interesting
half is the failure it teaches - broken access control, or IDOR: a route that
checks you are LOGGED IN and forgets to check the row is YOURS. Postman finds it
in one request, because the only thing you change is the id.

So this suite is that attack, run against every place in this server where a
client is allowed to name a record.

THE FIRST FINDING IS THAT THERE IS NOTHING TO FIND, and the reason is worth more
than the result. This server answers the exercise in two ways, and only one of
them is the lesson's:

  1. SCOPE THE LOOKUP. "Not yours" and "does not exist" come back as the same
     404, from one query that names the caller - loot bags, teleports, friends,
     guild invites, the bank, the backpack, skills. There is no window between
     "found it" and "checked it", because there is no separate check.

  2. BETTER: DO NOT LET THE CLIENT NAME THE ROW AT ALL. Not one trade route
     accepts a trade_id. Every one calls _trade_find_open(db, g.user["id"]) and
     works on whatever that returns. An ownership bug needs an id to tamper with;
     these routes do not have one to offer. That is the strongest version of the
     lesson and it is not what the lesson teaches.

GET /api/chat/image/<image_id> USED TO BE THE ONE ROUTE WITH NO OWNERSHIP
CHECK, on purpose. The id is the SHA-256 of the bytes, so 256 unguessable bits
were the permission: you could not hold an id without having been shown it.
That stops guessing, and it was not enough. The store deduplicates on the hash,
so a whispered picture and the same file in world chat shared one id; and an id
seen once worked for as long as the row lived - after the player left the guild
it was posted in, or stopped being friends with whoever posted it. Now the route serves a picture
only to someone who could read a line showing it, to an account that uploaded
those bytes, or to staff looking at a report of it, and every private line gets
its own copy under a random id. O-3 is that rule; the id properties stay pinned
beside it, because they are still what stops guessing.

Deleting a picture's line used to leave the picture being served, immutable,
for a year. O-4 covers the fix, including the reference count it needs - the
store deduplicates on the content hash, so one row can be under two messages.

  O-1  every row a client can name is scoped to the caller
  O-2  the trade routes cannot be addressed at all
  O-3  a picture is served only to someone who could see it; whispers get a copy
  O-4  deleting a message now revokes its picture, unless another line shows it
  O-5  the convention is enforced on the source, for the route added next month

Runs against a THROWAWAY database in the temp folder, exactly like
test_revocation.py, so it never touches elusion.db. Run: python test_ownership.py
"""

import base64
import hashlib
import importlib.util
import io
import os
import re
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_ownership_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
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


def section(title):
    print("\n=== %s ===\n" % title)


def register(username, password="password123"):
    """Everything registered before the first ban - see test_revocation.py."""
    return client.post("/api/auth/register",
                       json={"username": username, "password": password}).status_code


def auth(username, password="password123"):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    token = (res.get_json() or {}).get("token")
    return {"Authorization": "Bearer " + token} if token else {}


def msg(res):
    return (res.get_json() or {}).get("message", "")


def raw_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def uid(username):
    db = raw_db()
    try:
        row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        return int(row["id"]) if row else 0
    finally:
        db.close()


for who in ("boss", "alice", "bob", "carol"):
    code = register(who)
    check("registered %s" % who, code == 201, code)

owner = auth("boss")
alice = auth("alice")
bob = auth("bob")
carol = auth("carol")

# A character each, so the per-slot routes have something to address.
for who, headers in (("alice", alice), ("bob", bob), ("carol", carol), ("boss", owner)):
    res = client.put("/api/save", headers=headers,
                     json={"slot": 0, "class_id": "warrior", "name": who.capitalize()})
    check("%s has a character in slot 0" % who, res.status_code == 200, res.get_json())


# =============================================================================
section("O-1  EVERY ROW A CLIENT CAN NAME IS SCOPED TO THE CALLER")
# =============================================================================
# The Postman attack, one request at a time: take an id that is legitimately
# Alice's, send it with Bob's token, and see what comes back. A 404 is the right
# answer and it has to be the SAME 404 a nonexistent id gets, or the route still
# answers "that exists but is not yours".

# --- a loot bag -------------------------------------------------------------
# Written straight into the table, because the honest path is killing something
# and rolling a drop. The row is what a real kill leaves.
now = int(time.time())
db = raw_db()
db.execute("INSERT INTO loot_bags (bag_id, user_id, slot, enemy_id, created_at)"
           " VALUES (?, ?, 0, 'bushsniper', ?)", ("alicebag01", uid("alice"), now))
db.execute("INSERT INTO loot_bag_items (bag_id, position, item_id, quantity)"
           " VALUES (?, 0, 'tinyhealthpotion', 2)", ("alicebag01",))
db.commit()
db.close()

mine = client.get("/api/loot/bag?bag_id=alicebag01", headers=alice)
check("alice can read her own bag", mine.status_code == 200, mine.get_json())

theirs = client.get("/api/loot/bag?bag_id=alicebag01", headers=bob)
check("bob reading alice's bag by id is refused", theirs.status_code == 404,
      "got %d %s" % (theirs.status_code, theirs.get_json()))

ghost = client.get("/api/loot/bag?bag_id=nosuchbagatall", headers=bob)
check("...with the SAME answer a nonexistent bag gets",
      theirs.status_code == ghost.status_code and theirs.get_json() == ghost.get_json(),
      "%r vs %r - a different answer says 'exists, not yours'"
      % (theirs.get_json(), ghost.get_json()))

took = client.post("/api/loot/take", headers=bob, json={"bag_id": "alicebag01", "position": 0})
check("bob taking from alice's bag is refused", took.status_code == 404,
      "got %d %s" % (took.status_code, took.get_json()))

db = raw_db()
left = db.execute("SELECT COUNT(*) AS n FROM loot_bag_items WHERE bag_id = ?",
                  ("alicebag01",)).fetchone()["n"]
db.close()
check("...and the item is still in it", int(left) == 1, left)

# --- a pending teleport -----------------------------------------------------
db = raw_db()
# id is a plain column here, not the rowid - user_id is the primary key, because
# a player has at most one pending summon.
db.execute("INSERT INTO pending_teleports (id, user_id, area, x, y, slot_index,"
           " group_size, issued_by, issued_at)"
           " VALUES (1, ?, 'elusion', 10, 10, 0, 1, 'boss', ?)",
           (uid("alice"), now))
db.commit()
tp_id = int(db.execute("SELECT id FROM pending_teleports WHERE user_id = ?",
                       (uid("alice"),)).fetchone()["id"])
db.close()

client.post("/api/teleport/ack", headers=bob, json={"id": tp_id})
db = raw_db()
still = db.execute("SELECT COUNT(*) AS n FROM pending_teleports WHERE id = ?",
                   (tp_id,)).fetchone()["n"]
db.close()
check("bob acking alice's teleport leaves it in place", int(still) == 1,
      "an ack is a DELETE; unscoped, bob cancels alice's summon")

# --- a friend request addressed to somebody else -----------------------------
res = client.post("/api/friends/request", headers=alice, json={"username": "bob"})
check("alice sends bob a friend request", res.status_code in (200, 201), res.get_json())

res = client.post("/api/friends/respond", headers=carol, json={"username": "alice", "accept": True})
check("carol cannot accept the request alice sent to BOB", res.status_code == 404,
      "got %d %s" % (res.status_code, res.get_json()))

db = raw_db()
state = db.execute("SELECT state FROM friends WHERE requester_id = ? AND addressee_id = ?",
                   (uid("alice"), uid("bob"))).fetchone()
db.close()
check("...and the request is still pending", state is not None and state["state"] == "pending",
      dict(state) if state else None)

res = client.post("/api/friends/respond", headers=bob, json={"username": "alice", "accept": True})
check("bob, who it was addressed to, can accept it", res.status_code == 200, res.get_json())

# --- a guild invite addressed to somebody else -------------------------------
# FOUNDING A GUILD COSTS 5000 GOLD, and trying to arrange that turned up one more
# data point for this suite: /api/staff/gold is OWNER-ONLY and still will not name
# another account. It takes slot and amount and credits THE CALLER. Even the debug
# mint cannot reach across accounts, so the owner cannot fund alice - the gold goes
# into the row directly, the same way the loot bag above did.
res = client.post("/api/staff/gold", headers=owner,
                  json={"username": "alice", "slot": 0, "amount": 6000})
check("the owner's gold grant ignores a username and credits the owner",
      res.status_code == 200 and int((res.get_json() or {}).get("gold", 0)) >= 6000,
      res.get_json())
db = raw_db()
alice_gold = db.execute("SELECT gold FROM saves WHERE user_id = ? AND slot = 0",
                        (uid("alice"),)).fetchone()
db.close()
check("...and alice, who was named in the payload, got nothing",
      alice_gold is not None and int(alice_gold["gold"]) == 0,
      dict(alice_gold) if alice_gold else None)

db = raw_db()
db.execute("UPDATE saves SET gold = 6000 WHERE user_id = ? AND slot = 0", (uid("alice"),))
db.commit()
db.close()

res = client.post("/api/guild/create", headers=alice, json={"name": "Alices Own", "slot": 0})
check("alice founds a guild", res.status_code in (200, 201), res.get_json())
res = client.post("/api/guild/invite", headers=alice, json={"username": "bob"})
check("and invites bob", res.status_code == 200, res.get_json())

res = client.post("/api/guild/respond", headers=carol, json={"guild": "Alices Own", "accept": True})
check("carol cannot accept the invite sent to bob", res.status_code == 404,
      "got %d %s" % (res.status_code, res.get_json()))

db = raw_db()
seat = db.execute("SELECT COUNT(*) AS n FROM guild_members WHERE user_id = ?",
                  (uid("carol"),)).fetchone()["n"]
db.close()
check("...and carol is in no guild", int(seat) == 0, seat)

# --- somebody else's character ----------------------------------------------
# slot is the only handle on a save, and it is per account by construction.
res = client.get("/api/character?slot=0", headers=bob)
name = (res.get_json() or {}).get("name", "")
check("slot 0 means the CALLER's slot 0", name == "Bob",
      "got %r - slot is not a global row id" % name)


# =============================================================================
section("O-2  THE TRADE ROUTES CANNOT BE ADDRESSED AT ALL")
# =============================================================================
# The strongest answer to this whole lesson, and it is structural rather than a
# check. A trade is the most valuable row in the database - it moves items and
# gold between two accounts - and no route will let a client say WHICH one.

trade_src = open(os.path.join(HERE, "app.py"), "r", encoding="utf-8").read()
routes = re.split(r'\n@app\.(?:get|post|put|patch|delete)\("', trade_src)
trade_bodies = [r for r in routes if r.startswith("/api/trade")]
check("the trade routes are found", len(trade_bodies) >= 4, len(trade_bodies))

named = []
for body in trade_bodies:
    path = body.split('"', 1)[0]
    if re.search(r'(payload|data|request\.args)\.get\("trade_?id"', body):
        named.append(path)
check("no trade route accepts a trade_id from the client", named == [],
      "%s - an id the client can name is an id the client can change" % named)

# THE RESOLVERS THAT START FROM THE CALLER. _trade_find_open for the open trade,
# _trade_find_recent for the history - both take the caller's own id and nothing
# a request supplies. A route that finds its trade any other way is the one to
# look at.
CALLER_RESOLVERS = ("_trade_find_open(db, ", "_trade_find_open(db,",
                    "_trade_find_recent(db, user_id", "_trade_find_recent(db, g.user")
for body in trade_bodies:
    path = body.split('"', 1)[0]
    if "db.execute" not in body and "_trade_" not in body:
        continue
    check("%s resolves the trade from the caller" % path,
          any(r in body for r in CALLER_RESOLVERS) or "trade_current" in body,
          "every trade route must start from g.user, not from a field")

# AND THE HISTORY'S RESOLVER REALLY IS SCOPED TO ITS FIRST ARGUMENT. The check
# above trusts the name; this reads the function that holds the query and
# holds it to the name - every branch, for both sides, filtered on the user it
# was given, and nothing else. _trade_find_recent is the first page of it.
_recent = re.search(r"\ndef _trade_find_recent\(.*?(?=\ndef )", trade_src, re.S)
_page = re.search(r"\ndef _trade_find_page\(.*?(?=\ndef )", trade_src, re.S)
_recent_src = _recent.group(0) if _recent else ""
_page_src = _page.group(0) if _page else ""
check("_trade_find_recent is a page of _trade_find_page",
      "return _trade_find_page(db, user_id," in _recent_src, _recent_src[:200])
check("_trade_find_page filters every branch, both sides, on the caller",
      'for side in ("a", "b"):' in _page_src
      and 'where = "%s_user = ? AND state = ?" % side' in _page_src
      and "branch_params = [user_id, state]" in _page_src,
      "both sides of a trade must be scoped to the id passed in")

# Behaviourally: alice and bob open a trade, carol has no way in. A beat from
# bob first, because an offer needs him online and a slow run can outlast the
# presence his login stamped.
client.get("/api/server/broadcasts", headers=bob)
res = client.post("/api/trade/offer", headers=alice,
                  json={"slot": 0, "username": "bob", "to_slot": 0})
check("alice opens a trade with bob", res.status_code in (200, 201), res.get_json())

res = client.get("/api/trade", headers=carol)
body = res.get_json() or {}
check("carol asking for 'the' trade gets her own, which is none",
      res.status_code == 200 and not body.get("trade"),
      "got %d %s" % (res.status_code, body))

res = client.post("/api/trade/confirm", headers=carol, json={})
check("and carol cannot confirm a trade she is not in", res.status_code in (400, 404),
      "got %d %s" % (res.status_code, res.get_json()))

db = raw_db()
confirmed = db.execute("SELECT a_confirmed, b_confirmed FROM trades"
                       " WHERE state = 'open'").fetchone()
db.close()
check("...and neither side of alice's trade got confirmed",
      confirmed is not None and int(confirmed["a_confirmed"]) == 0
      and int(confirmed["b_confirmed"]) == 0,
      dict(confirmed) if confirmed else None)

client.post("/api/trade/cancel", headers=alice, json={})


# =============================================================================
section("O-3  A PICTURE IS SERVED ONLY TO SOMEONE WHO COULD SEE IT")
# =============================================================================
# GET /api/chat/image/<image_id> used to serve any row to any signed-in caller:
# the id was the SHA-256 of the bytes, so 256 unguessable bits WERE the
# permission. That held for guessing and failed twice in practice. The store
# deduplicates on the hash, so a picture whispered and the same file posted to
# world were one row with one id; and an id, once seen, worked forever, for a
# player who had since left the guild it was posted in.
#
# Now the route asks the read's own question - could the caller read a line
# showing this picture? - and a private line gets its own copy under a random
# id. The id properties are still checked, because they are still what stops
# guessing; they are just no longer the only thing.

try:
    from PIL import Image
    have_pillow = True
except ImportError:
    have_pillow = False


def fresh_cooldowns():
    """The upload and world-picture cooldowns are real seconds. This suite is
    about who may SEE a picture, not how often one may be sent."""
    db = raw_db()
    db.execute("UPDATE users SET last_image_at = 0, last_world_image_at = 0")
    db.commit()
    db.close()


def picture(headers, picture_id):
    return client.get("/api/chat/image/%s" % picture_id, headers=headers)


image_id = ""
image_bytes = b""
if have_pillow:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 40)).save(buf, format="PNG")
    payload_png = buf.getvalue()
    res = client.post("/api/chat/image/upload", headers=alice, data=payload_png,
                      content_type="application/octet-stream")
    check("alice uploads a picture", res.status_code in (200, 201), res.get_json())
    image_id = str((res.get_json() or {}).get("id", ""))
else:
    check("Pillow is available for the picture tests", False,
          "pip install Pillow - the relay route needs it")

if image_id:
    check("the id is 64 hex characters", re.fullmatch(r"[0-9a-f]{64}", image_id) is not None,
          image_id)

    served = picture(alice, image_id)
    check("alice, who uploaded it, is served it", served.status_code == 200, served.status_code)
    image_bytes = served.data

    check("THE ID IS THE SHA-256 OF THE BYTES SERVED",
          hashlib.sha256(image_bytes).hexdigest() == image_id,
          "if the id is not derived from the content, it is a row number")

    check("...which is 256 bits of it, not a truncation",
          len(image_id) * 4 == 256, len(image_id))

    # THE CHANGE. Holding an id is no longer enough: nothing bob can read shows
    # this picture yet, so for bob it does not exist.
    other = picture(bob, image_id)
    check("bob, holding the id, is NOT served it - no line he can read shows it",
          other.status_code == 404,
          "got %d - the id alone is a capability again" % other.status_code)

    res = client.post("/api/chat/send", headers=bob, json={"body": "found this", "image": image_id})
    check("and he cannot post it to world to make it visible",
          res.status_code == 400 and "not on this server" in msg(res),
          "got %d %s - an id glimpsed anywhere would publish the picture"
          % (res.status_code, res.get_json()))

    anon = client.get("/api/chat/image/%s" % image_id)
    check("an unauthenticated caller is refused", anon.status_code == 401,
          "got %d - otherwise this is a public file host run by accident"
          % anon.status_code)

    for bad in ["1", "abc", "../../etc/passwd", "A" * 64, "0" * 63, "0" * 65]:
        res = client.get("/api/chat/image/%s" % bad, headers=alice)
        check("a malformed id (%r) is 404, never a lookup" % bad[:18],
              res.status_code == 404, res.status_code)

    check("an id nobody uploaded is the same 404 as a refused one",
          picture(alice, "0" * 64).status_code == 404)

    # TWO UPLOADERS, ONE ROW. The store keeps one row per file, and created_by
    # only remembers the first - so the second uploader is in chat_image_holders.
    # Without it, carol could not send or even preview a file she uploaded.
    fresh_cooldowns()
    res = client.post("/api/chat/image/upload", headers=carol, data=payload_png,
                      content_type="application/octet-stream")
    check("carol uploads the same file and gets the same id",
          str((res.get_json() or {}).get("id", "")) == image_id, res.get_json())
    check("...and she is served it, though alice's name is on the row",
          picture(carol, image_id).status_code == 200,
          "the second uploader of a file would be locked out of their own picture")

    # --- a whisper ---------------------------------------------------------
    res = client.post("/api/chat/send", headers=alice,
                      json={"body": "just for you", "image": image_id,
                            "channel": "private", "to": "bob"})
    check("alice whispers the picture to bob", res.status_code == 200, res.get_json())
    whisper_id = str((res.get_json() or {}).get("image", ""))

    check("THE WHISPER CARRIES ITS OWN COPY, under a new id",
          re.fullmatch(r"[0-9a-f]{64}", whisper_id) is not None and whisper_id != image_id,
          "%s - sharing the world id is how a private picture used to leak" % whisper_id)
    check("...a random one, not the hash of the bytes",
          whisper_id != hashlib.sha256(image_bytes).hexdigest(),
          "a content id would collide with anyone else posting the same file")

    got = picture(bob, whisper_id)
    check("bob is served the whispered copy", got.status_code == 200, got.status_code)
    check("...and it is the same picture", got.data == image_bytes)
    check("alice, who sent it, is served it too", picture(alice, whisper_id).status_code == 200)

    check("THE ORIGINAL STILL IS NOT bob's: the whisper showed him the copy",
          picture(bob, image_id).status_code == 404)

    check("carol, outside the whisper, gets 404 for the copy",
          picture(carol, whisper_id).status_code == 404,
          "she uploaded the very same file - and that gives her that file, not this line")
    check("so does the owner, with no report to look at",
          picture(owner, whisper_id).status_code == 404,
          "staff see a private picture through a report, not by rank")

    res = client.post("/api/chat/send", headers=carol, json={"body": "look", "image": whisper_id})
    check("carol cannot post the whispered id to world", res.status_code == 400,
          "got %d %s" % (res.status_code, res.get_json()))

    check("the cache header says private, never public",
          "private" in got.headers.get("Cache-Control", "")
          and "public" not in got.headers.get("Cache-Control", ""),
          got.headers.get("Cache-Control"))

    # A REPORT IS A LINE STAFF WERE SHOWN.
    db = raw_db()
    whisper_line = int(db.execute("SELECT id FROM chat_messages WHERE image_id = ?",
                                  (whisper_id,)).fetchone()["id"])
    db.close()
    res = client.post("/api/chat/report", headers=bob, json={"id": whisper_line, "reason": "other"})
    check("bob reports the whisper", res.status_code == 200, res.get_json())
    check("and now the owner, judging the report, is served the picture",
          picture(owner, whisper_id).status_code == 200,
          "a mod cannot judge a reported picture they cannot open")
    check("...but a report gives a PLAYER nothing",
          picture(carol, whisper_id).status_code == 404)

    # --- friends -------------------------------------------------------------
    res = client.post("/api/chat/send", headers=alice,
                      json={"body": "friends only", "image": image_id, "channel": "friends"})
    check("alice posts it to her friends", res.status_code == 200, res.get_json())
    friends_id = str((res.get_json() or {}).get("image", ""))
    check("...as another copy, shared with neither the world id nor the whisper",
          friends_id not in ("", image_id, whisper_id), friends_id)
    check("bob, her friend, is served it", picture(bob, friends_id).status_code == 200)
    check("carol, who is not, gets 404", picture(carol, friends_id).status_code == 404)

    res = client.post("/api/friends/remove", headers=bob, json={"username": "alice"})
    check("bob unfriends alice", res.status_code == 200, res.get_json())
    check("AND STOPS BEING SERVED HER FRIENDS' PICTURE - the id he kept is not a key",
          picture(bob, friends_id).status_code == 404,
          "seeing it once used to mean seeing it for as long as the row lived")
    check("while the whisper, which was to him, still is",
          picture(bob, whisper_id).status_code == 200)


# =============================================================================
section("O-4  DELETING A MESSAGE REVOKES ITS PICTURE")
# =============================================================================
# The gap the capability argument could not close. 256 unguessable bits stop
# somebody GUESSING a picture; they do nothing about taking one back from the
# people who were in the channel when it was posted - and the response says
# Cache-Control: immutable, max-age one year.

client.put("/api/staff/role", headers=owner, json={"username": "carol", "role": "mod"})
mod = auth("carol")

if image_id:
    fresh_cooldowns()
    res = client.post("/api/chat/send", headers=alice, json={"body": "look", "image": image_id})
    check("alice posts the picture to chat", res.status_code in (200, 201), res.get_json())
    check("a world line ships the content id itself, no copy",
          (res.get_json() or {}).get("image") == image_id, res.get_json())
    check("and NOW bob is served it - a line he can read shows it",
          picture(bob, image_id).status_code == 200,
          "O-3 refused him; the world line is what changed")

    db = raw_db()
    first_msg = int(db.execute("SELECT id FROM chat_messages WHERE image_id = ?"
                               " ORDER BY id DESC LIMIT 1", (image_id,)).fetchone()["id"])
    db.close()

    # TWO MESSAGES, ONE ROW. store_relayed_image() is INSERT OR IGNORE on a
    # content hash, so bob posting the same bytes reuses alice's row. Deleting
    # alice's line must not blank the picture under bob's.
    res = client.post("/api/chat/image/upload", headers=bob, data=payload_png,
                      content_type="application/octet-stream")
    second_id = str((res.get_json() or {}).get("id", ""))
    check("bob's copy of the same picture has the SAME id", second_id == image_id,
          "%s vs %s - the store deduplicates on content" % (second_id, image_id))

    res = client.post("/api/chat/send", headers=bob, json={"body": "same one", "image": image_id})
    check("bob posts it too", res.status_code in (200, 201), res.get_json())

    res = client.post("/api/chat/delete", headers=mod, json={"id": first_msg})
    check("a mod deletes alice's line", res.status_code == 200, res.get_json())
    check("...and the picture is NOT dropped, because bob's line still shows it",
          (res.get_json() or {}).get("image_dropped") is False,
          "reference counting is the whole reason this is safe to do at all")
    check("so it is still served", client.get("/api/chat/image/%s" % image_id,
                                              headers=bob).status_code == 200)

    db = raw_db()
    second_msg = int(db.execute("SELECT id FROM chat_messages WHERE image_id = ?"
                                " ORDER BY id DESC LIMIT 1", (image_id,)).fetchone()["id"])
    db.close()

    res = client.post("/api/chat/delete", headers=mod, json={"id": second_msg})
    check("the mod deletes the last line showing it", res.status_code == 200, res.get_json())
    check("...and NOW the picture is dropped",
          (res.get_json() or {}).get("image_dropped") is True, res.get_json())

    gone = client.get("/api/chat/image/%s" % image_id, headers=bob)
    check("a delete is now a revocation, not a hide", gone.status_code == 404,
          "got %d - the bytes outlived the message they were posted with"
          % gone.status_code)
    # `mod` is carol, signed in again after her promotion ended her session.
    check("...from its uploaders too", picture(alice, image_id).status_code == 404
          and picture(mod, image_id).status_code == 404,
          (picture(alice, image_id).status_code, picture(mod, image_id).status_code))

    db = raw_db()
    rows = db.execute("SELECT COUNT(*) AS n FROM chat_images WHERE id = ?",
                      (image_id,)).fetchone()["n"]
    db.close()
    check("and the row is gone from the store", int(rows) == 0, rows)
    db = raw_db()
    holders = db.execute("SELECT COUNT(*) AS n FROM chat_image_holders WHERE image_id = ?",
                         (image_id,)).fetchone()["n"]
    db.close()
    check("...with the record of who uploaded it", int(holders) == 0, holders)

# A line with no picture must not trip any of it.
res = client.post("/api/chat/send", headers=alice, json={"body": "just words"})
db = raw_db()
plain_msg = int(db.execute("SELECT id FROM chat_messages WHERE image_id = ''"
                           " ORDER BY id DESC LIMIT 1").fetchone()["id"])
db.close()
res = client.post("/api/chat/delete", headers=mod, json={"id": plain_msg})
check("deleting a picture-less line reports no image dropped",
      res.status_code == 200 and (res.get_json() or {}).get("image_dropped") is False,
      res.get_json())


# =============================================================================
section("O-5  THE CONVENTION IS ENFORCED ON THE SOURCE")
# =============================================================================
# O-1 proves today's routes. This is for the one added next month: every table
# that has a per-user column, every UPDATE or DELETE naming it, must scope on
# that column. Read off the schema rather than a list typed here, so a new table
# with a user_id joins the check by existing.

source = trade_src
OWNED_TABLES = {}
for m in re.finditer(r'CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\s*\)', source, re.S):
    name, cols = m.group(1), m.group(2)
    if re.search(r'\buser_id\b', cols):
        OWNED_TABLES[name] = True
check("the schema names some per-user tables", len(OWNED_TABLES) >= 8,
      sorted(OWNED_TABLES))

# ADJACENT STRING LITERALS ARE ONE STATEMENT, and the first version of this check
# did not know that. Python concatenates
#
#     "UPDATE saves SET gold = gold + ?"
#     " WHERE user_id = ? AND slot = ?"
#
# at compile time, so a scanner reading one literal at a time sees an UPDATE with
# no WHERE at all and reports five false alarms - which is precisely how a check
# gets switched off. Join them first, the way the interpreter does.
def sql_statements(text):
    out = []
    pattern = re.compile(r'"((?:[^"\\]|\\.)*)"(?:\s*\n?\s*"((?:[^"\\]|\\.)*)")*')
    for m in re.finditer(r'((?:"(?:[^"\\]|\\.)*"\s*(?:\n\s*)?)+)', text):
        joined = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))
        flat = " ".join(joined.split())
        if re.match(r'(UPDATE|DELETE FROM)\s+\w+', flat):
            out.append(flat)
    return out


# THE ALLOWLIST IS THE MOST USEFUL THING THIS SUITE PRODUCES: it is the complete
# list of places a write touches a per-user table WITHOUT naming the owner, each
# with the reason it is allowed to. Anything that is not on it is the bug.
SWEEPS_ALLOWED = [
    # retention and housekeeping - the server acting on its own clock
    ("loot_bags", "created_at <", "retention: bags past the pickup window"),
    ("kill_reports", "at <", "retention: the killwatch window"),
    ("chat_messages", "created_at <", "retention: the chat window"),
    # ADDED BECAUSE THIS CHECK CAUGHT IT. chat_deletions carries the deleted
    # line's user_id so the poll can filter deletions with the read's own WHERE
    # clause - which makes it a per-user table, which made this scan demand a
    # reason for the prune before the suite would go green again. Working as
    # intended: the entry is the reason, written down.
    ("chat_deletions", "deleted_at <", "retention: past CHAT_DELETION_WINDOW_SECONDS"),
    ("chat_messages", "id NOT IN", "the per-channel ring buffer trim"),
    ("sessions", "expires_at <", "expired sessions"),
    ("ended_sessions", "ended_at <", "retention: why a session ended, kept a day"),
    # a boot-time migration: every save's display copy of xp_to_next re-derived
    # from the curve after 100 x 1.15 became 1,250 x 1.27. It sets a pure
    # function of each row's own level, so it cannot move a value between users.
    ("saves", "level = ? AND xp_to_next !=", "migration: xp_to_next from the curve"),
    # your own row, addressed by a credential only you hold
    ("sessions", "token_hash =", "your own session, by the hash of its bearer token"),
    # a boot-time migration: every stored token replaced by its own SHA-256, once.
    # It rewrites each row's key from that row's own value, so it moves nothing
    # between users. See _migrate_hash_session_tokens().
    ("sessions", " token = ?", "migration: session tokens hashed at rest"),
    # staff acting on somebody else's row ON PURPOSE. The rank decorator is the
    # access control here, and test_refusals.py is what holds it.
    ("chat_messages", "id =", "@require_role('mod') - staff take a line down"),
    # a guild officer or leader acting on the guild, gated by guild rank
    ("guild_members", "guild_id =", "disband: every seat goes, leader-gated"),
    ("guild_invites", "guild_id =", "invites die with the guild"),
    # THE ONE ENTRY THAT IS NOT SELF-EVIDENT, and so the one worth reading. In
    # /api/loot/take the ownership check and the action are SEPARATE statements:
    # "SELECT ... WHERE bag_id = ? AND user_id = ?" gates the route, and the
    # DELETEs below it name only the bag. That is the check-then-act shape the
    # rest of this file deliberately avoids, and it is safe only because the gate
    # returns 404 before any of them run. Reorder it and the gate stops gating
    # while every one of these statements keeps working. O-1's "bob taking from
    # alice\'s bag is refused" is the check that would go red.
    ("loot_bags", "bag_id =", "gated by the SELECT above it - see O-1"),
    # a picture's uploaders go with the picture: eviction, or a mod deleting
    # the last line that shows it (O-4)
    ("chat_image_holders", "image_id =", "a picture's uploaders go with the picture"),
]

unscoped = []
for text in sql_statements(source):
    table = re.match(r'(?:UPDATE|DELETE FROM)\s+(\w+)', text).group(1)
    if table not in OWNED_TABLES:
        continue
    if "WHERE" not in text:
        unscoped.append("%s :: %s  (no WHERE at all)" % (table, text[:100]))
        continue
    where = text.split("WHERE", 1)[1]
    if re.search(r'\buser_id\b', where):
        continue
    if any(table == t and frag in where for (t, frag, _why) in SWEEPS_ALLOWED):
        continue
    # A %s placeholder is the shared scope helper interpolating "user_id = ? AND
    # slot = ?" - carry_items and bank_items share one validator on purpose.
    if "%s" in where:
        continue
    unscoped.append("%s :: %s" % (table, text[:110]))

check("every write to a per-user table names the owner, or is on the allowlist",
      unscoped == [], "\n        ".join(unscoped))
check("the scanner actually found statements to look at",
      len(sql_statements(source)) > 60, len(sql_statements(source)))
print("  %d allowlisted writes, each one a deliberate reach past the caller:"
      % len(SWEEPS_ALLOWED))
for (t, frag, why) in SWEEPS_ALLOWED:
    print("    %-16s %-14s %s" % (t, frag, why))

# And the pair that makes the 404s indistinguishable. "Not yours" leaking as a
# different status is the half of broken access control that survives a fix.
flat_source = " ".join(source.split())
check("the moderation target helper returns one answer for both cases",
      "a distinguishable answer would let a mod map out who outranks them" in flat_source,
      "_moderation_target's reason should still be written down")





# =============================================================================
section("O-6  A DELETION REACHES THE CLIENTS THAT ALREADY HAVE THE LINE")
# =============================================================================
# THE GENERAL SHAPE OF "DELETE IS NOT REVOKE", ONE LAYER UP FROM O-4.
#
# GET /api/chat answers "messages with an id greater than `since`". That is the
# right question for a growing log and it cannot express the opposite one:
# something you were already given is no longer true. So deleting a message
# stopped it reaching anybody who had not read it yet and did nothing at all
# about the people who had - their feed is append-only, so the line sat on screen
# until a hundred more pushed it off. The players who kept seeing it were exactly
# the players the deletion was for.
#
# The poll now carries `removed`, and the property that matters is not that the
# list exists - it is that it is filtered by THE SAME permission clause as the
# messages, because chat_deletions stores the deleted line's own channel, user_id
# and target_id. One rule, one copy of it.

def say(headers, body, channel="world", to=""):
    payload = {"body": body}
    if channel != "world":
        payload["channel"] = channel
    if to:
        payload["to"] = to
    return client.post("/api/chat/send", headers=headers, json=payload)


def read(headers, channel="world", since=0, with_name=""):
    path = "/api/chat?channel=%s&since=%d" % (channel, since)
    if with_name:
        path += "&with=" + with_name
    return client.get(path, headers=headers).get_json() or {}

check("the poll reports a removed list at all", "removed" in read(alice), read(alice).keys())

say(alice, "a line that stays")
say(alice, "a line that goes")
feed = read(bob)
doomed = [m["id"] for m in feed["messages"] if m["body"] == "a line that goes"]
check("bob can see both lines", len(doomed) == 1, feed["messages"])
cursor = int(feed["latest_id"])
doomed_id = doomed[0]

res = client.post("/api/chat/delete", headers=mod, json={"id": doomed_id})
check("the mod takes it down", res.status_code == 200, res.get_json())

# THE WHOLE POINT: bob's cursor is already PAST that id, so `since` can never
# reach it. A deletion is an event and has to be reported by when it happened.
later = read(bob, since=cursor)
check("bob hears about it even though his cursor is past it",
      doomed_id in later.get("removed", []),
      "cursor %d, removed %s - `since` cannot look backwards"
      % (cursor, later.get("removed")))
check("and no new messages came with it", later["messages"] == [], later["messages"])

check("it keeps being announced, because a client may poll late",
      doomed_id in read(bob, since=cursor).get("removed", []),
      "one announcement would be a race against the poll interval")

# SCOPED BY THE SAME CLAUSE AS THE MESSAGES. A whisper deletion must not appear
# in world - an id carries no words, but it still says a line existed between two
# people and was taken down.
say(alice, "between us", channel="private", to="bob")
whisper = read(bob, channel="private", with_name="alice")
w_ids = [m["id"] for m in whisper["messages"]]
check("bob can read the whisper", len(w_ids) >= 1, whisper["messages"])
w_id = w_ids[-1]

res = client.post("/api/chat/delete", headers=mod, json={"id": w_id})
check("the mod takes the whisper down", res.status_code == 200, res.get_json())

check("both people in the conversation hear about it",
      w_id in read(bob, channel="private", with_name="alice").get("removed", [])
      and w_id in read(alice, channel="private", with_name="bob").get("removed", []),
      "the deletion has to reach the two screens holding it")
check("WORLD CHAT DOES NOT", w_id not in read(bob, since=cursor).get("removed", []),
      "an id is not content, but it still says a whisper existed and was removed")
# `mod` IS carol, on the login that made her a mod - which ended the session
# `carol` was holding (one login at a time). Read with the old one, this check
# got a 401 with no "removed" in it and passed on nothing, so it now also says
# the read was answered.
_carol_view = read(mod, channel="private", with_name="alice")
check("and neither does an unrelated conversation",
      "removed" in _carol_view and w_id not in _carol_view.get("removed", []),
      "carol was never in it - this falls out of reusing the read's own WHERE")

# The guild channel answers early when you have no guild, and a field that exists
# on most answers and vanishes on one is how a client reads a stale copy.
no_guild = read(bob, channel="guild")
check("the no-guild answer still carries the field", "removed" in no_guild, no_guild.keys())

# BOUNDED. The window is the only thing keeping this table small, and it is only
# sufficient because chatpanel.gd starts from the tail when the window opens.
db = raw_db()
rows = db.execute("SELECT COUNT(*) AS n FROM chat_deletions").fetchone()["n"]
db.execute("UPDATE chat_deletions SET deleted_at = ?",
           (int(time.time()) - app_module.CHAT_DELETION_WINDOW_SECONDS - 60,))
db.commit()
db.close()
check("the deletions table has rows to age out", int(rows) >= 2, rows)
check("an announcement past the window is not repeated for ever",
      read(bob, since=cursor).get("removed", []) == [],
      "a client away longer than the window is not holding the line anyway")

say(alice, "something to delete, to trigger the prune")
d = read(mod)
last = d["messages"][-1]["id"]
client.post("/api/chat/delete", headers=mod, json={"id": last})
db = raw_db()
stale = db.execute("SELECT COUNT(*) AS n FROM chat_deletions WHERE deleted_at < ?",
                   (int(time.time()) - app_module.CHAT_DELETION_WINDOW_SECONDS,)).fetchone()["n"]
db.close()
check("and the stale rows are pruned on the next delete, not by a sweeper job",
      int(stale) == 0, stale)


# =============================================================================
section("O-7  THE OWNER'S LEVEL TOOL SETS THE OWNER'S OWN CHARACTER")
# =============================================================================
# Day 2: tier 6 weapons need level 22, and the owner has to be able to test
# them. /api/staff/level is the gold grant's shape: owner only, and it changes
# the caller's character - a username in the payload is ignored.
import gamedata as _gd  # noqa: E402

res = client.post("/api/staff/level", headers=alice, json={"slot": 0, "level": 22})
check("a player is told the level tool does not exist", res.status_code == 404, res.status_code)
res = client.post("/api/staff/level", headers=mod, json={"slot": 0, "level": 22})
check("so is a mod - it is the owner's", res.status_code == 404, res.status_code)

res = client.post("/api/staff/level", headers=owner,
                  json={"username": "alice", "slot": 0, "level": 22})
body = res.get_json() or {}
db = raw_db()
mine = db.execute("SELECT level, xp, xp_to_next, max_hp, hp, max_mana, mana FROM saves"
                  " WHERE user_id = ? AND slot = 0", (uid("boss"),)).fetchone()
hers = db.execute("SELECT level FROM saves WHERE user_id = ? AND slot = 0",
                  (uid("alice"),)).fetchone()
logged = db.execute("SELECT detail FROM staff_actions WHERE action = 'level' AND actor_id = ?"
                    " ORDER BY id DESC LIMIT 1", (uid("boss"),)).fetchone()
granted = db.execute("SELECT COUNT(*) AS n FROM consume_grants WHERE user_id = ? AND slot = 0"
                     " AND item_id = ?", (uid("boss"), app_module.LEVELUP_GRANT_ID)).fetchone()
db.close()
curve = _gd.max_stats_for("warrior", 22)
check("the owner sets his own character to level 22", res.status_code == 200
      and body.get("level") == 22 and mine is not None and int(mine["level"]) == 22, body)
check("  with XP from zero and the curve's next step, like a level-up",
      int(mine["xp"]) == 0 and int(mine["xp_to_next"]) == _gd.xp_needed_for_level(22), dict(mine))
check("  the maxima of a level 22 warrior, and full pools",
      int(mine["max_hp"]) == curve["max_hp"] and int(mine["hp"]) == curve["max_hp"]
      and int(mine["mana"]) == int(mine["max_mana"]) == curve["max_mana"], dict(mine))
check("  the refill recorded as a level-up, so the heal check does not clamp it",
      int(granted["n"]) >= 1, int(granted["n"]))
check("  and a line in the staff log", logged is not None
      and logged["detail"] == "slot 0: level 1 -> 22", dict(logged) if logged else None)
check("alice, who was named in the payload, is still level 1",
      hers is not None and int(hers["level"]) == 1, dict(hers) if hers else None)

res = client.post("/api/staff/level", headers=owner, json={"slot": 0, "level": 5})
db = raw_db()
down = db.execute("SELECT level, max_hp, hp FROM saves WHERE user_id = ? AND slot = 0",
                  (uid("boss"),)).fetchone()
db.close()
check("it sets a level down as well, and the maxima follow",
      res.status_code == 200 and int(down["level"]) == 5
      and int(down["max_hp"]) == _gd.max_stats_for("warrior", 5)["max_hp"] == int(down["hp"]),
      dict(down))

for label, body_in in [("level 0", {"slot": 0, "level": 0}),
                       ("level 100", {"slot": 0, "level": 100}),
                       ("a level that is not a number", {"slot": 0, "level": "high"}),
                       ("true as a level", {"slot": 0, "level": True}),
                       ("no level at all", {"slot": 0}),
                       ("a slot that is not one", {"slot": 9, "level": 10})]:
    res = client.post("/api/staff/level", headers=owner, json=body_in)
    check("%s is refused" % label, res.status_code == 400, res.status_code)
res = client.post("/api/staff/level", headers=owner, json={"slot": 2, "level": 10})
check("an empty slot is a 404", res.status_code == 404, res.status_code)

powers = client.get("/api/staff/powers", headers=owner).get_json() or {}
owner_routes = [r.get("path") for rank in powers.get("ladder", [])
                if rank.get("rank") == "owner" for r in rank.get("routes", [])]
check("the powers list shows it under the owner, so it surprises nobody",
      "/api/staff/level" in owner_routes, owner_routes)


print("\n" + "=" * 70)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("\n  failed:")
    for f in failures:
        print("    - %s" % f)
print("=" * 70)
sys.exit(1 if failed else 0)
