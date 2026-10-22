"""The world's events (game 0.48.0, 0.49.0): double XP, more loot, a land made to stir.

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
  E-5  the guardrails (0.49.0): never too much at once for any land's
       monsters, refused when set and held to the ceilings when paid
  E-6  the dry run: allowed or not and why, the most each land pays, and
       what 20 players might earn, from the last two weeks' play
  E-7  the event report: before, during and after each event, who was
       online (counted once a minute), and which kind brings players

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_events.py
"""
import importlib.util
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_events_test.db")
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
        ({"kind": "xp", "amount": 2, "starts_at": NOW + 367 * 86400, "hours": 1}, "367 days ahead"),
        ({"kind": "xp", "amount": 2, "starts_at": NOW, "hours": 0}, "0 hours"),
        ({"kind": "xp", "amount": 2, "starts_at": NOW, "hours": 73}, "73 hours"),
        ({"kind": "xp", "amount": 2, "starts_at": NOW, "hours": 2.5}, "2.5 hours"),
        ({"kind": "xp", "amount": 2, "starts_at": "tomorrow", "hours": 2}, "a word for a time")]:
    event, why = services.validate(body, NOW, LANDS, clean)
    if event is not None or not why:
        refused.append(what)
check("refused, each with a reason: %d ways to be wrong" % 14, refused == [], refused)
year_on, why = services.validate({"kind": "xp", "amount": 2, "starts_at": NOW + 365 * 86400 + 3600, "hours": 3}, NOW, LANDS,
                                 clean)
check("a year ahead is taken (game 0.52.0, the calendar's whole year): the same date next year, an hour in",
      why is None and year_on["starts_at"] == NOW + 365 * 86400 + 3600, why)
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
      and listed["events"][0]["created_by"] == "eventsowner" and listed["limits"]["hours"] == [1, 72]
      and listed["limits"]["days_ahead"] == 366, listed)
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


def spy(enemy_id, class_id="", bag_scale=1.0, xp_scale=1.0, material_drop_scale=1.0):
    # material_drop_scale (0.74.0) is the owner's economy number, not an
    # event's: passed through as the kill route sent it.
    captured.append((bag_scale, xp_scale))
    return real_roll(enemy_id, class_id, bag_scale, xp_scale, material_drop_scale=material_drop_scale)


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

# =============================================================================
section("E-5  the guardrails - never too much at once")
# The owner's: "don't allow 3x XP + 3x loot + 3x gold all at once in the same
# area". Gold comes out of loot bags here, so loot is the gold.
conn = sqlite3.connect(DB_PATH)
data.ensure_tables(conn)
conn.commit()
conn.close()
STIR = {"on": True, "xp": 1.0, "bags": 1.5}


def guarded(candidate, others=(), stir=None):
    return services.guard(candidate, list(others), STIR if stir is None else stir, LANDS)[0]


double_xp = ev(None, "xp", 2.0, name="Double XP")
double_loot = ev(2, "loot", 2.0, name="Double Loot")
triple_xp = ev(None, "xp", 3.0, name="Triple XP")
check("Double XP and Double Loot together on every monster: allowed - 2x XP and 3x loot with the stirring land's own",
      guarded(double_xp, [double_loot]) is None)
why = guarded(triple_xp, [double_loot]) or ""
check("Triple XP over Double Loot: refused, saying whose monsters, with what, and the ceilings",
      why.startswith("Too much at once for the stirring land's monsters: 3x XP and 3x loot (9x together), with Double Loot, "
                     "the stirring land's own 1.5x loot - the most a kill can be is 3x XP, 3x loot and 6x the two together."),
      why)
check("  Triple Loot alone: allowed with the stirring lands' loot off, refused with it on (4.5x)",
      guarded(ev(None, "loot", 3.0), [], {"on": True, "xp": 1.0, "bags": 1.0}) is None
      and "4.5x loot" in (guarded(ev(None, "loot", 3.0)) or ""))
