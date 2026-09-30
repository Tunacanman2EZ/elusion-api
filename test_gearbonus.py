"""
What an amulet adds, counted by the server. Run: python3 test_gearbonus.py

WHY THE SERVER HAS TO COUNT IT AT ALL. max_hp and max_mana are server-derived
(gamedata.max_stats_for), and PUT /api/player/status clamps hp to that figure.
A Vitality amulet the server could not see would raise the client's ceiling and
then be clamped away on every save - the player would watch sixty health vanish
each time the game synced. So the bonus lives in gamedata.json, and every route
that derives a maximum reads what the character is wearing.

The amulet families follow their colour: green is health (Vitality), blue is
mana (Arcana), crimson is damage (Fury), purple is armour (Ward).
"""
import importlib.util, os, re, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Temp directory, not beside this file - see the note in test_healing.py.
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_gearbonus_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "NOT_A_TEST_ACCOUNT"
os.environ.pop("ELUSION_GAMEDATA", None)
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(_spec)
sys.modules["elusion_app"] = app_module
_spec.loader.exec_module(app_module)
client = app_module.app.test_client()
gamedata = app_module.gamedata

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))


def account(u):
    client.post("/api/auth/register", json={"username": u, "password": "hunter2hunter2"})
    tok = client.post("/api/auth/login", json={"username": u, "password": "hunter2hunter2"}).get_json()["token"]
    return {"Authorization": "Bearer " + tok}

def db():
    return sqlite3.connect(DB_PATH)

def uid_of(u):
    conn = db(); r = conn.execute("SELECT id FROM users WHERE username = ?", (u,)).fetchone(); conn.close()
    return r[0]

def give(u, item_id, qty=1, slot=0):
    conn = db()
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (u,)).fetchone()[0]
    used = [r[0] for r in conn.execute(
        "SELECT position FROM carry_items WHERE user_id=? AND slot=?", (uid, slot))]
    position = next(i for i in range(200) if i not in used)
    conn.execute("INSERT INTO carry_items (user_id, slot, position, item_id, quantity)"
                 " VALUES (?,?,?,?,?)", (uid, slot, position, item_id, qty))
    conn.commit(); conn.close()

def set_row(u, slot=0, **fields):
    conn = db()
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (u,)).fetchone()[0]
    sets = ", ".join("%s = ?" % k for k in fields)
    conn.execute("UPDATE saves SET %s WHERE user_id = ? AND slot = ?" % sets,
                 list(fields.values()) + [uid, slot])
    conn.commit(); conn.close()

def row_of(u, slot=0):
    conn = db(); conn.row_factory = sqlite3.Row
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (u,)).fetchone()[0]
    r = conn.execute("SELECT * FROM saves WHERE user_id = ? AND slot = ?", (uid, slot)).fetchone()
    conn.close(); return r

def equip(h, item_id, slot=0):
    return client.post("/api/character/equip", headers=h, json={"slot": slot, "item_id": item_id})

def unequip(h, es, slot=0):
    return client.post("/api/character/unequip", headers=h, json={"slot": slot, "equip_slot": es})

def status(h, slot=0):
    return client.get("/api/player/status?slot=%d" % slot, headers=h).get_json()

def curve(class_id, level):
    return gamedata.max_stats_for(class_id, level)


# =============================================================================
section("THE AMULETS ARE IN THE CATALOGUE")
# =============================================================================
FAMILIES = {
    # family: (bonus field, tiers it spans)
    "vitality": ("bonus_max_hp", [3, 4, 5]),
    "arcana":   ("bonus_max_mana", [2, 3, 4, 5]),
    "fury":     ("bonus_damage_percent", [2, 3, 4, 5]),
    "ward":     ("armor_value", [2, 3, 4, 5]),
}
PREFIX = {2: "lesser", 3: "", 4: "greater", 5: "exalted"}
LEVEL_FOR_TIER = {2: 5, 3: 10, 4: 16, 5: 22}
OTHER_BONUSES = set(gamedata.GEAR_BONUS_FIELDS) | {"armor_value", "damage"}

