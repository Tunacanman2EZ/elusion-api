"""
test_teleport.py - moving players, and landing them spread out.

    venv\\Scripts\\python.exe test_teleport.py

Throwaway database in your temp folder. Never touches elusion.db.

TWO HALVES, AND THE FIRST ONE IS THE POINT.

The arithmetic: given a slot, where does that player stand? This is pure
maths with no server in it, so it can be checked properly - and it is the
part that decides whether a hundred players arrive as a ring or as a pile
that shoves itself apart through the nearest wall. The tests below check the
property that actually matters (nobody lands within TELEPORT_SPACING of
anybody else) rather than spot-checking a few coordinates.

The plumbing: who may move whom, and a move surviving until it is actually
carried out.

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_teleport_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "CHECKER"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
app_module.send_mail_async = lambda *a, **k: True
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
                       json={"username": name, "password": "password123"}).get_json()["token"]


offset = app_module.teleport_offset
SPACING = app_module.TELEPORT_SPACING


# =============================================================================
print("\n--- the arithmetic: slots never collide ---")

check("slot 0 is the destination itself", offset(0) == (0.0, 0.0), str(offset(0)))

# Ring sizes: ring r must hold exactly 6r slots.
sizes = {}
for i in range(0, 400):
    sizes.setdefault(app_module.teleport_ring_for_slot(i), 0)
    sizes[app_module.teleport_ring_for_slot(i)] += 1
check("ring 0 holds 1 slot", sizes.get(0) == 1, str(sizes.get(0)))
check("ring 1 holds 6 slots", sizes.get(1) == 6, str(sizes.get(1)))
check("ring 2 holds 12 slots", sizes.get(2) == 12, str(sizes.get(2)))
check("ring 3 holds 18 slots", sizes.get(3) == 18, str(sizes.get(3)))

# THE PROPERTY THAT MATTERS. Two hundred players, and not one pair closer to
# each other than the spacing. A rounding tolerance because the offsets are
# rounded to 2dp for the wire.
points = [offset(i) for i in range(200)]
worst = None
worst_pair = None
for a in range(len(points)):
    for b in range(a + 1, len(points)):
        distance = math.dist(points[a], points[b])
        if worst is None or distance < worst:
            worst = distance
            worst_pair = (a, b)
check("no two of 200 players land within %.0fpx of each other (closest %.1fpx, slots %s)"
      % (SPACING, worst or 0, worst_pair),
      worst is not None and worst >= SPACING - 0.5,
      "closest pair %s at %.2fpx" % (worst_pair, worst or 0))

# Every slot is exactly its ring's radius from the centre.
radius_ok = True
for i in range(1, 200):
    ring = app_module.teleport_ring_for_slot(i)
    got = math.dist((0.0, 0.0), offset(i))
    if abs(got - ring * SPACING) > 0.5:
        radius_ok = False
        break
check("every slot sits on its ring's radius", radius_ok)

check("the same slot always gives the same place",
      all(offset(i) == offset(i) for i in range(50)))

check("a negative slot is treated as the centre rather than crashing",
      offset(-5) == (0.0, 0.0), str(offset(-5)))

# How wide does a big group actually spread? Worth knowing before you use it.
furthest = max(math.dist((0.0, 0.0), offset(i)) for i in range(100))
check("100 players fit inside a %.0fpx radius (they do: %.0fpx)" % (6 * SPACING, furthest),
      furthest <= 6 * SPACING + 0.5, "%.2f" % furthest)


# =============================================================================
print("\n--- who may move whom ---")

owner = register("checker")
dev = register("devguy")
mod = register("modguy")
player = register("someplayer")

client.put("/api/staff/role", json={"username": "devguy", "role": "dev"}, headers=auth(owner))
client.put("/api/staff/role", json={"username": "modguy", "role": "mod"}, headers=auth(owner))

res = client.post("/api/staff/teleport", json={"username": "someplayer", "area": "field"},
                  headers=auth(player))
check("a player cannot teleport anyone", res.status_code == 404, str(res.status_code))

res = client.post("/api/staff/teleport", json={"username": "someplayer", "area": "field"},
                  headers=auth(mod))
check("a mod cannot either - it is dev and above", res.status_code == 404, str(res.status_code))

res = client.post("/api/staff/teleport", json={"username": "someplayer", "area": "field",
                                               "x": 100, "y": 200}, headers=auth(dev))
check("a dev can move a player", res.status_code == 200, str(res.status_code))
check("and it reports one move", res.get_json().get("moved") == 1, str(res.get_json()))

res = client.post("/api/staff/teleport", json={"username": "checker"}, headers=auth(dev))
check("nobody can move the owner", res.status_code == 404, str(res.status_code))

res = client.post("/api/staff/teleport", json={"everyone": True, "area": "field"},
                  headers=auth(dev))
check("a dev cannot move EVERYONE - that is the owner's", res.status_code == 403, str(res.status_code))

res = client.post("/api/staff/teleport", json={}, headers=auth(dev))
check("no username and no everyone is refused", res.status_code == 400, str(res.status_code))


# =============================================================================
print("\n--- the move is collected on the poll the client already makes ---")

res = client.get("/api/server/broadcasts?since=0", headers=auth(player))
port = res.get_json().get("teleport")
check("the player is told where to go", isinstance(port, dict), str(port))
if isinstance(port, dict):
    check("area came through", port.get("area") == "field", str(port))
    check("a lone player lands exactly on the spot",
          port.get("x") == 100 and port.get("y") == 200, str(port))
    check("it carries an id to acknowledge", isinstance(port.get("id"), int), str(port))
    check("and says who sent them", port.get("by") == "devguy", str(port))

res = client.get("/api/server/broadcasts?since=0", headers=auth(mod))
check("nobody else is told to move", res.get_json().get("teleport") is None,
      str(res.get_json().get("teleport")))


# =============================================================================
print("\n--- a move survives until it is actually carried out ---")

port = client.get("/api/server/broadcasts?since=0", headers=auth(player)).get_json()["teleport"]
check("still waiting after a poll that did not ack it", isinstance(port, dict))

res = client.post("/api/teleport/ack", json={"id": port["id"] + 999}, headers=auth(player))
check("a wrong id clears nothing", res.get_json().get("cleared") is False, str(res.get_json()))
check("so the move is still there",
      client.get("/api/server/broadcasts?since=0",
                 headers=auth(player)).get_json().get("teleport") is not None)

res = client.post("/api/teleport/ack", json={"id": port["id"]}, headers=auth(player))
check("the right id clears it", res.get_json().get("cleared") is True, str(res.get_json()))
check("and the player is not moved twice",
      client.get("/api/server/broadcasts?since=0",
                 headers=auth(player)).get_json().get("teleport") is None)


# =============================================================================
print("\n--- moving everyone spreads them out ---")

res = client.post("/api/staff/teleport",
                  json={"everyone": True, "area": "town", "x": 500, "y": 500},
                  headers=auth(owner))
check("the owner can move everyone", res.status_code == 200, str(res.status_code))
moved = res.get_json().get("moved", 0)
check("it moved all four accounts", moved == 4, str(moved))

landing = {}
for name, token in [("checker", owner), ("devguy", dev), ("modguy", mod), ("someplayer", player)]:
    spot = client.get("/api/server/broadcasts?since=0", headers=auth(token)).get_json().get("teleport")
    check("%s was given a place" % name, isinstance(spot, dict), str(spot))
    if isinstance(spot, dict):
        landing[name] = (spot["x"], spot["y"])
        check("%s knows it is 1 of %d" % (name, moved), spot.get("of") == moved, str(spot))

check("all four landed somewhere different", len(set(landing.values())) == len(landing), str(landing))
if len(landing) > 1:
    spots = list(landing.values())
    closest = min(math.dist(spots[a], spots[b])
                  for a in range(len(spots)) for b in range(a + 1, len(spots)))
    check("and none within %.0fpx of another (closest %.1fpx)" % (SPACING, closest),
          closest >= SPACING - 0.5, "%.2f" % closest)

# A second teleport replaces the first rather than queueing behind it.
client.post("/api/staff/teleport", json={"username": "someplayer", "area": "crypt"},
            headers=auth(owner))
spot = client.get("/api/server/broadcasts?since=0", headers=auth(player)).get_json()["teleport"]
check("a newer teleport replaces the waiting one", spot.get("area") == "crypt", str(spot))


# =============================================================================
print("\n--- it shows up in the powers list, because that list is derived ---")

d = client.get("/api/staff/powers", headers=auth(owner)).get_json()
dev_paths = []
for rank in d["ladder"]:
    if rank["rank"] == "dev":
        dev_paths = [r["path"] for r in rank["routes"]]
check("/api/staff/teleport appears under dev automatically",
      "/api/staff/teleport" in dev_paths, str(dev_paths))


# =============================================================================
print("\n--- saves.area: the field that was dead until now ---")

# WHY THIS SECTION EXISTS. saves.area shipped from the beginning and nothing on
# the client ever set it, so every character read "elusion" forever - which
# quietly made trade proximity meaningless, since tradepanel.gd compares this
# exact field to decide who is near whom.
#
# Fixing the client half exposed a second bug: the server defaulted the column
# to "elusion" on EVERY write, so any save that did not mention an area yanked
# that character back to the starting zone. It now follows the same "omitted
# means leave it alone" rule as active_pet_id, equipment, hotbar and explored.

mover = register("mover")


def area_of(token):
    return client.get("/api/character?slot=0", headers=auth(token)).get_json().get("area")


client.put("/api/save", json={"slot": 0, "class_id": "warrior", "name": "w"},
           headers=auth(mover))
check("a brand new character starts in elusion", area_of(mover) == "elusion", str(area_of(mover)))

client.put("/api/save", json={"slot": 0, "class_id": "warrior", "name": "w", "area": "field"},
           headers=auth(mover))
check("the server stores the area the client reports", area_of(mover) == "field", str(area_of(mover)))

client.put("/api/save", json={"slot": 0, "class_id": "warrior", "name": "w"},
           headers=auth(mover))
check("OMITTING it leaves it alone (this was the bug)", area_of(mover) == "field",
      str(area_of(mover)))

client.put("/api/save", json={"slot": 0, "class_id": "warrior", "name": "w", "area": "   "},
           headers=auth(mover))
check("a blank area is treated as omitted, not as a move home",
      area_of(mover) == "field", str(area_of(mover)))

client.put("/api/save", json={"slot": 0, "class_id": "warrior", "name": "w", "area": "bossarena"},
           headers=auth(mover))
check("and it still moves when actually told to", area_of(mover) == "bossarena",
      str(area_of(mover)))

res = client.put("/api/save", json={"slot": 0, "class_id": "warrior", "name": "w",
                                    "area": "x" * 80}, headers=auth(mover))
check("an absurd area string is refused", res.status_code == 400, str(res.status_code))

# The payoff: "come here" works, because the caller's area finally means something.
client.put("/api/save", json={"slot": 0, "class_id": "warrior", "name": "w", "area": "bossarena"},
           headers=auth(owner))
res = client.post("/api/staff/teleport", json={"username": "mover"}, headers=auth(owner))
check("teleport with no area defaults to the caller's REAL area",
      res.get_json().get("area") == "bossarena", str(res.get_json()))


# =============================================================================
print("\n=================================")
print("passed: %d   failed: %d" % (passed, failed))
if failures:
    print("failed checks:")
    for name in failures:
        print("  - %s" % name)
print("=================================")
sys.exit(1 if failed else 0)