check("  3x XP for the fire lands and 3x loot for the ice lands do not meet",
      guarded(ev(None, "xp", 3.0, land=6), [ev(2, "loot", 3.0, land=3)], {"on": False}) is None)
check("  on one land they do, and the land is named",
      (guarded(ev(None, "xp", 3.0, land=6), [ev(2, "loot", 2.5, land=6, name="Fire Loot")], {"on": False}) or "")
      .startswith("Too much at once for the fire lands' monsters: 3x XP and 2.5x loot (7.5x together), with Fire Loot -"))
check("  one that ends as the other begins does not meet it; one that begins halfway through does",
      guarded(ev(None, "xp", 3.0, starts=NOW, ends=NOW + HOUR), [ev(2, "loot", 2.5, starts=NOW + HOUR, ends=NOW + 2 * HOUR)],
              {"on": False}) is None
      and guarded(ev(None, "xp", 3.0, starts=NOW, ends=NOW + 2 * HOUR),
                  [ev(2, "loot", 2.5, starts=NOW + HOUR, ends=NOW + 3 * HOUR)], {"on": False}) is not None)
check("  the stirring land's monsters: they meet a land's own whenever the lands stir - not while they are off",
      guarded(ev(None, "xp", 3.0, land=services.LAND_STIRRING), [ev(2, "loot", 2.5, land=6)], {"on": True, "xp": 1.0, "bags": 1.0})
      is not None
      and guarded(ev(None, "xp", 3.0, land=services.LAND_STIRRING), [ev(2, "loot", 2.5, land=6)], {"on": False}) is None)
check("  and one called off is not counted",
      guarded(triple_xp, [ev(2, "loot", 2.0, cancelled=NOW - 5, name="Double Loot")]) is None)
check("a kill's whole pay is held to the ceilings, loot giving way first",
      services.ceiling(4.5, 1.0) == (3.0, 1.0) and services.ceiling(3.0, 3.0) == (2.0, 3.0)
      and services.ceiling(2.0, 2.0) == (2.0, 2.0) and services.ceiling(1.0, 5.0) == (1.0, 3.0))
check("allowed, but said: a stir event while the lands are off; two stir events at once",
      services.warnings(ev(None, "stir", 1.0, land=6), [], {"on": False}, LANDS)
      == ["The stirring lands are switched off, so this will stir nothing until they are on (World tab)."]
      and services.warnings(ev(None, "stir", 1.0, land=6), [ev(2, "stir", 1.0, land=3, name="The Ice Lands Stir")],
                            STIR, LANDS) == ["It overlaps The Ice Lands Stir: while both are on, the one that began last stirs."])
TWO_XP = ("It overlaps Double XP: while both are on, a kill gets the bigger XP, never both, and the game "
          "shows only that one.")
check("two Double XPs at once (game 0.50.1): allowed, but said - they do not add up, and the game shows one",
      services.warnings(ev(None, "xp", 2.0), [ev(2, "xp", 2.0, name="Double XP")], STIR, LANDS) == [TWO_XP]
      and services.warnings(ev(None, "loot", 1.5, land=6), [ev(2, "loot", 2.0, name="Double Loot")], STIR, LANDS)
      == ["It overlaps Double Loot: while both are on, a kill gets the bigger loot, never both, and the game "
          "shows only that one."], services.warnings(ev(None, "xp", 2.0), [ev(2, "xp", 2.0, name="Double XP")], STIR, LANDS))
check("  the stirring land's meets any land's; two named lands, one ending as the other begins, "
      "one called off, or XP over loot do not",
      services.warnings(ev(None, "xp", 2.0, land=services.LAND_STIRRING), [ev(2, "xp", 2.0, land=3, name="Double XP")],
                        STIR, LANDS) == [TWO_XP]
      and services.warnings(ev(None, "xp", 2.0, land=6), [ev(2, "xp", 2.0, land=3, name="Double XP")], STIR, LANDS) == []
      and services.warnings(ev(None, "xp", 2.0, starts=NOW + HOUR, ends=NOW + 2 * HOUR),
                            [ev(2, "xp", 2.0, name="Double XP")], STIR, LANDS) == []
      and services.warnings(ev(None, "xp", 2.0), [ev(2, "xp", 2.0, cancelled=NOW - 5, name="Double XP")], STIR, LANDS) == []
      and services.warnings(ev(None, "xp", 2.0), [ev(2, "loot", 2.0, name="Double Loot")], STIR, LANDS) == [])