amulets = {}
for family, (field, tiers) in FAMILIES.items():
    amounts = []
    for tier in tiers:
        iid = "%s%samulet" % (PREFIX[tier], family)
        item = gamedata.ITEMS.get(iid)
        check("%s exists" % iid, item is not None)
        if item is None:
            continue
        amulets[iid] = item
        check("  worn as an amulet", gamedata.equip_slot_for(iid) == "amulet", item.get("equip_slot_name"))
        check("  at tier %d, level %d" % (tier, LEVEL_FOR_TIER[tier]),
              int(item["tier"]) == tier and int(item["required_level"]) == LEVEL_FOR_TIER[tier],
              (item["tier"], item["required_level"]))
        check("  any class can wear it", not item.get("required_classes"), item.get("required_classes"))
        amount = int(item.get(field, 0) or 0)
        amounts.append(amount)
        check("  carries its family's bonus (%s > 0)" % field, amount > 0, amount)
        stray = [f for f in OTHER_BONUSES - {field} if int(item.get(f, 0) or 0) != 0]
        check("  and nothing else", not stray, stray)
        check("  can drop from an enemy of its tier",
              iid in gamedata.loot_pool(tier, "gear"), tier)
    check("%s climbs with tier" % family, amounts == sorted(set(amounts)), amounts)

check("fifteen new amulets", len(amulets) == 15, len(amulets))
check("each worth more than the plain amulet of its tier",
      all(int(a["value"]) > int(gamedata.ITEMS[{2: "jadeamulet", 3: "cobaltamulet",
                                                 4: "amethystamulet", 5: "emberamulet"}[int(a["tier"])]]["value"])
          for a in amulets.values()))


# =============================================================================
section("GEAR_BONUSES SUMS WHAT IS WORN, WHERE IT IS WORN")
# =============================================================================
b = gamedata.gear_bonuses({"amulet": "exaltedvitalityamulet"})
check("a Vitality amulet adds its health", b["bonus_max_hp"] == 60, b)
b = gamedata.gear_bonuses({"amulet": "greaterarcanaamulet"})
check("an Arcana amulet adds its mana", b["bonus_max_mana"] == 55, b)
b = gamedata.gear_bonuses({"amulet": "furyamulet"})
check("a Fury amulet adds its damage percent", b["bonus_damage_percent"] == 5, b)
b = gamedata.gear_bonuses({"weapon": "exaltedvitalityamulet"})
check("an amulet stored under the wrong slot adds nothing", b["bonus_max_hp"] == 0, b)
b = gamedata.gear_bonuses({"amulet": "nosuchamulet"})
check("an unknown id adds nothing", sum(b.values()) == 0, b)
for bad in (None, [], "amulet", 7):
    b = gamedata.gear_bonuses(bad)
    check("a %s map adds nothing" % type(bad).__name__, sum(b.values()) == 0, b)
b = gamedata.gear_bonuses({"amulet": 12})
check("a non-string id adds nothing", sum(b.values()) == 0, b)
b = gamedata.gear_bonuses({"chest": "ironchest", "amulet": "vitalityamulet"})
check("plain armour adds no bonus of its own", b["bonus_max_hp"] == 25, b)

_saved = dict(gamedata.ITEMS["vitalityamulet"])
try:
    gamedata.ITEMS["vitalityamulet"]["bonus_max_hp"] = -500
    b = gamedata.gear_bonuses({"amulet": "vitalityamulet"})
    check("a negative bonus in the catalogue is floored at 0", b["bonus_max_hp"] == 0, b)
finally:
    gamedata.ITEMS["vitalityamulet"].clear()
    gamedata.ITEMS["vitalityamulet"].update(_saved)

