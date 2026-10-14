"""
test_voice.py - party voice's relay, and reporting a player (game 0.43.0).

    venv\\Scripts\\python.exe test_voice.py

Throwaway database in your temp folder, like every other suite here. Never
touches elusion.db, and never calls Cloudflare: the relay is a stand-in.

WHAT THIS IS GUARDING (the owner: "I really really want the option for voice
chat in game for parties", and "clicking over a person should have add friend
inv to guild or party and report button that syncs to our mod/dev/owner"):

  - VOICE IS A PARTY'S, and goes through a relay. Only somebody in a party is
    handed the relay's credentials; they are made on the server from its TURN
    key, which never leaves it; and the game is told to use the relay and
    nothing else, so nobody learns anybody's address.
  - WITHOUT A KEY, VOICE SAYS SO, rather than calling directly.
  - WHAT THE RELAY SAYS IS CHECKED: only STUN and TURN addresses, none on
    port 53, and TURN ones only with their username and password.
  - A REPORT ABOUT A PLAYER reaches staff on the same Reports tab as a
    reported line: on that player's card, counted, closed by the same buttons.
    One per reporter and player while it is open.

Presence's side - who may pass a call's setup to whom - is test_presence.py,
P-11.

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_voice_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "VOICEOWNER"
for key in ("ELUSION_TURN_KEY_ID", "ELUSION_TURN_KEY_TOKEN", "ELUSION_VOICE_DIRECT"):
    os.environ.pop(key, None)

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


owner = register("VOICEOWNER")
ann = register("vann")
bob = register("vbob")
cat = register("vcat")
for tok, name in ((owner, "Boss"), (ann, "Ann"), (bob, "Bob"), (cat, "Cat")):
    make_character(tok, name)

# THE STAND-IN RELAY: what Cloudflare's answer looks like (its docs), and a
# record of every time it was asked.
asked = []
answer = {"servers": None}
CLOUDFLARE_LIKE = [
    {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
    {"urls": ["turn:turn.cloudflare.com:3478?transport=udp", "turn:turn.cloudflare.com:53?transport=udp",
              "turn:turn.cloudflare.com:3478?transport=tcp", "turns:turn.cloudflare.com:5349?transport=tcp",
              "turn:turn.cloudflare.com:80?transport=tcp", "turns:turn.cloudflare.com:443?transport=tcp"],
     "username": "u" * 40, "credential": "c" * 40},
]


def fake_fetch(key_id, token, ttl):
    asked.append((key_id, token, ttl))
    return answer["servers"]


app_module._turn_fetch = fake_fetch

print("\n--- V-1 voice is a party's ---")
res = client.get("/api/party/voice", headers=auth(ann))
check("somebody in no party is told voice is for parties (409)", res.status_code == 409, res.get_json())
check("/api/party/voice needs a login", client.get("/api/party/voice").status_code == 401)
client.post("/api/party/invite", headers=auth(ann), json={"username": "vbob"})
client.post("/api/party/respond", headers=auth(bob), json={"from": "vann", "accept": True})
check("(ann and bob are a party)", client.get("/api/party", headers=auth(bob)).get_json()["party"] is not None)

print("\n--- V-2 without a key, voice says so ---")
res = client.get("/api/party/voice", headers=auth(ann))
check("no TURN key: 503, voice is not set up", res.status_code == 503 and "not set up" in res.get_json()["message"],
      res.get_json())
check("  and the relay was never asked", not asked, asked)
os.environ["ELUSION_TURN_KEY_ID"] = "../evil"
os.environ["ELUSION_TURN_KEY_TOKEN"] = "secret-token"
res = client.get("/api/party/voice", headers=auth(ann))
check("a key id that is not one (a path) counts as none", res.status_code == 503 and not asked, res.get_json())

print("\n--- V-3 with a key, through the relay and nothing else ---")
os.environ["ELUSION_TURN_KEY_ID"] = "0123456789abcdef0123456789abcdef"
answer["servers"] = CLOUDFLARE_LIKE
res = client.get("/api/party/voice", headers=auth(ann))
body = res.get_json() or {}
check("a party member is handed the relay", res.status_code == 200 and body.get("ice_servers"), body)
check("  with the policy 'relay': the game uses the relay and no direct address", body.get("policy") == "relay", body)
check("  the relay was asked with the server's key, for a day",
      asked and asked[-1] == ("0123456789abcdef0123456789abcdef", "secret-token", app_module.VOICE_TURN_TTL), asked)
check("  the key's token is nowhere in the answer", "secret-token" not in res.get_data(as_text=True))
urls = [u for s in body.get("ice_servers", []) for u in s["urls"]]
check("  no address on port 53 (browsers block it)",
      urls and not any(app_module.re.search(r":53(\?|$)", u) for u in urls)
      and any(u.startswith("turns:") and ":443" in u for u in urls), urls)
turn = [s for s in body.get("ice_servers", []) if any(u.startswith("turn") for u in s["urls"])]
check("  the TURN addresses come with their username and password",
      turn and all(s.get("username") and s.get("credential") for s in turn), turn)
check("  and an expiry a day out", body.get("expires_at", 0) - app_module.time.time() > 23 * 3600, body)
before = len(asked)
again = client.get("/api/party/voice", headers=auth(ann)).get_json()
check("asking again within hours hands the same credentials, without asking the relay",
      len(asked) == before and again.get("ice_servers") == body.get("ice_servers"), (len(asked), before))
res = client.get("/api/party/voice", headers=auth(bob))
check("the other member gets their own", res.status_code == 200 and len(asked) == before + 1, len(asked))

print("\n--- V-4 what the relay says is checked ---")
clean = app_module.clean_ice_servers
check("an entry without a password for its TURN address is dropped",
      clean([{"urls": ["turn:turn.example.com:3478"]}]) == [], clean([{"urls": ["turn:turn.example.com:3478"]}]))
check("  a STUN entry needs none", clean([{"urls": "stun:stun.example.com:3478"}]) == [
    {"urls": ["stun:stun.example.com:3478"]}])
check("  an address that is not STUN or TURN is dropped",
      clean([{"urls": ["javascript:alert(1)", "http://x.example.com"]}]) == [])
check("  as is an address carrying anything else",
      clean([{"urls": ["turn:x.example.com:3478?transport=udp&evil=1"], "username": "a", "credential": "b"}]) == [])
check("  and an answer that is not a list or entry at all", clean("nope") == [] and clean(None) == [])
answer["servers"] = [{"urls": ["stun:stun.cloudflare.com:3478"]}]
res = client.get("/api/party/voice", headers=auth(cat))
check("(cat is in no party)", res.status_code == 409)
client.post("/api/party/invite", headers=auth(ann), json={"username": "vcat"})
client.post("/api/party/respond", headers=auth(cat), json={"from": "vann", "accept": True})
res = client.get("/api/party/voice", headers=auth(cat))
check("a relay answer with no TURN in it is a 502, not a direct call", res.status_code == 502, res.get_json())
answer["servers"] = None
app_module._voice_issued.clear()
res = client.get("/api/party/voice", headers=auth(cat))
check("  as is no answer at all", res.status_code == 502, res.get_json())

print("\n--- V-5 asked too often ---")
answer["servers"] = CLOUDFLARE_LIKE
app_module._voice_issued.clear()
app_module._voice_asks.clear()
codes = []
for _ in range(app_module.VOICE_FETCHES_PER_HOUR + 1):
    app_module._voice_issued.clear()
    codes.append(client.get("/api/party/voice", headers=auth(cat)).status_code)
check("the relay is asked at most VOICE_FETCHES_PER_HOUR times an hour for one player",
      codes[:-1] == [200] * app_module.VOICE_FETCHES_PER_HOUR and codes[-1] == 429, codes)

print("\n--- V-6 one computer, no relay: only when the server says so ---")
os.environ["ELUSION_VOICE_DIRECT"] = "on"
res = client.get("/api/party/voice", headers=auth(ann)).get_json()
check("ELUSION_VOICE_DIRECT=on: no relay and every address allowed (a test on one computer)",
      res.get("ice_servers") == [] and res.get("policy") == "all", res)
os.environ.pop("ELUSION_VOICE_DIRECT")
res = client.get("/api/party/voice", headers=auth(ann)).get_json()
check("  and off again, the relay", res.get("policy") == "relay", res)

print("\n--- V-7 reporting a player reaches staff ---")
res = client.post("/api/report/player", headers=auth(ann), json={"username": "vbob", "reason": "harassment",
                                                                   "area": "big_field"})
check("a player reports another", res.status_code == 200 and res.get_json().get("reported"), res.get_json())
res = client.post("/api/report/player", headers=auth(ann), json={"username": "vbob", "reason": "spam"})
check("  a second press while it is open is not a second report",
      res.status_code == 200 and res.get_json().get("already"), res.get_json())
res = client.post("/api/report/player", headers=auth(cat), json={"username": "vbob", "reason": "cheating"})
check("  somebody else's report about them is their own", res.status_code == 200 and not res.get_json().get("already"))
staff = client.get("/api/staff/reports", headers=auth(owner)).get_json()
card = next((c for c in staff.get("players", []) if c["reported"] == "vbob"), None)
check("staff see one card for the player, two people reporting",
      card is not None and card["people"] == 2 and card["reports"] == 2, card)
check("  each report a line on it: 'player', where they were seen, and the reason",
      card and {l["channel"] for l in card["lines"]} == {"player"}
      and any(l["body"] == "Reported in big field" for l in card["lines"])
      and any(l["body"] == "Reported in the world" for l in card["lines"])
      and card["reasons"] == {"harassment": 1, "cheating": 1}, card)
check("  never as a chat line that exists, so there is nothing to delete",
      card and not any(l["line_exists"] for l in card["lines"]), card)
check("  counted in the open-reports badge", staff.get("open_players", 0) >= 1 and staff.get("open", 0) >= 2, staff)
line = card["lines"][0]
res = client.post("/api/staff/reports/resolve", headers=auth(owner),
                  json={"message_id": line["message_id"], "outcome": "dismissed"})
check("one of them closes like a line", res.status_code == 200 and res.get_json()["closed"] == 1, res.get_json())
res = client.post("/api/staff/reports/resolve", headers=auth(owner), json={"username": "vbob", "outcome": "actioned"})
check("  the card's own close takes the rest", res.status_code == 200, res.get_json())
left_open = client.get("/api/staff/reports", headers=auth(owner)).get_json()
check("  and nothing about them is left open",
      not any(c["reported"] == "vbob" for c in left_open.get("players", [])), left_open)
res = client.post("/api/report/player", headers=auth(ann), json={"username": "vbob", "reason": "hate"})
check("reporting them again once it was closed opens it again",
      res.status_code == 200 and not res.get_json().get("already"), res.get_json())
again = client.get("/api/staff/reports", headers=auth(owner)).get_json()
card = next((c for c in again.get("players", []) if c["reported"] == "vbob"), None)
check("  with the new reason", card and card["reasons"] == {"hate": 1}, card)

print("\n--- V-8 what a report may not be ---")
for body, code, label in (
        ({"username": "vbob", "reason": "ugly"}, 400, "a reason that is not one"),
        ({"username": "vann", "reason": "spam"}, 400, "yourself"),
        ({"username": "nobody_here", "reason": "spam"}, 404, "nobody"),
        ({"username": "x" * 65, "reason": "spam"}, 404, "a name longer than any")):
    res = client.post("/api/report/player", headers=auth(ann), json=body)
    check("refused: %s (%d)" % (label, code), res.status_code == code, (res.status_code, res.get_json()))
res = client.post("/api/report/player", headers=auth(cat), json={"username": "vann", "reason": "spam",
                                                                   "area": "../../etc"})
conn = db_conn()
row = conn.execute("SELECT body FROM chat_reports WHERE reported_name = 'vann' AND channel = 'player'").fetchone()
conn.close()
check("an area that is not an area id is not written down - 'in the world'",
      row is not None and row["body"] == "Reported in the world", dict(row) if row else None)
check("a report needs a login", client.post("/api/report/player", json={}).status_code == 401)
conn = db_conn()
conn.execute("DELETE FROM chat_reports")
conn.commit()
conn.close()
codes = [client.post("/api/report/player", headers=auth(ann),
                     json={"username": n, "reason": "spam"}).status_code
         for n in ["vbob", "vcat", "VOICEOWNER"]]
conn = db_conn()
conn.execute("UPDATE chat_reports SET created_at = strftime('%s','now')")
for i in range(app_module.REPORTS_PER_HOUR):
    conn.execute("INSERT INTO chat_reports (message_id, reporter_id, reporter_name, reported_id, reported_name,"
                 " channel, body, reason, created_at) VALUES (?, (SELECT id FROM users WHERE username='vann'),"
                 " 'vann', 1, 'x', 'world', 'b', 'spam', strftime('%s','now'))", (900000 + i,))
conn.commit()
conn.close()
res = client.post("/api/report/player", headers=auth(ann), json={"username": "vcat", "reason": "hate"})
check("the hour's limit is the chat reports' own, shared", codes == [200, 200, 200] and res.status_code in (200, 429),
      (codes, res.status_code))
conn = db_conn()
conn.execute("DELETE FROM chat_reports WHERE reporter_id = (SELECT id FROM users WHERE username='vann')"
             " AND channel = 'player'")
conn.commit()
conn.close()
res = client.post("/api/report/player", headers=auth(ann), json={"username": "vcat", "reason": "hate"})
check("  over it, a new report is a 429", res.status_code == 429, (res.status_code, res.get_json()))


print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