sql("DELETE FROM world_events")
sql("DELETE FROM broadcasts")
now = int(time.time())
res = client.post("/api/world/events", headers=auth(owner), json={"kind": "loot", "amount": 2, "starts_at": now, "hours": 2})
check("  (Double Loot on every monster, set now)", res.status_code == 201, res.get_json())
res = client.post("/api/world/events", headers=auth(owner), json={"kind": "xp", "amount": 3, "starts_at": now + HOUR, "hours": 2})
check("Triple XP set over it: a 409 that says why - and nothing written, nobody told",
      res.status_code == 409 and res.get_json()["message"].startswith("Too much at once for the stirring land's monsters")
      and len(sql("SELECT id FROM world_events")) == 1 and sql("SELECT id FROM broadcasts WHERE kind = 'event'") == [],
      res.get_json())
stir_land = next(iter(BY_LAND))
client.post("/api/server/world/stir", headers=auth(owner), json={"element": stir_land})
client.post("/api/server/world", headers=auth(owner), json={"bags_times": 3.0})
captured.clear()
gamedata.roll_kill_rewards = spy
try:
    kill(BY_LAND[stir_land])
finally:
    gamedata.roll_kill_rewards = real_roll
check("the stirring lands' loot raised to 3x after Double Loot was set: a kill is still paid 3x loot, not 6x",
      captured and captured[0][0] == 3.0, captured)
client.post("/api/server/world", headers=auth(owner), json={"bags_times": 1.5})
res = client.post("/api/world/events", headers=auth(owner), json={"kind": "stir", "land": 6, "starts_at": now + 9 * HOUR,
                                                                  "hours": 1})
check("a stir event set with the lands on and nothing over it: no warnings",
      res.status_code == 201 and res.get_json()["warnings"] == [], res.get_json())
client.post("/api/server/world", headers=auth(owner), json={"on": False})
res = client.post("/api/world/events", headers=auth(owner), json={"kind": "stir", "land": 3, "starts_at": now + 20 * HOUR,
                                                                  "hours": 1})
client.post("/api/server/world", headers=auth(owner), json={"on": True})
check("  with the lands switched off: set, and the answer says it will stir nothing until they are on",
      res.status_code == 201 and res.get_json()["warnings"]
      == ["The stirring lands are switched off, so this will stir nothing until they are on (World tab)."], res.get_json())
res = client.post("/api/world/events", headers=auth(owner), json={"kind": "loot", "amount": 1.5, "starts_at": now, "hours": 1})
check("a second loot event over Double Loot: set, and the answer says a kill gets the bigger, never both",
      res.status_code == 201 and res.get_json()["warnings"]
      == ["It overlaps Double Loot: while both are on, a kill gets the bigger loot, never both, and the game "
          "shows only that one."], res.get_json())

# =============================================================================
section("E-6  the dry run - what it would do, before it is set")
views = app_module.app.view_functions
for label, token in (("a player gets the same bare 404 from the dry run and the report", player),
                     ("a dev gets the same bare 404 from the dry run and the report", devtok)):
    codes = [client.post("/api/world/events/preview", headers=auth(token),
                         json={"kind": "xp", "amount": 2, "starts_at": now, "hours": 1}).status_code,
             client.get("/api/world/events/report", headers=auth(token)).status_code]
    check(label, codes == [404, 404], codes)
check("  and neither asks for the password again - they only read",
      not getattr(views["preview_event"], "_elusion_fresh_password", False)
      and not getattr(views["read_event_report"], "_elusion_fresh_password", False)
      and getattr(views["preview_event"], "_elusion_min_role", None) == "coowner"
      and getattr(views["read_event_report"], "_elusion_min_role", None) == "coowner")
