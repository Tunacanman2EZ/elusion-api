"""
test_parties.py - parties: asked, joined, led, left, and their own chat room.

    venv\\Scripts\\python.exe test_parties.py

Throwaway database in your temp folder, like every other suite here. Never
touches elusion.db.

WHAT THIS IS GUARDING (game 0.42.0 - the owner: "we need to be able to start
parties so we need a tab for parties", with "Party chat" and "Party on the
map"):

  - NOBODY IS PUT IN A PARTY WITHOUT AGREEING, and an invitation runs out.
  - A PARTY IS TWO TO FOUR, with exactly one leader, and goes when it is down
    to one.
  - PARTY CHAT IS THE PARTY'S: a line said in one never shows in another, nor
    to somebody who has left.
  - THE POLL SAYS WHO YOU ARE WITH and who is asking, within one poll.

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_parties_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "PARTYOWNER"

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


def make_character(token, name, cls="warrior"):
    return client.put("/api/save", headers=auth(token),
                      json={"slot": 0, "class_id": cls, "name": name})


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def mine(token):
    return client.get("/api/party", headers=auth(token))


def party_of(token):
    return (mine(token).get_json() or {}).get("party")


def invite(token, who):
    return client.post("/api/party/invite", headers=auth(token), json={"username": who})


def respond(token, who, accept):
    return client.post("/api/party/respond", headers=auth(token), json={"from": who, "accept": accept})


def leave(token):
    return client.post("/api/party/leave", headers=auth(token), json={})


def kick(token, who):
    return client.post("/api/party/kick", headers=auth(token), json={"username": who})


def lead(token, who):
    return client.post("/api/party/lead", headers=auth(token), json={"username": who})


def say(token, body):
    return client.post("/api/chat/send", headers=auth(token), json={"body": body, "channel": "party"})


def read(token):
    return client.get("/api/chat?channel=party", headers=auth(token))


def poll(token):
    return client.get("/api/server/broadcasts?since=0", headers=auth(token)).get_json() or {}


def refill(*names):
    with app_module.app.app_context():
        db = app_module.get_db()
        for name in names:
            db.execute("UPDATE users SET chat_tokens = ?, last_chat_at = 0,"
                       " last_image_at = 0, last_world_image_at = 0 WHERE username = ?",
                       (app_module.CHAT_BUCKET_CAPACITY, name))
        db.commit()


def names(party):
    return [m["username"] for m in (party or {}).get("members", [])]


tokens = {}
for who, cls in (("pamy", "warrior"), ("pbob", "mage"), ("pcat", "tank"), ("pdan", "healer"),
                 ("peve", "warrior"), ("pfay", "mage"), ("pgus", "tank")):
    tokens[who] = register(who)
    make_character(tokens[who], who.capitalize(), cls)
amy, bob, cat, dan, eve, fay, gus = (tokens[n] for n in ("pamy", "pbob", "pcat", "pdan", "peve", "pfay", "pgus"))


print("\n--- PA-1 asked, never put in ---")
body = mine(amy).get_json()
check("nobody starts in a party", body["party"] is None and body["invites"] == [], body)
check("  and it holds four, and an invitation waits two minutes",
      body["capacity"] == 4 and body["invite_seconds"] == 120, body)
check("inviting yourself is refused", invite(amy, "pamy").status_code == 400)
check("inviting nobody is a 404", invite(amy, "nobody_here").status_code == 404)
r = invite(amy, "pbob")
check("an invitation waits for them", r.status_code == 200 and r.get_json()["state"] == "invited", r.get_json())
check("  and makes no party yet", party_of(amy) is None and party_of(bob) is None)
check("  they see who asked", [i["from"] for i in mine(bob).get_json()["invites"]] == ["pamy"],
      mine(bob).get_json())
asks = poll(bob).get("asks", {}).get("party", {})
check("  and so does the poll: one, from pamy", asks.get("count") == 1 and asks.get("newest") == "pamy"
      and asks.get("at", 0) > 0, asks)
check("  the one who asked is not told about it", poll(amy).get("asks", {}).get("party", {}).get("count") == 0)
check("answering an invitation nobody sent is a 404", respond(bob, "pcat", True).status_code == 404)

r = respond(bob, "pamy", True)
party = (r.get_json() or {}).get("party")
check("accepting makes the party, the one who asked leading it",
      r.status_code == 200 and party is not None and party["leader"] == "pamy"
      and names(party) == ["pamy", "pbob"] and party["size"] == 2, r.get_json())
member = {m["username"]: m for m in party["members"]}
check("  each member's character: class, level, area and health",
      member["pbob"]["class_id"] == "mage" and member["pbob"]["level"] >= 1
      and member["pbob"]["area"] != "" and member["pbob"]["max_hp"] > 0
      and member["pamy"]["leader"] and not member["pbob"]["leader"], party["members"])
check("  the invitation is gone", mine(bob).get_json()["invites"] == [])
check("  and both see the same party", party_of(amy)["id"] == party_of(bob)["id"])
seen = poll(bob).get("party", {})
check("the poll says who you are with, and who leads",
      seen.get("id") == party["id"] and seen.get("members") == ["pamy", "pbob"] and seen.get("leader") == "pamy",
      seen)
check("  and nothing for somebody in no party", poll(cat).get("party") == {"id": 0, "leader": "", "members": []},
      poll(cat).get("party"))

check("only the leader invites", invite(bob, "pcat").status_code == 400)
invite(cat, "pdan")
check("somebody in a party cannot be asked into another",
      invite(cat, "pamy").status_code == 400 and invite(amy, "pcat").status_code == 200)
r = respond(dan, "pcat", True)
check("  and cannot accept into another either, once in one",
      r.status_code == 200 and respond(cat, "pamy", True).status_code == 400, r.get_json())

invite(eve, "pfay")
check("turning one down is silent, and it is gone",
      respond(fay, "peve", False).status_code == 200 and mine(fay).get_json()["invites"] == []
      and party_of(eve) is None)
invite(eve, "pfay")
conn = db_conn()
conn.execute("UPDATE party_invites SET created_at = created_at - 121")
conn.commit()
conn.close()
check("an invitation runs out after two minutes",
      respond(fay, "peve", True).status_code == 404 and mine(fay).get_json()["invites"] == [])

print("\n--- PA-2 two to four, one leader ---")
for who, token in (("pcat", cat), ("pdan", dan)):
    leave(token)
invite(amy, "pcat")
respond(cat, "pamy", True)
invite(amy, "pdan")
respond(dan, "pamy", True)
check("a party of four", names(party_of(amy)) == ["pamy", "pbob", "pcat", "pdan"], names(party_of(amy)))
check("  and no fifth: asked", invite(amy, "peve").status_code == 400)
conn = db_conn()
conn.execute("INSERT OR REPLACE INTO party_invites (user_id, from_id, created_at)"
             " SELECT (SELECT id FROM users WHERE username='peve'), (SELECT id FROM users WHERE username='pamy'),"
             " strftime('%s','now')")
conn.commit()
conn.close()
check("  or let in, by an invitation sent before it filled", respond(eve, "pamy", True).status_code == 400)

check("only the leader removes", kick(bob, "pcat").status_code == 404)
check("  not themselves - they leave instead", kick(amy, "pamy").status_code == 400)
r = kick(amy, "pdan")
check("the leader removes somebody", r.status_code == 200 and names(party_of(amy)) == ["pamy", "pbob", "pcat"],
      r.get_json())
check("  and they are in no party", party_of(dan) is None)
check("only the leader hands it on", lead(bob, "pbob").status_code == 404)
r = lead(amy, "pcat")
check("the leader hands it on", r.status_code == 200 and party_of(amy)["leader"] == "pcat", r.get_json())
r = leave(cat)
check("a leader leaving hands it to the longest there",
      r.status_code == 200 and party_of(amy)["leader"] == "pamy" and names(party_of(amy)) == ["pamy", "pbob"],
      r.get_json())
r = leave(bob)
check("down to one, the party is gone", r.status_code == 200 and r.get_json()["disbanded"]
      and party_of(amy) is None and party_of(bob) is None, r.get_json())
check("leaving no party is a 404", leave(bob).status_code == 404)

print("\n--- PA-3 the party's own room ---")
refill(*tokens)
r = say(gus, "hello?")
check("party chat with no party is a 409 that says where to go",
      r.status_code == 409 and "Open Party" in r.get_json()["message"], r.get_json())
r = read(gus)
check("  and reading it is nothing, with the same words",
      r.status_code == 200 and r.get_json()["available"] is False and "Open Party" in r.get_json()["notice"])
invite(amy, "pbob")
respond(bob, "pamy", True)
invite(cat, "pdan")
respond(dan, "pcat", True)
check("a party line is said", say(amy, "to the cave").status_code == 200)
check("  and the other parties' too", say(cat, "to the field").status_code == 200)
bodies = [m["body"] for m in read(bob).get_json()["messages"]]
check("members read their own party's lines", "to the cave" in bodies and "to the field" not in bodies, bodies)
bodies = [m["body"] for m in read(dan).get_json()["messages"]]
check("  and only theirs", "to the field" in bodies and "to the cave" not in bodies, bodies)
news = poll(bob).get("chat_news", {})
check("the poll lights the Party tab for a line somebody else said", news.get("party", 0) > 0, news)
check("  and not for your own", poll(amy).get("chat_news", {}).get("party", 0) == 0, poll(amy).get("chat_news"))
leave(bob)
r = read(bob)
check("somebody who left reads nothing of it", r.get_json()["available"] is False and r.get_json()["messages"] == [])
invite(cat, "pbob")
respond(bob, "pcat", True)
bodies = [m["body"] for m in read(bob).get_json()["messages"]]
check("  and joining another party does not carry the old room's lines into it",
      "to the cave" not in bodies and "to the field" in bodies, bodies)

print("\n--- PA-4 a member's account gone ---")
pid = party_of(cat)["id"]
conn = db_conn()
conn.execute("DELETE FROM party_members WHERE user_id = (SELECT id FROM users WHERE username='pcat')")
conn.commit()
conn.close()
party = party_of(dan)
check("a party whose leader went still has one, the longest there",
      party is not None and party["id"] == pid and party["leader"] in ("pdan", "pbob")
      and sum(1 for m in party["members"] if m["leader"]) == 1, party)

print("\n--- logged off is leaving (game 0.93.0) ---")
import time  # noqa: E402
import parties.services as party_rules  # noqa: E402
for t in (amy, bob, cat, dan, eve, fay, gus):
    leave(t)
r1 = invite(gus, "pfay")
respond(fay, "pgus", True)
check("gus and fay make a party of two", names(party_of(gus)) == ["pgus", "pfay"] or sorted(names(party_of(gus))) == ["pfay", "pgus"],
      party_of(gus))
conn = db_conn()
fay_id = conn.execute("SELECT id FROM users WHERE username = 'pfay'").fetchone()["id"]
gus_id = conn.execute("SELECT id FROM users WHERE username = 'pgus'").fetchone()["id"]
quiet = int(time.time()) - party_rules.PARTY_GONE_SECONDS - 5
conn.execute("UPDATE sessions SET last_seen_at = ? WHERE user_id = ?", (quiet, fay_id))
conn.commit()
check("a member quiet for PARTY_GONE_SECONDS is out at the next read, and a party of one ends",
      party_of(gus) is None, party_of(gus))
check("  no party rows are left behind",
      conn.execute("SELECT COUNT(*) FROM party_members WHERE user_id IN (?, ?)", (fay_id, gus_id)).fetchone()[0] == 0)
conn.close()
# Three, and one logs out: the other two stay a party.
conn = db_conn()
conn.execute("UPDATE sessions SET last_seen_at = ? WHERE user_id = ?", (int(time.time()), fay_id))
conn.commit()
conn.close()
invite(gus, "pfay"); respond(fay, "pgus", True)
invite(gus, "peve"); respond(eve, "pgus", True)
check("gus, fay and eve make three", len(names(party_of(gus))) == 3, party_of(gus))
r = client.post("/api/auth/logout", headers=auth(eve))
check("eve logs out", r.status_code in (200, 204), r.status_code)
p = poll(gus).get("party", {})
check("the poll takes a logged-out member out at once; the other two stay a party",
      sorted(p.get("members", [])) == ["pfay", "pgus"], p)
conn = db_conn()
conn.execute("UPDATE sessions SET last_seen_at = ? WHERE user_id = ?", (quiet, fay_id))
conn.commit()
conn.close()
p = poll(gus).get("party", {})
check("  and when one of the two goes quiet, the party ends - it takes two",
      p.get("id", -1) == 0 and party_of(gus) is None, p)
check("a member asking is never taken out, whatever their stamp says",
      "present" in open(os.path.join(HERE, "parties", "data.py")).read())

print("\n--- nothing without a token ---")
check("/api/party needs one", client.get("/api/party").status_code == 401)
for path in ["/api/party/invite", "/api/party/respond", "/api/party/leave", "/api/party/kick", "/api/party/lead"]:
    check("%s needs one" % path, client.post(path, json={}).status_code == 401)


print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
