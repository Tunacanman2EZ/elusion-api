"""The world's events (game 0.48.0): double XP, more loot, a land made to stir.

The owner's plan: "Treat events (double XP, double loot, forced stir, etc.)
as their own feature area with a clean public contract". Built like
playerstats/, so each layer is held on its own:

  E-1  services.py, the rules: what an event may be, its name and its words,
       which are on and which are next, what a kill gets (the bigger of two,
       never the product), which land a stir event makes stir
  E-2  data.py, the SQL: the table it owns, written and read, a call-off and
       an announcement that happen once
  E-3  routes.py, the edge: the contract any player reads; the owner's list,
       scheduling and calling off - the owner's alone, the password again
  E-4  the world: on every poll, said once when one begins, paid at the kill,
       a stir event's land stirring until it ends (or is called off), and an
       events table that cannot be read costing nobody a kill

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_events.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_events_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "eventsowner"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()
gamedata = app_module.gamedata

from events import data, services  # noqa: E402

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
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


LANDS = app_module.LANDS
HOUR = services.HOUR
NOW = 1_800_000_000


def clean(raw, limit):
    return app_module.clean_player_text(raw, limit)


def ev(eid, kind, amount=2.0, land=0, starts=NOW - HOUR, ends=NOW + HOUR, cancelled=0, name="E"):
    return {"id": eid, "name": name, "kind": kind, "amount": amount, "land": land, "starts_at": starts,
            "ends_at": ends, "cancelled_at": cancelled, "announced": 0, "created_by": "x"}


# =============================================================================
section("E-1  services.py - the rules")
good, why = services.validate({"kind": "xp", "amount": 2, "starts_at": NOW + 600, "hours": 3}, NOW, LANDS, clean)
check("double XP for every monster, in ten minutes, for three hours: named for what it does",
      why is None and good == {"kind": "xp", "amount": 2.0, "land": 0, "starts_at": NOW + 600,
                               "ends_at": NOW + 600 + 3 * HOUR, "name": "Double XP"}, (good, why))
good, why = services.validate({"kind": "stir", "land": 6, "starts_at": NOW - 120, "hours": 24,
                               "name": "  Fire\nweekend\u0007 "}, NOW, LANDS, clean)
check("a stir of the fire lands starting two minutes ago starts now, and a typed name is cleaned",
      why is None and good["starts_at"] == NOW and good["land"] == 6 and good["name"] == "Fire weekend"
      and good["amount"] == 1.0, (good, why))
refused = []
for body, what in [
        (None, "nothing"), ({"kind": "gold", "starts_at": NOW, "hours": 1}, "a kind it does not have"),
        ({"kind": "xp", "amount": 5, "starts_at": NOW, "hours": 1}, "5x"),
        ({"kind": "xp", "amount": 1.1, "starts_at": NOW, "hours": 1}, "1.1x"),
        ({"kind": "xp", "amount": True, "starts_at": NOW, "hours": 1}, "a bool for an amount"),
        ({"kind": "loot", "amount": 2, "land": 9, "starts_at": NOW, "hours": 1}, "land 9"),
        ({"kind": "stir", "land": 0, "starts_at": NOW, "hours": 1}, "a stir of no land"),
        ({"kind": "stir", "starts_at": NOW, "hours": 1}, "a stir with no land"),
        ({"kind": "xp", "amount": 2, "starts_at": NOW - 3600, "hours": 1}, "an hour ago"),
        ({"kind": "xp", "amount": 2, "starts_at": NOW + 61 * 86400, "hours": 1}, "61 days ahead"),
        ({"kind": "xp", "amount": 2, "starts_at": NOW, "hours": 0}, "0 hours"),
        ({"kind": "xp", "amount": 2, "starts_at": NOW, "hours": 73}, "73 hours"),
        ({"kind": "xp", "amount": 2, "starts_at": NOW, "hours": 2.5}, "2.5 hours"),
        ({"kind": "xp", "amount": 2, "starts_at": "tomorrow", "hours": 2}, "a word for a time")]:
    event, why = services.validate(body, NOW, LANDS, clean)
    if event is not None or not why:
        refused.append(what)
check("refused, each with a reason: %d ways to be wrong" % 14, refused == [], refused)
check("names and words from the numbers",
      services.default_name("loot", 3.0, 0, LANDS) == "Triple Loot"
      and services.default_name("xp", 1.5, 0, LANDS) == "1.5x XP"
      and services.default_name("stir", 1.0, 6, LANDS) == "The Fire Lands Stir"
      and services.describe(ev(1, "xp", 2.0, 0), LANDS) == "2x XP from every monster."
      and services.describe(ev(1, "loot", 1.5, 6), LANDS) == "Loot bags drop 1.5x as often from the fire lands' monsters."
      and services.describe(ev(1, "xp", 2.0, -1), LANDS) == "2x XP from the stirring land's monsters."
      and services.describe(ev(1, "stir", 1.0, 3), LANDS) == "The ice lands stir for the whole event.")
rows = [ev(1, "xp", 2.0), ev(2, "loot", 1.5, starts=NOW + HOUR, ends=NOW + 2 * HOUR),
        ev(3, "xp", 3.0, cancelled=NOW - 5), ev(4, "stir", 1.0, 6, starts=NOW + 8 * 86400, ends=NOW + 9 * 86400)]
body = services.active_and_next(rows, NOW, LANDS)
check("the contract: what is on (not one called off), what is next within a week, and the server's clock",
      [e["id"] for e in body["events"]] == [1] and [e["id"] for e in body["next"]] == [2] and body["now"] == NOW
      and body["events"][0] == {"id": 1, "name": "E", "kind": "xp", "description": "2x XP from every monster.",
                                "starts_at": NOW - HOUR, "ends_at": NOW + HOUR, "land": 0, "multipliers": {"xp": 2.0}},
      body)
# Seven coming, written in no order, ids backwards to when they start.
coming = [ev(10 - k, "xp", 2.0, starts=NOW + (3 + (k * 5) % 7) * HOUR, ends=NOW + 20 * HOUR) for k in range(7)]
body = services.active_and_next(coming, NOW, LANDS)
check("next: the nearest first, five at most",
      [e["starts_at"] for e in body["next"]] == sorted(e["starts_at"] for e in coming)[:5],
      [e["starts_at"] - NOW for e in body["next"]])
scales = [
    services.kill_scales([ev(1, "xp", 2.0), ev(2, "xp", 1.5)], NOW, 6, 0),
    services.kill_scales([ev(1, "loot", 2.0, 6)], NOW, 6, 0),
    services.kill_scales([ev(1, "loot", 2.0, 6)], NOW, 7, 0),
    services.kill_scales([ev(1, "xp", 2.0, -1)], NOW, 3, 3),
    services.kill_scales([ev(1, "xp", 2.0, -1)], NOW, 3, 0),
    services.kill_scales([ev(1, "xp", 2.0, starts=NOW + 10)], NOW, 6, 0),
]
check("what a kill gets: the bigger of two XP events (not 3x); a land's own; the stirring land's; nothing before it starts",
      scales == [(1.0, 2.0), (2.0, 1.0), (1.0, 1.0), (1.0, 2.0), (1.0, 1.0), (1.0, 1.0)], scales)
check("a stir event: the one that began last, if two overlap; none when none is on",
      services.forced_stir([ev(1, "stir", 1.0, 6, starts=NOW - 50), ev(2, "stir", 1.0, 3, starts=NOW - 10)], NOW)["id"] == 2
      and services.forced_stir([ev(1, "xp")], NOW) is None)
check("time in words", services.span_words(3 * HOUR) == "3 hours" and services.span_words(HOUR) == "1 hour"
      and services.span_words(2 * 86400 + 4 * HOUR) == "2 days 4 hours" and services.span_words(45 * 60) == "45 minutes")

# =============================================================================
section("E-2  data.py - the SQL")
mem = sqlite3.connect(":memory:")
mem.row_factory = sqlite3.Row
check("a database without the table answers with nothing", data.live(mem, NOW, 3600) == []
      and data.schedule(mem, 0) == [] and data.get(mem, 1) is None and data.count_upcoming(mem, NOW) == 0
      and data.mark_announced(mem, 1) is False)
data.ensure_tables(mem)
data.ensure_tables(mem)
eid = data.insert(mem, {"name": "Double XP", "kind": "xp", "amount": 2.0, "land": 0, "starts_at": NOW,
                        "ends_at": NOW + HOUR}, "boss", NOW)
later = data.insert(mem, {"name": "Later", "kind": "loot", "amount": 1.5, "land": 0, "starts_at": NOW + 30 * 86400,
                          "ends_at": NOW + 30 * 86400 + HOUR}, "boss", NOW)
check("the package makes its own table, twice safely, and writes an event", eid == 1
      and data.get(mem, eid)["name"] == "Double XP" and data.get(mem, eid)["created_by"] == "boss")
check("live: on now and within the window ahead, not beyond", [e["id"] for e in data.live(mem, NOW, 86400)] == [eid]
      and [e["id"] for e in data.live(mem, NOW, 31 * 86400)] == [eid, later])
check("announced once: the first caller, never the second", data.mark_announced(mem, eid) and not data.mark_announced(mem, eid))
check("called off once, kept in the schedule, gone from what is live",
      data.cancel(mem, later, "boss", NOW) and not data.cancel(mem, later, "boss", NOW)
      and [e["id"] for e in data.live(mem, NOW, 31 * 86400)] == [eid]
      and {e["id"] for e in data.schedule(mem, 0)} == {eid, later} and data.get(mem, later)["cancelled_by"] == "boss")
check("an event already over cannot be called off", not data.cancel(mem, eid, "boss", NOW + 2 * HOUR))

# =============================================================================
section("E-3  routes.py - the edge")
owner = register("eventsowner")
player = register("eventsplayer")
devtok = register("eventsdev")
sql("UPDATE users SET role = 'dev' WHERE username = 'eventsdev'")
client.put("/api/save", headers=auth(player), json={"slot": 0, "class_id": "warrior", "name": "P"})
player_id = int(sql("SELECT id FROM users WHERE username = 'eventsplayer'")[0]["id"])
now = int(time.time())
res = client.get("/api/world/events/active", headers=auth(player))
check("any player reads what is on and what is next", res.status_code == 200
      and res.get_json()["events"] == [] and res.get_json()["next"] == [] and "now" in res.get_json(), res.get_json())
check("  and nobody without a login", client.get("/api/world/events/active").status_code == 401)
# Labels written out whole, not formatted: SECURITY.md quotes them.
for label, token in (("a player gets the same bare 404 from the owner's three", player),
                     ("a dev gets the same bare 404 from the owner's three", devtok)):
    hidden = [client.get("/api/world/events/schedule", headers=auth(token)).status_code,
              client.post("/api/world/events", headers=auth(token), json={"kind": "xp", "amount": 2,
                                                                         "starts_at": now, "hours": 1}).status_code,
              client.post("/api/world/events/cancel", headers=auth(token), json={"id": 1}).status_code]
    check(label, hidden == [404, 404, 404], hidden)
views = app_module.app.view_functions
check("the owner's two that change the world ask for the password again; reading does not",
      getattr(views["schedule_event"], "_elusion_fresh_password", False)
      and getattr(views["cancel_event"], "_elusion_fresh_password", False)
      and not getattr(views["read_event_schedule"], "_elusion_fresh_password", False)
      and getattr(views["read_event_schedule"], "_elusion_min_role", None) == "coowner")
sql("DELETE FROM broadcasts")
res = client.post("/api/world/events", headers=auth(owner),
                  json={"kind": "xp", "amount": 2, "starts_at": now + 2 * 86400 + 4 * HOUR + 30, "hours": 3})
body = res.get_json() or {}
said = [r["body"] for r in sql("SELECT body FROM broadcasts WHERE kind = 'event'")]
check("the owner schedules one: 201 and the event as the contract has it",
      res.status_code == 201 and body["event"]["name"] == "Double XP" and body["event"]["multipliers"] == {"xp": 2.0}, body)
check("  and everyone is told it is coming", said == ["Coming up: Double XP, in 2 days 4 hours, for 3 hours - "
                                                     "2x XP from every monster."], said)
logged = sql("SELECT action, detail FROM staff_actions WHERE action = 'event'")
check("  and it is in the staff log as an event", len(logged) == 1 and "Double XP" in logged[0]["detail"],
      [dict(r) for r in logged])
coming_id = body["event"]["id"]
res = client.post("/api/world/events", headers=auth(owner), json={"kind": "xp", "amount": 9, "starts_at": now, "hours": 1})
check("a bad one is a 400 that says why", res.status_code == 400 and "amount" in res.get_json()["message"], res.get_json())
listed = client.get("/api/world/events/schedule", headers=auth(owner)).get_json()
check("the owner's list: the event, coming, with who set it and the limits",
      listed["events"][0]["id"] == coming_id and listed["events"][0]["state"] == "coming"
      and listed["events"][0]["created_by"] == "eventsowner" and listed["limits"]["hours"] == [1, 72], listed)
sql("DELETE FROM broadcasts")
res = client.post("/api/world/events/cancel", headers=auth(owner), json={"id": coming_id})
said = [r["body"] for r in sql("SELECT body FROM broadcasts WHERE kind = 'event'")]
check("called off: said so", res.status_code == 200 and said == ["Double XP will not happen after all."], said)
check("  twice is a 404, and no id is a 400",
      client.post("/api/world/events/cancel", headers=auth(owner), json={"id": coming_id}).status_code == 404
      and client.post("/api/world/events/cancel", headers=auth(owner), json={}).status_code == 400)
for i in range(services.MOST_WAITING):
    sql("INSERT INTO world_events (name, kind, amount, land, starts_at, ends_at, created_at) "
        "VALUES ('W', 'loot', 1.5, 0, ?, ?, ?)", (now + 86400 * 10 + i, now + 86400 * 10 + i + HOUR, now))
res = client.post("/api/world/events", headers=auth(owner), json={"kind": "xp", "amount": 2, "starts_at": now, "hours": 1})
check("with %d already waiting, a 409 until one is called off" % services.MOST_WAITING, res.status_code == 409,
      res.status_code)
sql("DELETE FROM world_events")

# =============================================================================
section("E-4  the world")
BY_LAND = {}
for enemy_id, enemy in sorted(gamedata.ENEMIES.items()):
    element = int(enemy.get("element", 0) or 0)
    if element in LANDS and enemy.get("grants_rewards") and 0 < float(enemy.get("bag_drop_chance", 0)) < 1:
        BY_LAND.setdefault(element, enemy_id)


def poll(token):
    return client.get("/api/server/broadcasts?since=0", headers=auth(token)).get_json()


sql("DELETE FROM broadcasts")
res = client.post("/api/world/events", headers=auth(owner), json={"kind": "loot", "amount": 2, "land": 0,
                                                                  "starts_at": now, "hours": 2})
loot_id = res.get_json()["event"]["id"]
first = poll(player)
second = poll(player)
begun = [r["body"] for r in sql("SELECT body FROM broadcasts WHERE kind = 'event'")]
check("the poll carries the events: Double Loot on, with its time window and multipliers",
      [e["id"] for e in first["events"]["events"]] == [loot_id]
      and first["events"]["events"][0]["multipliers"] == {"loot": 2.0}
      and first["events"]["events"][0]["ends_at"] - first["events"]["events"][0]["starts_at"] == 2 * HOUR,
      first.get("events"))
check("  and the first to ask after it began says so to everyone - once, however many ask",
      begun == ["Double Loot has begun, for 2 hours: Loot bags drop 2x as often from every monster."], begun)

captured = []
real_roll = gamedata.roll_kill_rewards


def spy(enemy_id, class_id="", bag_scale=1.0, xp_scale=1.0):
    captured.append((bag_scale, xp_scale))
    return real_roll(enemy_id, class_id, bag_scale, xp_scale)


def kill(enemy_id):
    sql("UPDATE saves SET kill_tokens = 50, last_kill_at = 0 WHERE user_id = ?", (player_id,))
    return client.post("/api/combat/kill", headers=auth(player), json={"slot": 0, "enemy_id": enemy_id})


land = poll(player)["world"]["element"]
other = next(e for e in LANDS if e != land)
gamedata.roll_kill_rewards = spy
try:
    kill(BY_LAND[other])
    kill(BY_LAND[land])
    client.post("/api/world/events", headers=auth(owner), json={"kind": "xp", "amount": 1.5, "land": other,
                                                               "starts_at": now, "hours": 1})
    kill(BY_LAND[other])
finally:
    gamedata.roll_kill_rewards = real_roll
check("a kill while Double Loot is on: twice the bag chance", captured[0] == (2.0, 1.0), captured)
check("  times the stirring land's own for its monsters (1.5 x 2 = 3)", captured[1] == (3.0, 1.0), captured)
check("  and an XP event of one land on top: 2x loot, 1.5x XP", captured[2] == (2.0, 1.5), captured)
sql("UPDATE world_events SET cancelled_at = ? WHERE cancelled_at = 0", (now,))

stir_land = other
before_tick = poll(player)["world"]["tick"]
res = client.post("/api/world/events", headers=auth(owner), json={"kind": "stir", "land": stir_land,
                                                                  "starts_at": now, "hours": 5})
stir_id = res.get_json()["event"]["id"]
world = poll(player)["world"]
ticks = [dict(r) for r in sql("SELECT * FROM world_ticks ORDER BY tick")]
check("a stir event: its land stirs now, for the whole event, as a new stir",
      world["element"] == stir_land and world["tick"] == before_tick + 1
      and world["ends_at"] == res.get_json()["event"]["ends_at"] and ticks[-1]["chosen_by"] == "event #%d" % stir_id,
      (world, ticks[-1]))
check("  and the stir that was going on ended when it began", ticks[-2]["ends_at"] <= int(time.time()), ticks[-2])
again = poll(player)["world"]
check("  asked again, the same stir - not a new one each poll", again["tick"] == world["tick"])
client.post("/api/world/events/cancel", headers=auth(owner), json={"id": stir_id})
after = poll(player)["world"]
check("called off, its stir ends now and the world's own rhythm carries on",
      after["tick"] == world["tick"] + 1 and after["ends_at"] - after["started_at"] == 3 * HOUR, after)

sql("DROP TABLE world_events")
res = kill(BY_LAND[land])
check("an events table that cannot be read: the kill is paid, the poll answers, nothing is on",
      res.status_code == 200 and poll(player)["events"]["events"] == [], res.status_code)


def broken(*_args, **_kwargs):
    raise ValueError("a rule that breaks")


kept = (services.kill_scales, services.active_and_next, services.forced_stir)
services.kill_scales = services.active_and_next = services.forced_stir = broken
try:
    res = kill(BY_LAND[land])
    body = poll(player)
finally:
    services.kill_scales, services.active_and_next, services.forced_stir = kept
check("  and a rule that throws, not just a missing table: the same - and the land still stirs",
      res.status_code == 200 and body["events"]["events"] == [] and body["world"]["element"] != 0,
      (res.status_code, body.get("world")))

print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