sql("DELETE FROM world_events")
sql("DELETE FROM kill_reports")
sql("DELETE FROM gold_ledger WHERE reason IN ('loot', 'shop_sell')")
lands_seen = sorted(BY_LAND)[:2]
first_hour = now // HOUR - 60
for i in range(12):
    hour = first_hour + i
    sql("INSERT INTO kill_reports (user_id, slot, enemy_id, xp, attack_xp, level_at, at) VALUES (?, 0, ?, ?, 0, 1, ?)",
        (player_id, BY_LAND[lands_seen[0 if i < 6 else 1]], 100 * (i + 1), hour * HOUR + 60))
    sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, 0, ?, 'loot', 'seed')",
        (hour * HOUR + 120, player_id, 10 * (i + 1)))
# AN HOUR WHEN AN EVENT WAS ON is not the usual, and is left out.
busy_hour = first_hour + 20
sql("INSERT INTO kill_reports (user_id, slot, enemy_id, xp, attack_xp, level_at, at) VALUES (?, 0, ?, 99999, 0, 1, ?)",
    (player_id, BY_LAND[lands_seen[0]], busy_hour * HOUR + 60))
sql("INSERT INTO world_events (name, kind, amount, land, starts_at, ends_at, created_at) VALUES ('Past XP', 'xp', 2, 0, ?, ?, ?)",
    (busy_hour * HOUR, busy_hour * HOUR + HOUR, busy_hour * HOUR - 100))
plan = {"kind": "xp", "amount": 2, "land": 0, "starts_at": now + 86400, "hours": 3, "players": 20}
res = client.post("/api/world/events/preview", headers=auth(owner), json=plan)
body = res.get_json() or {}
cast = body.get("forecast", {})
check("Double XP for 3 hours, 20 players: allowed, each land's most, and the event as it would be",
      res.status_code == 200 and body["allowed"] is True and body["why"] == "" and len(body["worst"]) == 7
      and body["event"]["name"] == "Double XP" and body["event"]["id"] is None, body)
check("  the forecast: the middle half of 12 player-hours (400 to 900 XP an hour) x 20 players x 3 hours, and twice that",
      cast.get("player_hours") == 12 and cast.get("enough") is True
      and cast["xp"] == {"usual": [24000, 54000], "with": [48000, 108000]}
      and cast["gold"] == {"usual": [2400, 5400], "with": [2400, 5400]}, cast)
check("  the hour an event was on (99,999 XP) is not in the usual", cast.get("per_player_hour", {}).get("xp") == [400, 700, 900],
      cast.get("per_player_hour"))
plan = {"kind": "loot", "amount": 2, "land": lands_seen[0], "starts_at": now + 86400, "hours": 3, "players": 20}
cast = client.post("/api/world/events/preview", headers=auth(owner), json=plan).get_json()["forecast"]
check("  one land's Double Loot: half the kills were that land's, so gold 1.5x, XP as usual",
      cast["share"] == 0.5 and cast["gold"]["with"] == [3600, 8100] and cast["xp"]["with"] == cast["xp"]["usual"], cast)
sql("INSERT INTO world_events (name, kind, amount, land, starts_at, ends_at, created_at) VALUES ('Double Loot', 'loot', 2, 0, ?, ?, ?)",
    (now + 86400, now + 86400 + 4 * HOUR, now))
sql("DELETE FROM broadcasts")
before_count = len(sql("SELECT id FROM world_events"))
res = client.post("/api/world/events/preview", headers=auth(owner),
                  json={"kind": "xp", "amount": 3, "land": 0, "starts_at": now + 86400 + HOUR, "hours": 2})
body = res.get_json() or {}
check("Triple XP over it, dry run: not allowed, and why - and nothing written, nobody told",
      res.status_code == 200 and body["allowed"] is False and body["why"].startswith("Too much at once")
      and body["with"] == ["Double Loot"] and len(sql("SELECT id FROM world_events")) == before_count
      and sql("SELECT id FROM broadcasts") == [], body)