bare = curve("warrior", 22)
worn = gamedata.max_stats_for("warrior", 22, {"amulet": "exaltedvitalityamulet"})
check("max_stats_for adds the health to the curve", worn["max_hp"] == bare["max_hp"] + 60, (worn, bare))
check("  and leaves mana alone", worn["max_mana"] == bare["max_mana"], (worn, bare))
worn = gamedata.max_stats_for("mage", 22, {"amulet": "exaltedarcanaamulet"})
check("an Arcana amulet lifts max mana", worn["max_mana"] == curve("mage", 22)["max_mana"] + 80, worn)
worn = gamedata.max_stats_for("warrior", 22, {"amulet": "exaltedwardamulet"})
check("a Ward amulet does not touch the maxima", worn == bare, worn)
check("no equipment is the bare curve", gamedata.max_stats_for("warrior", 22, {}) == bare)


# =============================================================================
section("PUTTING ONE ON RAISES THE CEILING, NOT THE HEALTH")
# =============================================================================
H = account("amuletwearer")
client.put("/api/save", headers=H, json={"slot": 0, "class_id": "warrior", "name": "Wearer"})
BASE22 = curve("warrior", 22)
set_row("amuletwearer", level=22, hp=BASE22["max_hp"], max_hp=BASE22["max_hp"],
        mana=BASE22["max_mana"], max_mana=BASE22["max_mana"])
give("amuletwearer", "exaltedvitalityamulet")

r = equip(H, "exaltedvitalityamulet")
check("equip -> 200", r.status_code == 200, r.data[:200])
body = r.get_json() or {}
st = body.get("stats", {})
check("the response names the new ceiling", st.get("max_hp") == BASE22["max_hp"] + 60, st)
check("and hp where it was", st.get("hp") == BASE22["max_hp"], st)
row = row_of("amuletwearer")
check("the row holds the raised max_hp", int(row["max_hp"]) == BASE22["max_hp"] + 60, row["max_hp"])
check("and has not refilled", int(row["hp"]) == BASE22["max_hp"], row["hp"])

# REGEN FILLS THE GAP, AND THE STATUS ROUTE LETS IT STAND. Written straight to
# the row, as though the time had passed, so the healing reconciler has nothing
# to argue with - what is under test is the CEILING, not the regen allowance.
set_row("amuletwearer", hp=BASE22["max_hp"] + 60)
r = client.put("/api/player/status", headers=H, json={"slot": 0, "hp": BASE22["max_hp"] + 60})
check("a status save at the raised maximum -> 200", r.status_code == 200, r.data[:200])
s = status(H)
check("and is not clamped back to the bare curve",
      s["hp"] == BASE22["max_hp"] + 60 and s["max_hp"] == BASE22["max_hp"] + 60, (s["hp"], s["max_hp"]))
r = client.put("/api/player/status", headers=H, json={"slot": 0, "hp": BASE22["max_hp"] + 61})
s = status(H)
check("one point over the raised maximum is still clamped", s["hp"] <= BASE22["max_hp"] + 60, s["hp"])
r = client.put("/api/player/status", headers=H, json={"slot": 0, "max_hp": 99999})
check("the client still cannot name its own max_hp",
      status(H)["max_hp"] == BASE22["max_hp"] + 60, status(H)["max_hp"])

r = client.put("/api/save", headers=H, json={"slot": 0, "class_id": "warrior", "name": "Wearer"})
check("a whole-character save keeps the bonus",
      int(row_of("amuletwearer")["max_hp"]) == BASE22["max_hp"] + 60, row_of("amuletwearer")["max_hp"])


# =============================================================================
section("TAKING IT OFF TAKES THE HEALTH WITH IT")
# =============================================================================
r = unequip(H, "amulet")
check("unequip -> 200", r.status_code == 200, r.data[:200])
st = (r.get_json() or {}).get("stats", {})
check("the ceiling is back to the curve", st.get("max_hp") == BASE22["max_hp"], st)
check("and hp came down to it", st.get("hp") == BASE22["max_hp"], st)
row = row_of("amuletwearer")
check("the row agrees: hp is not above max_hp",
      int(row["hp"]) == int(row["max_hp"]) == BASE22["max_hp"], (row["hp"], row["max_hp"]))

