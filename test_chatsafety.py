"""
Ignore, report and mute - the three tools a busy world chat needs on day 1.
Run: python3 test_chatsafety.py

  S-1  IGNORE is the player's own. An ignored player's lines are left out of
       every channel you read, and their whispers, friend requests and trades
       to you are refused. Staff cannot be ignored.
  S-2  REPORT hands a line to staff. The line is copied into the report, so it
       survives being deleted; staff see one entry per line, with a count, and
       close them together.
  S-3  MUTE is staff's, smaller than a ban: the account plays on and cannot
       speak - in any channel - until the time runs out.

Throwaway database in the temp folder. Never touches elusion.db.
"""
import importlib.util, os, sqlite3, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_chatsafety_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "safetyowner"
os.environ.pop("ELUSION_GAMEDATA", None)
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(_spec)
sys.modules["elusion_app"] = app_module
_spec.loader.exec_module(app_module)
client = app_module.app.test_client()

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

def raw(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        out = conn.execute(sql, args).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()

_ip = [20]
def account(name, role="player"):
    _ip[0] += 1
    tok = client.post("/api/auth/register", json={"username": name, "password": "password123"},
                      environ_base={"REMOTE_ADDR": "203.0.113.%d" % _ip[0]}).get_json()["token"]
    if role != "player":
        raw("UPDATE users SET role = ? WHERE username = ?", (role, name))
    return {"Authorization": "Bearer " + tok}

def refill(*names):
    for name in names:
        raw("UPDATE users SET chat_tokens = ?, last_chat_at = 0 WHERE username = ?",
            (app_module.CHAT_BUCKET_CAPACITY, name))

def say(h, body, channel="world", to=""):
    payload = {"body": body}
    if channel != "world":
        payload["channel"] = channel
    if to:
        payload["to"] = to
    return client.post("/api/chat/send", headers=h, json=payload)

def read(h, channel="world", with_name=""):
    path = "/api/chat?channel=%s" % channel
    if with_name:
        path += "&with=" + with_name
    return client.get(path, headers=h).get_json() or {}

def bodies(answer):
    return [m.get("body") for m in answer.get("messages", [])]

def line_id(h, body, channel="world", with_name=""):
    for m in read(h, channel, with_name).get("messages", []):
        if m.get("body") == body:
            return int(m["id"])
    return 0

def friends(a_name, a, b_name, b):
    client.post("/api/friends/request", headers=a, json={"username": b_name})
    client.post("/api/friends/respond", headers=b, json={"username": a_name, "accept": True})

OWNER = account("safetyowner")
MOD = account("safemod", "mod")
MOD2 = account("othermod", "mod")
DEV = account("safedev", "dev")
ANN = account("ann")
BOB = account("bob")
CAT = account("cat")
names = ("ann", "bob", "cat", "safemod", "othermod", "safedev", "safetyowner")


# =============================================================================
section("S-1  ignore")
# =============================================================================
refill(*names)
say(BOB, "bob before being ignored")
say(BOB, "an old whisper from bob", "private", "ann")
r = client.post("/api/ignores", headers=ANN, json={"username": "bob"})
check("ann ignores bob", r.status_code == 200 and r.get_json().get("ignored") is True, r.get_json())
r = client.post("/api/ignores", headers=ANN, json={"username": "BOB"})
check("  asking again is fine, and says so", r.status_code == 200 and r.get_json().get("already") is True, r.get_json())
listed = client.get("/api/ignores", headers=ANN).get_json()
check("  and bob is on her list", [e["username"] for e in listed.get("ignored", [])] == ["bob"], listed)
check("yourself is refused", client.post("/api/ignores", headers=ANN, json={"username": "ann"}).status_code == 400)
check("nobody is a 404", client.post("/api/ignores", headers=ANN, json={"username": "ghost"}).status_code == 404)
r = client.post("/api/ignores", headers=ANN, json={"username": "safemod"})
check("staff cannot be ignored", r.status_code == 403 and "report" in r.get_json().get("message", ""), r.get_json())
check("  nor the owner", client.post("/api/ignores", headers=ANN, json={"username": "safetyowner"}).status_code == 403)

refill(*names)
say(BOB, "bob says hello")
say(CAT, "cat says hello")
seen = bodies(read(ANN))
check("world chat leaves bob out for ann - his old lines too", "bob says hello" not in seen
      and "bob before being ignored" not in seen and "cat says hello" in seen, seen)
check("  and only for ann: cat still reads him", "bob says hello" in bodies(read(CAT)))
check("  and bob still reads ann's world", "cat says hello" in bodies(read(BOB)))
tail = read(ANN)
check("the cursor is the newest line ann can read, not bob's",
      tail.get("latest_id") == line_id(ANN, "cat says hello"), tail.get("latest_id"))

check("a whisper from before the ignore is gone from ann's whisper tab",
      "an old whisper from bob" not in bodies(read(ANN, "private", "bob")), bodies(read(ANN, "private", "bob")))
news = client.get("/api/server/broadcasts", headers=ANN).get_json().get("chat_news", {})
check("  and is not news, nor any whisper from him",
      (news.get("whisper") or {}).get("from") != "bob", news.get("whisper"))
r = say(BOB, "psst", "private", "ann")
check("bob's whisper to ann is refused", r.status_code == 403
      and "not taking whispers" in r.get_json().get("message", ""), r.get_json())
refill(*names)
r = say(ANN, "i can still reach you", "private", "bob")
check("  ann can still whisper bob - ignoring is not being ignored", r.status_code == 200, r.get_json())
r = client.post("/api/friends/request", headers=BOB, json={"username": "ann"})
check("bob cannot ask ann to be friends", r.status_code == 403
      and "friend requests" in r.get_json().get("message", ""), r.get_json())

client.put("/api/save", headers=ANN, json={"slot": 0, "class_id": "warrior", "name": "Ann"})
client.put("/api/save", headers=BOB, json={"slot": 0, "class_id": "warrior", "name": "Bob"})
client.get("/api/server/broadcasts?slot=0", headers=ANN)
client.get("/api/server/broadcasts?slot=0", headers=BOB)
r = client.post("/api/trade/offer", headers=BOB, json={"slot": 0, "username": "ann"})
check("bob cannot open a trade with ann", r.status_code == 403
      and "trades" in r.get_json().get("message", ""), r.get_json())

# The rooms: friends and guild.
friends("ann", ANN, "cat", CAT)
raw("INSERT INTO ignores (user_id, ignored_id, created_at) VALUES"
    " ((SELECT id FROM users WHERE username = 'ann'), (SELECT id FROM users WHERE username = 'cat'), ?)",
    (int(time.time()),))
refill(*names)
say(CAT, "cat to her friends", "friends")
check("an ignored friend's friends-channel line is left out too",
      "cat to her friends" not in bodies(read(ANN, "friends")), bodies(read(ANN, "friends")))
news = client.get("/api/server/broadcasts", headers=ANN).get_json().get("chat_news", {})
check("  and is not news", news.get("friends", 0) < line_id(CAT, "cat to her friends", "friends"), news)
raw("DELETE FROM ignores WHERE ignored_id = (SELECT id FROM users WHERE username = 'cat')")

raw("INSERT INTO guilds (name, folded, founded_by, created_at) VALUES ('Safety', 'safety', 'ann', ?)",
    (int(time.time()),))
for who, rank in (("ann", "leader"), ("bob", "member")):
    raw("INSERT INTO guild_members (guild_id, user_id, rank, joined_at) VALUES"
        " ((SELECT id FROM guilds WHERE name = 'Safety'), (SELECT id FROM users WHERE username = ?), ?, ?)",
        (who, rank, int(time.time())))
refill(*names)
say(BOB, "bob in the guild", "guild")
check("an ignored guildmate's line is left out of the guild channel",
      "bob in the guild" not in bodies(read(ANN, "guild")), bodies(read(ANN, "guild")))

before = len(bodies(read(ANN)))
r = client.post("/api/ignores/remove", headers=ANN, json={"username": "bob"})
check("ann stops ignoring bob", r.status_code == 200 and r.get_json().get("was_ignored") is True, r.get_json())
check("  and his lines are back", "bob says hello" in bodies(read(ANN)) and len(bodies(read(ANN))) > before)
check("  removing somebody not ignored says so",
      client.post("/api/ignores/remove", headers=ANN, json={"username": "bob"}).get_json().get("was_ignored") is False)

app_module.IGNORE_LIMIT = 2
client.post("/api/ignores", headers=ANN, json={"username": "bob"})
client.post("/api/ignores", headers=ANN, json={"username": "cat"})
account("dan")
r = client.post("/api/ignores", headers=ANN, json={"username": "dan"})
check("the list has a ceiling, and says what to do", r.status_code == 409
      and "Stop ignoring somebody first" in r.get_json().get("message", ""), r.get_json())
app_module.IGNORE_LIMIT = 200
client.post("/api/ignores/remove", headers=ANN, json={"username": "bob"})
client.post("/api/ignores/remove", headers=ANN, json={"username": "cat"})
plan = raw("EXPLAIN QUERY PLAN SELECT ignored_id FROM ignores WHERE user_id = ?", (1,))
check("the ignore lookup is the table's own key, not a scan",
      any("PRIMARY KEY" in str(p[-1]) or "USING" in str(p[-1]) for p in plan), plan)


# =============================================================================
section("S-2  report")
# =============================================================================
refill(*names)
say(BOB, "buy gold at scamsite dot com")
bad = line_id(ANN, "buy gold at scamsite dot com")
r = client.post("/api/chat/report", headers=ANN, json={"id": bad, "reason": "spam"})
check("ann reports bob's line", r.status_code == 200 and r.get_json().get("reported") is True, r.get_json())
r = client.post("/api/chat/report", headers=ANN, json={"id": bad, "reason": "spam"})
check("  a second press is not a second report", r.status_code == 200 and r.get_json().get("already") is True)
r = client.post("/api/chat/report", headers=CAT, json={"id": bad, "reason": "cheating"})
check("cat reports the same line", r.status_code == 200, r.get_json())
check("a reason that is not on the list is refused",
      client.post("/api/chat/report", headers=CAT, json={"id": bad, "reason": "vibes"}).status_code == 400)
check("your own line is refused",
      client.post("/api/chat/report", headers=BOB, json={"id": bad, "reason": "spam"}).status_code == 400)
check("a line that does not exist is a 404",
      client.post("/api/chat/report", headers=ANN, json={"id": 999999, "reason": "spam"}).status_code == 404)
refill(*names)
say(BOB, "a secret for cat", "private", "cat")
secret = line_id(CAT, "a secret for cat", "private", "bob")
r = client.post("/api/chat/report", headers=ANN, json={"id": secret, "reason": "spam"})
check("a whisper between two other people is a 404 - you were never shown it", r.status_code == 404, r.status_code)
r = client.post("/api/chat/report", headers=CAT, json={"id": secret, "reason": "harassment"})
check("  but the one it was said to can report it", r.status_code == 200, r.get_json())
refill(*names)
say(CAT, "cat to her friends only", "friends")
among = line_id(ANN, "cat to her friends only", "friends")
check("a friends-channel line is a 404 to somebody who is not the author's friend",
      client.post("/api/chat/report", headers=BOB, json={"id": among, "reason": "spam"}).status_code == 404)
r = client.post("/api/chat/report", headers=ANN, json={"id": among, "reason": "other"})
check("  and reportable by a friend who was shown it", r.status_code == 200, r.get_json())
client.post("/api/staff/reports/resolve", headers=MOD, json={"message_id": among, "outcome": "dismissed"})

rep = client.get("/api/staff/reports", headers=MOD).get_json()
entry = next((e for e in rep.get("reports", []) if e["message_id"] == bad), {})
check("staff see one entry for the line, with both reporters",
      entry.get("reports") == 2 and entry.get("reporters") == ["ann", "cat"], entry)
check("  what was said, who said it, and why it was reported",
      entry.get("body") == "buy gold at scamsite dot com" and entry.get("reported") == "bob"
      and entry.get("reasons") == {"spam": 1, "cheating": 1}, entry)
check("  and the count of lines waiting", rep.get("open") == 2, rep.get("open"))
check("a player cannot read the reports", client.get("/api/staff/reports", headers=ANN).status_code == 404)
poll = client.get("/api/server/broadcasts", headers=MOD).get_json()
check("the poll tells staff how many lines are waiting", poll.get("open_reports") == 2, poll.get("open_reports"))
check("  and tells a player nothing", client.get("/api/server/broadcasts", headers=ANN).get_json().get("open_reports") == 0)

r = client.post("/api/chat/delete", headers=MOD, json={"id": bad})
check("the mod deletes the line", r.status_code == 200, r.get_json())
check("  which answers its reports", client.get("/api/staff/reports", headers=MOD).get_json().get("open") == 1)
gone = next((e for e in client.get("/api/staff/reports?state=all", headers=MOD).get_json()["reports"]
             if e["message_id"] == bad), {})
check("  and the report keeps what was said, with the line gone",
      gone.get("body") == "buy gold at scamsite dot com" and gone.get("line_exists") is False
      and gone.get("outcome") == "deleted", gone)

r = client.post("/api/staff/reports/resolve", headers=MOD, json={"message_id": secret, "outcome": "dismissed"})
check("a mod dismisses the other", r.status_code == 200 and r.get_json().get("closed") == 1, r.get_json())
check("  nothing is left open", client.get("/api/staff/reports", headers=MOD).get_json().get("open") == 0)
check("  and a second close is a 404",
      client.post("/api/staff/reports/resolve", headers=MOD, json={"message_id": secret, "outcome": "dismissed"}).status_code == 404)
kinds = [row[0] for row in raw("SELECT action FROM staff_actions WHERE action IN ('report', 'chat_delete')")]
check("  both are in the moderation log", "report" in kinds and "chat_delete" in kinds, kinds)

refill(*names)
say(MOD2, "a mod being rude")
rude = line_id(ANN, "a mod being rude")
client.post("/api/chat/report", headers=ANN, json={"id": rude, "reason": "harassment"})
entry = next((e for e in client.get("/api/staff/reports", headers=MOD).get_json()["reports"]
              if e["message_id"] == rude), {})
check("a report about a mod is shown to a mod, marked as not theirs to close",
      entry.get("actionable") is False and entry.get("reported_role") == "mod", entry)
check("  and a mod cannot dismiss it",
      client.post("/api/staff/reports/resolve", headers=MOD, json={"message_id": rude, "outcome": "dismissed"}).status_code == 404)
check("  a dev can",
      client.post("/api/staff/reports/resolve", headers=DEV, json={"message_id": rude, "outcome": "actioned"}).status_code == 200)

app_module.REPORTS_PER_HOUR = 1
refill(*names)
say(BOB, "one more line")
say(BOB, "and another")
client.post("/api/chat/report", headers=CAT, json={"id": line_id(CAT, "one more line"), "reason": "spam"})
r = client.post("/api/chat/report", headers=CAT, json={"id": line_id(CAT, "and another"), "reason": "spam"})
check("reports have an hourly ceiling per player", r.status_code == 429, r.status_code)
app_module.REPORTS_PER_HOUR = 20


# =============================================================================
section("S-3  mute")
# =============================================================================
r = client.post("/api/staff/mute", headers=MOD, json={"username": "bob", "minutes": 10, "reason": "spamming world"})
check("a mod mutes bob for ten minutes", r.status_code == 200 and r.get_json().get("muted") is True, r.get_json())
refill(*names)
r = say(BOB, "can anyone hear me")
body = r.get_json() or {}
check("bob cannot speak", r.status_code == 403, r.status_code)
check("  and is told for how long, and why",
      "10 minutes" in body.get("message", "") and "spamming world" in body.get("message", ""), body)
check("  in whispers too", say(BOB, "psst", "private", "cat").status_code == 403)
check("  and in his guild", say(BOB, "guild?", "guild").status_code == 403)
check("his chat read says he is muted, before he types",
      (read(BOB).get("muted") or {}).get("reason") == "spamming world", read(BOB).get("muted"))
check("  and nobody else's does", read(ANN).get("muted") is None)
check("he still plays - a mute is not a ban", client.get("/api/account", headers=BOB).status_code == 200)
view = client.get("/api/staff/user/bob", headers=MOD).get_json()
check("the staff view shows the mute", (view.get("mute") or {}).get("reason") == "spamming world", view.get("mute"))
listing = client.get("/api/staff/users?q=bob", headers=MOD).get_json()
row = next((a for a in listing.get("accounts", []) if a["username"] == "bob"), {})
check("  and so does the list", (row.get("mute") or {}).get("seconds_left", 0) > 0, row.get("mute"))
check("  and his record counts it, beside bans and kicks", (row.get("record") or {}).get("mute") == 1, row.get("record"))

check("a mod cannot mute for more than a day",
      client.post("/api/staff/mute", headers=MOD, json={"username": "cat", "minutes": 1441, "reason": "x"}).status_code == 403)
check("  a dev can", client.post("/api/staff/mute", headers=DEV,
                                 json={"username": "cat", "minutes": 2880, "reason": "x"}).status_code == 200)
check("a reason is required",
      client.post("/api/staff/mute", headers=MOD, json={"username": "ann", "minutes": 5}).status_code == 400)
check("  and a time", client.post("/api/staff/mute", headers=MOD,
                                  json={"username": "ann", "minutes": 0, "reason": "x"}).status_code == 400)
check("a mod cannot mute another mod",
      client.post("/api/staff/mute", headers=MOD, json={"username": "othermod", "minutes": 5, "reason": "x"}).status_code == 404)
check("  or the owner",
      client.post("/api/staff/mute", headers=DEV, json={"username": "safetyowner", "minutes": 5, "reason": "x"}).status_code == 404)
check("a player cannot mute anybody",
      client.post("/api/staff/mute", headers=ANN, json={"username": "cat", "minutes": 5, "reason": "x"}).status_code == 404)

r = client.post("/api/staff/unmute", headers=MOD, json={"username": "bob"})
check("unmuted, bob speaks again", r.status_code == 200 and say(BOB, "i am back").status_code == 200, r.get_json())
raw("UPDATE users SET chat_muted_until = ? WHERE username = 'cat'", (int(time.time()) - 1,))
refill(*names)
check("a mute that has run out is no mute", say(CAT, "time served").status_code == 200)
kinds = [row[0] for row in raw("SELECT action FROM staff_actions WHERE target_name = 'bob'")]
check("mute and unmute are in the moderation log", "mute" in kinds and "unmute" in kinds, kinds)
check("  under names the log's filter knows",
      all(k in app_module.STAFF_ACTION_KINDS for k in ("mute", "unmute", "report")))


# =============================================================================
section("S-4  a card per player, closed by acting, kept 90 days")
# =============================================================================
DAN = account("dorian")
EVE = account("evelyn")
refill(*names, "dorian", "evelyn")
for n in range(6):
    say(BOB, "spam line %d" % n)
say(DAN, "dan says one rude thing")
# DAN'S REPORTS GO IN FIRST, so newest-first would put bob on top - only
# "the most people" puts dan there.
dan_line = line_id(ANN, "dan says one rude thing")
for who in (ANN, CAT, EVE):
    client.post("/api/chat/report", headers=who, json={"id": dan_line, "reason": "harassment"})
raw("UPDATE chat_reports SET created_at = created_at - 60 WHERE reported_name = 'dorian'")
for n in range(6):
    client.post("/api/chat/report", headers=ANN, json={"id": line_id(ANN, "spam line %d" % n), "reason": "spam"})
client.post("/api/chat/report", headers=CAT, json={"id": line_id(CAT, "spam line 5"), "reason": "spam"})

rep = client.get("/api/staff/reports", headers=MOD).get_json()
cards = {c["reported"]: c for c in rep.get("players", [])}
bob_card = cards.get("bob", {})
check("one card per reported player, not per line",
      bob_card.get("line_count") == 6 and len(bob_card.get("lines", [])) == 6, bob_card.get("line_count"))
check("  with how many people, which reasons, and who",
      bob_card.get("people") == 2 and bob_card.get("reasons") == {"spam": 7}
      and bob_card.get("reporters") == ["ann", "cat"], bob_card)
check("  the newest of their lines first, each with its own count",
      [l["body"] for l in bob_card.get("lines", [])][0] == "spam line 5"
      and bob_card["lines"][0]["reports"] == 2, [l.get("body") for l in bob_card.get("lines", [])])
check("worst first: three people about one line outrank one person about six",
      [c["reported"] for c in rep["players"]][:2] == ["dorian", "bob"], [c["reported"] for c in rep["players"]])
check("  the poll's number is players, and the lines are still counted",
      rep.get("open_players") == 2 and rep.get("open") == 7
      and client.get("/api/server/broadcasts", headers=MOD).get_json().get("open_report_players") == 2,
      (rep.get("open_players"), rep.get("open")))
check("  and a player is told nothing",
      client.get("/api/server/broadcasts", headers=ANN).get_json().get("open_report_players") == 0)
check("  the per-line list is still there for older builds",
      len(rep.get("reports", [])) == 7, len(rep.get("reports", [])))
app_module.REPORT_LINES_PER_PLAYER = 4
small = next((c for c in client.get("/api/staff/reports", headers=MOD).get_json()["players"]
              if c["reported"] == "bob"), {})
check("a card shows only the newest few lines, and says how many there are",
      len(small.get("lines", [])) == 4 and small.get("line_count") == 6, small.get("line_count"))
app_module.REPORT_LINES_PER_PLAYER = 10

before_log = raw("SELECT COUNT(*) FROM staff_actions WHERE action = 'report'")[0][0]
r = client.post("/api/staff/reports/resolve", headers=MOD, json={"username": "BOB", "outcome": "dismissed"})
check("dismissing a card closes every line on it", r.status_code == 200 and r.get_json().get("lines") == 6, r.get_json())
after_log = raw("SELECT COUNT(*) FROM staff_actions WHERE action = 'report'")[0][0]
check("  with one line in the log, not six", after_log - before_log == 1, after_log - before_log)
check("  and bob's card is gone",
      "bob" not in [c["reported"] for c in client.get("/api/staff/reports", headers=MOD).get_json()["players"]])
check("  a second press is a 404",
      client.post("/api/staff/reports/resolve", headers=MOD, json={"username": "bob", "outcome": "dismissed"}).status_code == 404)
refill(*names, "dorian", "evelyn")
say(MOD2, "a mod being rude again")
client.post("/api/chat/report", headers=ANN, json={"id": line_id(ANN, "a mod being rude again"), "reason": "harassment"})
check("  and so is a card about somebody a mod cannot act on",
      client.post("/api/staff/reports/resolve", headers=MOD, json={"username": "othermod", "outcome": "dismissed"}).status_code == 404)
check("  which a dev can close",
      client.post("/api/staff/reports/resolve", headers=DEV, json={"username": "othermod", "outcome": "dismissed"}).status_code == 200)
check("  or nobody", client.post("/api/staff/reports/resolve", headers=MOD,
                                 json={"username": "nobody_here", "outcome": "dismissed"}).status_code == 404)

r = client.post("/api/staff/mute", headers=MOD, json={"username": "dorian", "minutes": 60, "reason": "rude"})
check("muting dan closes the reports about him", r.status_code == 200 and r.get_json().get("reports_closed") == 1,
      r.get_json())
check("  as actioned, by who muted him",
      raw("SELECT DISTINCT outcome, resolved_by FROM chat_reports WHERE reported_name = 'dorian'") == [("actioned", "safemod")],
      raw("SELECT DISTINCT outcome, resolved_by FROM chat_reports WHERE reported_name = 'dorian'"))
check("  and the mute's log line says so",
      raw("SELECT detail FROM staff_actions WHERE action = 'mute' AND target_name = 'dorian'")[0][0].endswith(
          "closed 1 reported line"), raw("SELECT detail FROM staff_actions WHERE action = 'mute' AND target_name = 'dorian'"))
check("  the Reports tab is empty", client.get("/api/staff/reports", headers=MOD).get_json().get("players") == [])

client.post("/api/staff/unmute", headers=MOD, json={"username": "dorian"})
refill(*names, "dorian", "evelyn")
say(DAN, "dan again")
client.post("/api/chat/report", headers=EVE, json={"id": line_id(EVE, "dan again"), "reason": "spam"})
r = client.post("/api/staff/kick", headers=MOD, json={"username": "dorian"})
check("a kick closes them too", r.status_code == 200 and r.get_json().get("reports_closed") == 1, r.get_json())
refill(*names, "dorian", "evelyn")
say(EVE, "eve is next")
client.post("/api/chat/report", headers=ANN, json={"id": line_id(ANN, "eve is next"), "reason": "hate"})
r = client.post("/api/staff/ban", headers=MOD, json={"username": "evelyn", "days": 1, "reason": "hate"})
check("  and so does a ban", r.status_code == 200 and r.get_json().get("reports_closed") == 1, r.get_json())
check("a sanction with nothing reported closes nothing, and says nothing about it",
      client.post("/api/staff/kick", headers=MOD, json={"username": "cat"}).get_json().get("reports_closed") == 0
      and not raw("SELECT detail FROM staff_actions WHERE action = 'kick' AND target_name = 'cat'")[0][0].endswith("line"))

refill(*names, "dorian", "evelyn")
say(BOB, "an old open one")
client.post("/api/chat/report", headers=ANN, json={"id": line_id(ANN, "an old open one"), "reason": "other"})
old_close = int(time.time()) - app_module.REPORT_KEEP_SECONDS - 60
raw("UPDATE chat_reports SET resolved_at = ? WHERE reported_name = 'bob' AND resolved_at > 0", (old_close,))
raw("UPDATE chat_reports SET created_at = ? WHERE resolved_at = 0", (old_close,))
kept_open = raw("SELECT COUNT(*) FROM chat_reports WHERE resolved_at = 0")[0][0]
check("(there is an old open report to keep)", kept_open >= 1, kept_open)
refill(*names, "dorian", "evelyn")
say(BOB, "a fresh line to report")
client.post("/api/chat/report", headers=ANN, json={"id": line_id(ANN, "a fresh line to report"), "reason": "spam"})
check("closed reports past 90 days are dropped the next time one is filed",
      raw("SELECT COUNT(*) FROM chat_reports WHERE reported_name = 'bob' AND resolved_at > 0")[0][0] == 0,
      raw("SELECT COUNT(*) FROM chat_reports WHERE reported_name = 'bob' AND resolved_at > 0"))
check("  but an open one is never dropped, however old",
      raw("SELECT COUNT(*) FROM chat_reports WHERE resolved_at = 0")[0][0] == kept_open + 1,
      raw("SELECT COUNT(*) FROM chat_reports WHERE resolved_at = 0"))
check("  and what staff decided is still in the log",
      raw("SELECT COUNT(*) FROM staff_actions WHERE action = 'report' AND target_name = 'bob'")[0][0] >= 1)


print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