bad = [client.post("/api/world/events/preview", headers=auth(owner), json=dict(plan, players=p)).status_code
       for p in (0, 501, "20", True)]
check("  players is 1 to 500: 0, 501, a word and a yes are each a 400", bad == [400, 400, 400, 400], bad)
few = services.forecast(ev(None, "xp", 2.0), 5, services.baseline([(1, 1, 100)], [], [], lambda _e: 0, []), LANDS, True)
check("  from fewer than %d player-hours, the forecast says it is a guess" % services.FEW_PLAYER_HOURS,
      few["enough"] is False and few["player_hours"] == 1)

# =============================================================================
section("E-7  the event report - before, during, after, and who was online")
sql("DELETE FROM online_samples")
sql("DELETE FROM world_events")
sql("DELETE FROM kill_reports")
sql("DELETE FROM gold_ledger WHERE reason IN ('loot', 'shop_sell')")
api = app_module.world_events
T = (int(time.time()) // HOUR - 30) * HOUR
sql("UPDATE sessions SET last_seen_at = ?", (T,))
with app_module.app.app_context():
    db = app_module.get_db()
    api._counted_minute = None
    api.sample(db, T)
    db.commit()
    first_row = [tuple(r) for r in sql("SELECT bucket, everyone, players FROM online_samples")]
    sql("UPDATE sessions SET last_seen_at = 0 WHERE user_id = ?", (player_id,))
    api.sample(db, T + 10)
    api.sample(db, T + 60)
    db.commit()
kept_row = [tuple(r) for r in sql("SELECT bucket, everyone, players FROM online_samples")]
check("who is online, counted: three, one a player - the owner and the dev are not players",
      first_row == [(T // 300, 3, 1)], first_row)
check("  at most once a minute, and the five minutes keep the most they were told", kept_row == [(T // 300, 3, 1)], kept_row)
api._counted_minute = None
poll(player)
mine = sql("SELECT players FROM online_samples WHERE bucket = ?", (int(time.time()) // 300,))
check("  the poll counts: this five minutes has a row, with the player in it", mine and mine[0]["players"] >= 1,
      [dict(r) for r in mine])
S = T + 4 * HOUR
# Online for its first hour only: a five minutes with no row is nobody.
for bucket in range(S // 300, (S + HOUR) // 300):
    sql("INSERT INTO online_samples (bucket, at, everyone, players) VALUES (?, ?, 3, 2)", (bucket, bucket * 300))
for bucket in range((S + 2 * HOUR) // 300, (S + 4 * HOUR) // 300):
    sql("INSERT INTO online_samples (bucket, at, everyone, players) VALUES (?, ?, 2, 1)", (bucket, bucket * 300))
for i in range(4):
    sql("INSERT INTO kill_reports (user_id, slot, enemy_id, xp, attack_xp, level_at, at) VALUES (?, 0, ?, 250, 0, 1, ?)",
        (player_id, BY_LAND[lands_seen[0]], S + 600 + i))
sql("INSERT INTO kill_reports (user_id, slot, enemy_id, xp, attack_xp, level_at, at) VALUES (?, 0, ?, 100, 0, 1, ?)",
    (player_id, BY_LAND[lands_seen[0]], S - 600))
sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, 0, 300, 'loot', 'seed')", (S + 900, player_id))
# Not earned by play: a grant, and gold spent.
sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, 0, 1000, 'staff_gold', 'seed')",
    (S + 950, player_id))
sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, 0, -500, 'shop_buy', 'seed')",
    (S + 960, player_id))
newbie = register("eventsnewbie")
sql("UPDATE users SET created_at = ? WHERE username = 'eventsnewbie'", (S + 600,))


def seed_event(name, kind, start, length, cancelled=0, land=0):
    sql("INSERT INTO world_events (name, kind, amount, land, starts_at, ends_at, created_at, cancelled_at)"
        " VALUES (?, ?, 2, ?, ?, ?, ?, ?)", (name, kind, land, start, start + length, start - 1000, cancelled))


seed_event("Double XP", "xp", S, 2 * HOUR)
seed_event("Early Loot", "loot", T - 3 * HOUR, HOUR)
seed_event("The Fire Lands Stir", "stir", S + 6 * HOUR, 3 * HOUR, cancelled=S + 7 * HOUR, land=6)
seed_event("Old Loot", "loot", int(time.time()) - 20 * 86400, HOUR)
seed_event("On Now", "xp", int(time.time()) - 1800, 2 * HOUR)
seed_event("Too Old", "xp", int(time.time()) - 31 * 86400, HOUR)
res = client.get("/api/world/events/report", headers=auth(owner))
report = res.get_json() or {}
by_name = {e["name"]: e for e in report.get("events", [])}
xp_event = by_name.get("Double XP", {})
spans = xp_event.get("windows", {})
check("the report: every event begun in the last 30 days, newest first - not one older",
      res.status_code == 200 and [e["name"] for e in report["events"]]
      == ["On Now", "The Fire Lands Stir", "Double XP", "Early Loot", "Old Loot"], [e["name"] for e in report.get("events", [])])
check("Double XP: online 0 before, 1 during on average (2 at most, its first hour), 1 after - it brought players",
      spans["before"]["online"] == {"players": 0.0, "peak": 0, "everyone": 0.0}
      and spans["during"]["online"] == {"players": 1.0, "peak": 2, "everyone": 1.5}
      and spans["after"]["online"]["players"] == 1.0 and xp_event["lift"] == 1.0
      and xp_event["verdict"] == "Brought players: 1 more online on average (0 before, 1 during).", xp_event)
check("  XP and gold an hour before and during it (gold earned by play, not granted or spent), the kills, who played, who signed up",
      spans["before"]["xp_per_hour"] == 50 and spans["during"]["xp_per_hour"] == 500
      and spans["during"]["kills_per_hour"] == 2 and spans["during"]["gold_per_hour"] == 150
      and spans["during"]["played"] == 1 and spans["during"]["new_players"] == 1 and spans["after"]["xp_per_hour"] == 0,
      spans)
check("one from before the counting began: not counted, and said so",
      by_name["Early Loot"]["windows"]["before"]["online"] is None and by_name["Early Loot"]["lift"] is None
      and by_name["Early Loot"]["verdict"] == "Online not counted for it - the counting began later.")
stirred = by_name["The Fire Lands Stir"]
check("one called off an hour in: it ended then, and its windows are an hour each",
      stirred["state"] == "cancelled" and stirred["ended_at"] == S + 7 * HOUR
      and stirred["windows"]["during"]["hours"] == 1.0 and stirred["windows"]["before"]["start"] == S + 5 * HOUR
      and stirred["verdict"] == "Nobody was online before it or during it.", stirred)
check("one older than the kill log (two weeks): its XP is not known, not nothing",
      by_name["Old Loot"]["windows"]["during"]["xp_per_hour"] is None
      and by_name["Old Loot"]["windows"]["during"]["gold_per_hour"] == 0)
check("one on now: no after yet", by_name["On Now"]["state"] == "on" and by_name["On Now"]["windows"]["after"] is None)
check("which kind moves the needle: XP events +1 online across the one finished and counted; the stir +0; loot not counted",
      report["kinds"] == [{"kind": "xp", "events": 2, "counted": 1, "lift": 1.0},
                          {"kind": "loot", "events": 2, "counted": 0, "lift": None},
                          {"kind": "stir", "events": 1, "counted": 1, "lift": 0.0}], report.get("kinds"))
chart = [p for p in report.get("online", []) if S <= p["at"] < S + 2 * HOUR]
check("the last 7 days of who was online, for the chart, and when the counting began",
      len(chart) == 12 and all(p["players"] == 2 and p["everyone"] == 3 for p in chart)
      and report["sampling_since"] == (T // 300) * 300 and report["sample_seconds"] == 300, len(chart))

print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