set_row("amuletwearer", hp=100)
give("amuletwearer", "exaltedvitalityamulet") if "exaltedvitalityamulet" not in [
    r_[0] for r_ in db().execute("SELECT item_id FROM carry_items WHERE user_id = ?",
                                 (uid_of("amuletwearer"),))] else None
equip(H, "exaltedvitalityamulet")
unequip(H, "amulet")
check("a wounded character stays exactly as wounded through on and off",
      int(row_of("amuletwearer")["hp"]) == 100, row_of("amuletwearer")["hp"])

# THE SAME CLAMP THROUGH A SAVE, for a row an older server left over its ceiling.
set_row("amuletwearer", hp=BASE22["max_hp"] + 60, max_hp=BASE22["max_hp"] + 60)
client.put("/api/save", headers=H, json={"slot": 0, "class_id": "warrior", "name": "Wearer"})
row = row_of("amuletwearer")
check("a save brings an over-ceiling hp down to the derived maximum",
      int(row["hp"]) == int(row["max_hp"]) == BASE22["max_hp"], (row["hp"], row["max_hp"]))


# =============================================================================
section("ARCANA IS MANA, FURY AND WARD MOVE NEITHER POOL")
# =============================================================================
M = account("amuletmage")
client.put("/api/save", headers=M, json={"slot": 0, "class_id": "mage", "name": "Arcanist"})
MAGE = curve("mage", 22)
set_row("amuletmage", level=22, hp=MAGE["max_hp"], max_hp=MAGE["max_hp"],
        mana=MAGE["max_mana"], max_mana=MAGE["max_mana"])
for iid in ("exaltedarcanaamulet", "exaltedfuryamulet", "exaltedwardamulet"):
    give("amuletmage", iid)

r = equip(M, "exaltedarcanaamulet")
st = (r.get_json() or {}).get("stats", {})
check("Arcana: max_mana rises by 80", st.get("max_mana") == MAGE["max_mana"] + 80, st)
check("Arcana: max_hp does not move", st.get("max_hp") == MAGE["max_hp"], st)
r = equip(M, "exaltedfuryamulet")
st = (r.get_json() or {}).get("stats", {})
check("swapping to Fury: max_mana falls back", st.get("max_mana") == MAGE["max_mana"], st)
check("  and mana is not left above it",
      int(st.get("mana", 1) or 0) <= int(st.get("max_mana", 0) or 0) and "mana" in st, st)
check("  and max_hp is untouched", st.get("max_hp") == MAGE["max_hp"], st)
check("the Arcana amulet went back into the bag",
      "exaltedarcanaamulet" in [r_[0] for r_ in db().execute(
          "SELECT item_id FROM carry_items WHERE user_id = ?", (uid_of("amuletmage"),))])
r = equip(M, "exaltedwardamulet")
st = (r.get_json() or {}).get("stats", {})
check("Ward: neither maximum moves",
      st.get("max_hp") == MAGE["max_hp"] and st.get("max_mana") == MAGE["max_mana"], st)


# =============================================================================
section("THE LEVEL IS STILL THE GATE")
# =============================================================================
L = account("amuletlowbie")
client.put("/api/save", headers=L, json={"slot": 0, "class_id": "warrior", "name": "Lowbie"})
give("amuletlowbie", "exaltedvitalityamulet")
r = equip(L, "exaltedvitalityamulet")
check("a level 1 cannot wear a level 22 amulet -> 403", r.status_code == 403, r.status_code)
check("and gains nothing from holding it",
      int(row_of("amuletlowbie")["max_hp"]) == curve("warrior", 1)["max_hp"], row_of("amuletlowbie")["max_hp"])


# =============================================================================
section("DYING, COMING BACK AND LEVELLING ALL KEEP THE BONUS")
# =============================================================================
D = account("amuletdier")
client.put("/api/save", headers=D, json={"slot": 0, "class_id": "warrior", "name": "Dier"})
set_row("amuletdier", level=22, hp=BASE22["max_hp"], max_hp=BASE22["max_hp"])
give("amuletdier", "exaltedvitalityamulet")
equip(D, "exaltedvitalityamulet")
r = client.put("/api/player/status", headers=D, json={"slot": 0, "hp": 0})
check("the character dies", r.status_code == 200 and status(D)["hp"] == 0, r.data[:160])
r = client.post("/api/character/respawn", headers=D, json={"slot": 0})
check("respawn -> 200", r.status_code == 200, r.data[:200])
s = status(D)
check("respawn fills to the maximum WITH the amulet",
      s["hp"] == s["max_hp"] == BASE22["max_hp"] + 60, (s["hp"], s["max_hp"]))

# A PAID REVIVE fills to the same maximum. Lusions are written straight onto
# the account: what is under test is the ceiling the revive fills to, not the
# price, which test_economy.py covers.
r = client.put("/api/player/status", headers=D, json={"slot": 0, "hp": 0})
conn = db()
conn.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (uid_of("amuletdier"),))
conn.execute("UPDATE accounts SET lusions = ? WHERE user_id = ?",
             (int(gamedata.CONSTANTS.get("revive_cost", 20)) * 2, uid_of("amuletdier")))
conn.commit(); conn.close()
r = client.post("/api/character/revive", headers=D, json={"slot": 0, "pay": "lusions"})
check("revive -> 200", r.status_code == 200, r.data[:200])
s = status(D)
check("a revive fills to the maximum WITH the amulet",
      s["hp"] == s["max_hp"] == BASE22["max_hp"] + 60, (s["hp"], s["max_hp"]))

# A LEVEL EARNED FROM A KILL re-derives the maxima in the kill route itself.
# One XP short of the curve's requirement: the kill route derives xp_to_next
# from the level, so writing a small one into the row no longer forces a level.
set_row("amuletdier", level=10, xp=gamedata.xp_needed_for_level(10) - 1, hp=100)
before_level = 10
enemy = sorted(e for e in gamedata.ENEMIES
               if gamedata.ENEMIES[e].get("grants_rewards", True)
               and gamedata.spawn_count_for(e) > 0)[0]
r = client.post("/api/combat/kill", headers=D, json={"slot": 0, "enemy_id": enemy})
check("a kill -> 200", r.status_code == 200, r.data[:200])
row = row_of("amuletdier")
check("the kill levelled the character", int(row["level"]) > before_level, row["level"])
check("and the new max_hp includes the amulet",
      int(row["max_hp"]) == curve("warrior", int(row["level"]))["max_hp"] + 60,
      (row["max_hp"], curve("warrior", int(row["level"]))["max_hp"]))


# =============================================================================
section("EVERY DERIVATION GOES THROUGH THE ONE THAT READS THE GEAR")
# =============================================================================
# A new route that called gamedata.max_stats_for(class, level) directly would
# derive a bare maximum, and the next status save would take a Vitality
# amulet's health away. _derived_stats(row) is the only door.
src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
# "(?!\))" skips prose that names the function - "max_stats_for()" in a
# docstring - and keeps every call that passes it something.
calls = [m.start() for m in re.finditer(r"gamedata\.max_stats_for\((?!\))", src)]
helper_at = src.find("def _derived_stats(")
helper_end = src.find("\ndef ", helper_at + 1)
check("app.py calls max_stats_for exactly once", len(calls) == 1, len(calls))
check("  and that once is inside _derived_stats()",
      all(helper_at < c < helper_end for c in calls), calls)
check("every _derived_stats caller exists (at least six routes use it)",
      src.count("_derived_stats(") >= 7, src.count("_derived_stats("))


print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
