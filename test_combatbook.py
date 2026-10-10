"""
The server's books on every monster. Run: python3 test_combatbook.py

combatbook.py keeps its own count of every monster's health from what an
area's leader says ("w") and every hit any game sends, holds each hit to what
that character could really do (gamedata.combat_bounds()), and judges every
death: AGREED, SHORT or NOT DUE. E3_SCOPE.md, option C, step 1 - it watches,
and nothing in play changes. Pure logic, driven with its own clock; presence.py
wiring is test_presence.py P-8.

  CB-1  THE BOUNDS ARE THE GAME'S ARITHMETIC: each class's biggest hit and
        damage a second from the best it holds and may wear, its skills, its
        pets; what it may not wear does not count; an older ticket only
        loosens them.
  CB-2  AN HONEST FIGHT AGREES: hits inside the bounds, paced at the rate,
        bring the server's count to zero with the leader's; one row per
        player who hit, with what each did.
  CB-3  A DEATH NOBODY CAUSED IS SHORT: a monster the leader kills with no
        hits, or with less than its health.
  CB-4  ONE HIT TOO BIG is booked at the most the character could land and
        written down - unless a fresh ticket shows it could after all.
  CB-5  TOO FAST: damage past the character's rate is refused by the books,
        counted against them, and written down.
  CB-6  SPAWNS AGAINST THE MAP: an unknown monster, a spot the map does not
        hold, the wrong monster at a spot, two at one spot, one back before
        the respawn - and a scene loading is not a respawn.
  CB-7  SLIMES: a large that split at half health releases its smalls and no
        more; one that split early is written down; a twin needs a large to
        make it; a large's split pays nobody and so is no row.
  CB-8  LATE HITS: a hit that crossed the death in the air still counts as
        helping, for HOLD_SECONDS.
  CB-9  JOINED PART-WAY: a monster the books never saw makes them ask the
        leader for everything (once in a while, not every message), and a
        monster joined part-way is counted from the leader's word.
  CB-10 A NEW LEADER'S WORD on health is taken where it is lower, never
        higher.
  CB-11 WALKING: an honest walk and sprint pass; faster is written down, and so
        is a teleport-sized step.
  CB-12 RANGE: a hit from beyond any screen is written down, and still booked.
  CB-13 NOTHING A LEADER SENDS BREAKS THE BOOKS.
"""
import json, math, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.pop("ELUSION_GAMEDATA", None)
sys.path.insert(0, HERE)

import gamedata  # noqa: E402
import combatbook  # noqa: E402
from combatbook import AreaBook, Fighter  # noqa: E402

passed = failed = 0
failures = []


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print("  pass  %s" % label)
    else:
        failed += 1
        failures.append(label)
        print("  FAIL  %s   %s" % (label, detail))


def section(title):
    print("\n=== %s ===\n" % title)


C = gamedata.COMBAT
FIELD = gamedata.AREAS["field"]
SPOTS = {s["o"]: s for s in FIELD["spawns"]}


def spot_of(enemy_id):
    return next(s for s in FIELD["spawns"] if s["e"] == enemy_id)


def record(monster_id, enemy_id, origin=None, x=None, y=None, hp=None, mh=None, **props):
    row = gamedata.ENEMIES[enemy_id]
    spot = spot_of(enemy_id) if origin is None and any(s["e"] == enemy_id for s in FIELD["spawns"]) else None
    o = origin if origin is not None else (spot["o"] if spot else "")
    p = {"ed": row["resource"], "eo": -1, "lr": 400.0, "g": False}
    p.update(props)
    return {"id": monster_id, "o": o, "s": "res://scene/enemy/x.tscn", "pp": "ysortworld/enemies",
            "x": x if x is not None else (spot["x"] if spot else 0.0),
            "y": y if y is not None else (spot["y"] if spot else 0.0),
            "a": "idledown", "hp": hp if hp is not None else row["max_hp"],
            "mh": mh if mh is not None else row["max_hp"], "p": p}


def warrior(user_id=1, slot=0, gear=("ironsword",), agility=1, lvl=1):
    f = Fighter(user_id, slot)
    f.set_bounds(gamedata.combat_bounds({"cls": "warrior", "lvl": lvl, "gear": list(gear),
                                         "skills": {"attack": 1, "magic": 1, "agility": agility},
                                         "pets": []}), 0.0)
    return f


def fresh_field(now=0.0, leader=None):
    """A book on the Field, just loaded, and its leader - who has said nothing
    of where it stands yet, so no hit is measured for distance."""
    book = AreaBook("field", FIELD)
    leader = leader or warrior(1)
    leader.scene_at = now
    return book, leader


def kinds(flags):
    return [f[2] for f in flags]


def fight(book, fighter, monster_id, total, now, step=None):
    """Hits of the fighter's own biggest honest size, paced at its rate, until
    `total` damage is in. Returns the time after the last."""
    unit = fighter.bounds["max_hit"]
    gap = step if step is not None else unit / fighter.bounds["dps"]
    left = total
    while left > 0:
        dmg = min(unit, left)
        book.hit(fighter, monster_id, dmg, now)
        left -= dmg
        now += gap
    return now


# =============================================================================
section("CB-1 THE BOUNDS ARE THE GAME'S ARITHMETIC")
# =============================================================================
# Worked here from the exported numbers, apart from gamedata.py's own code.
def top(item_id):
    item = gamedata.item_row(item_id)
    dmg, spread = item["damage"], min(max(item["damage_spread"], 0.0), 0.9)
    low = max(1, math.floor(dmg * (1 - spread)))
    return max(low, math.ceil(dmg * (1 + spread)))


def pct(item_id):
    # A weapon from jade up carries a damage bonus of its own, worn in the
    # weapon slot like any other.
    return 1 + max(0, int(gamedata.item_row(item_id).get("bonus_damage_percent", 0) or 0)) / 100.0


def hit_of(base, item_id):
    return (base + top(item_id)) * pct(item_id)


cls_rows = C["classes"]
w = cls_rows["warrior"]
b = gamedata.combat_bounds({"cls": "warrior", "lvl": 1, "gear": ["ironsword"], "skills": {}, "pets": []})
unit = w["base"] + top("ironsword")
check("a warrior's biggest hit is its base and the sword's top roll",
      b["max_hit"] == math.ceil(unit), (b, unit))
check("  and its damage a second is a swing and its wave, once a swing",
      abs(b["dps"] - unit * (1 + w["wave_ratio"]) / w["swing_seconds"]) < 0.02, b)
check("  and it walks at its own speed, sprinting",
      b["speed"] == w["speed"] * C["sprint"], b)

b = gamedata.combat_bounds({"cls": "warrior", "lvl": 1, "gear": ["ironsword"],
                            "skills": {"attack": 51, "magic": 11, "agility": 31}, "pets": []})
mult = 1 + 50 * C["skill_step"] + 10 * C["skill_step"]
haste = 1 + 30 * C["agility_step"]
check("skills raise the hit by their steps, agility the rate and the walk",
      b["max_hit"] == math.ceil(unit * mult)
      and abs(b["dps"] - unit * mult * haste * (1 + w["wave_ratio"]) / w["swing_seconds"]) < 0.05
      and b["speed"] == (w["speed"] + 30 * C["agility_speed"]) * C["sprint"], b)
b = gamedata.combat_bounds({"cls": "warrior", "lvl": 1, "gear": [], "skills": {"agility": 99}, "pets": []})
check("  agility at its highest is its steps, under the cap the game puts on it",
      abs(b["dps"] - w["base"] * min(1 + 98 * C["agility_step"], C["agility_cap"])
          * (1 + w["wave_ratio"]) / w["swing_seconds"]) < 0.05, b)

held = gamedata.combat_bounds({"cls": "warrior", "lvl": 30, "gear": ["ironsword", "embersword"],
                               "skills": {}, "pets": []})
check("the best weapon HELD counts, worn or not, with its own damage bonus",
      held["max_hit"] == math.ceil(hit_of(w["base"], "embersword")), held)
low = gamedata.combat_bounds({"cls": "warrior", "lvl": 1, "gear": ["ironsword", "embersword"],
                              "skills": {}, "pets": []})
check("  but not one the character may not wear yet (its level)",
      low["max_hit"] == w["base"] + top("ironsword"), low)
other = gamedata.combat_bounds({"cls": "mage", "lvl": 30, "gear": ["embersword"], "skills": {}, "pets": []})
check("  nor another class's", other["max_hit"] == cls_rows["mage"]["base"], other)

# THE DOUBLE AXE LEFT SPINNING (0.11.9): it climbs to axe_spin_max_rate swings
# a second, and a pass out or back is a swing - the bound holds the top rate,
# or a warrior who leaves the axe in a pack is "too fast" for being good at it.
spin = float(w.get("axe_spin_max_rate", 0))
ba = gamedata.combat_bounds({"cls": "warrior", "lvl": 30, "gear": ["doubleaxe"], "skills": {}, "pets": []})
check("the catalogue says how fast the Double Axe spins at the most",
      spin > 1.0, w)
bleed = float(w.get("axe_bleed_share", 0)) / float(w.get("axe_bleed_every", 1) or 1)
check("  and a warrior with it is bounded at that rate, plus a pass, plus the bleed its cuts leave (0.14.0)",
      bleed > 0
      and abs(ba["dps"] - hit_of(w["base"], "doubleaxe") * (1 / w["cooldown"] + (1 + spin) / w["swing_seconds"] + bleed)) < 0.05,
      [ba, spin, bleed])
_older_row = dict(w)
_older_row.pop("axe_spin_max_rate", None)
_older_row.pop("axe_bleed_share", None)
_older_row.pop("axe_bleed_every", None)
_saved_row = cls_rows["warrior"]
cls_rows["warrior"] = _older_row
try:
    bo = gamedata.combat_bounds({"cls": "warrior", "lvl": 30, "gear": ["doubleaxe"], "skills": {}, "pets": []})
finally:
    cls_rows["warrior"] = _saved_row
check("  a catalogue from before says nothing, and spins at 1x and bleeds nothing - the old bound",
      abs(bo["dps"] - hit_of(w["base"], "doubleaxe") * (1 / w["cooldown"] + 2 / w["swing_seconds"])) < 0.05, bo)

m = cls_rows["mage"]
b1 = gamedata.combat_bounds({"cls": "mage", "lvl": 30, "gear": ["emberstaff"], "skills": {}, "pets": []})
b2 = gamedata.combat_bounds({"cls": "mage", "lvl": 30, "gear": ["meteorite"], "skills": {}, "pets": []})
check("a mage casts once a cooldown",
      abs(b1["dps"] - hit_of(m["base"], "emberstaff") / m["cooldown"]) < 0.05, b1)
check("  the Meteorite twice, and its crater's burn on top (0.12.0)",
      m.get("meteor_burn_share", 0) > 0
      and abs(b2["dps"] - hit_of(m["base"], "meteorite") * (2 / m["cooldown"] + m["meteor_burn_share"] / m["meteor_burn_every"])) < 0.05,
      [b2, m])
t = cls_rows["tank"]
b1 = gamedata.combat_bounds({"cls": "tank", "lvl": 30, "gear": ["embermaul"], "skills": {}, "pets": []})
b2 = gamedata.combat_bounds({"cls": "tank", "lvl": 30, "gear": ["dynamite"], "skills": {}, "pets": []})
ticks = t["dynamite_stick_ticks"]
per_throw = max(2, t["dynamite_bundle_sticks"], t.get("dynamite_barrage_sticks", 0))
check("a tank's aura ticks once a cooldown",
      b1["max_hit"] == math.ceil(hit_of(t["base"], "embermaul"))
      and abs(b1["dps"] - hit_of(t["base"], "embermaul") / t["cooldown"]) < 0.05, b1)
chain = 1 + t.get("dynamite_chain_bonus", 0)
check("  and with Dynamite the ring still burns (0.14.0), and on top of it every throw is the most sticks one can be"
      " - a barrage's five since game 0.18.0 - of dynamite_stick_ticks ticks each, harder when a blast set it off"
      " (0.12.0), with its scorch smouldering (0.13.0)",
      chain > 1 and t.get("dynamite_field_share", 0) > 0 and ticks > 1 and per_throw == 5
      and t["dynamite_barrage_sticks"] == 5 and 0 < t["dynamite_barrage_chance"] < t["dynamite_bundle_chance"] < 1
      and b2["max_hit"] == math.ceil(hit_of(t["base"], "dynamite") * ticks * chain)
      and abs(b2["dps"] - hit_of(t["base"], "dynamite") * (1 / t["cooldown"] + per_throw * ticks * chain / t["dynamite_cooldown"]
                                                           + ticks * t["dynamite_field_share"] / t["dynamite_field_every"])) < 0.05,
      [b2, t])
_older_tank = {k: v for k, v in t.items()
               if k not in ("dynamite_stick_ticks", "dynamite_bundle_chance", "dynamite_bundle_sticks",
                            "dynamite_barrage_chance", "dynamite_barrage_sticks")}
_saved_tank = cls_rows["tank"]
cls_rows["tank"] = _older_tank
try:
    bt = gamedata.combat_bounds({"cls": "tank", "lvl": 30, "gear": ["dynamite"], "skills": {}, "pets": []})
finally:
    cls_rows["tank"] = _saved_tank
old_sticks = t["dynamite_cooldown"] / t["cooldown"]
check("  a catalogue from before 0.14.0 says nothing: a stick is the cooldown's worth, two at most a throw - the old bound",
      bt["max_hit"] == math.ceil(hit_of(t["base"], "dynamite") * old_sticks * chain)
      and abs(bt["dps"] - hit_of(t["base"], "dynamite") * (1 / t["cooldown"] + 2 * old_sticks * chain / t["dynamite_cooldown"]
                                                           + old_sticks * t["dynamite_field_share"] / t["dynamite_field_every"])) < 0.05,
      bt)
_mid_tank = {k: v for k, v in t.items() if k not in ("dynamite_barrage_chance", "dynamite_barrage_sticks")}
cls_rows["tank"] = _mid_tank
try:
    bm = gamedata.combat_bounds({"cls": "tank", "lvl": 30, "gear": ["dynamite"], "skills": {}, "pets": []})
finally:
    cls_rows["tank"] = _saved_tank
check("  a catalogue from 0.14.0 to 0.17 says nothing of the barrage: a bundle's three at most a throw",
      abs(bm["dps"] - hit_of(t["base"], "dynamite") * (1 / t["cooldown"] + 3 * ticks * chain / t["dynamite_cooldown"]
                                                           + ticks * t["dynamite_field_share"] / t["dynamite_field_every"])) < 0.05
      and bm["dps"] < b2["dps"], [bm, b2])
h = cls_rows["healer"]
b1 = gamedata.combat_bounds({"cls": "healer", "lvl": 30, "gear": ["emberscepter"], "skills": {}, "pets": []})
check("a healer fires once a cooldown",
      abs(b1["dps"] - hit_of(h["base"], "emberscepter") / h["cooldown"]) < 0.05, b1)

pet = C["pets"]["petboss"]
bp = gamedata.combat_bounds({"cls": "healer", "lvl": 1, "gear": [], "skills": {}, "pets": ["petboss"]})
check("a pet hits for half the character's multiplier, and adds its rate and its puddles",
      bp["max_hit"] == max(h["base"], int(pet["damage"] * C["pet_share"]))
      and abs(bp["dps"] - (h["base"] / h["cooldown"] + int(pet["damage"] * C["pet_share"]) / pet["cooldown"]
                           + C["puddle_damage"] * gamedata.PUDDLE_TICKS_PER_SECOND)) < 0.05, bp)
check("  only a pet the ticket says is held",
      gamedata.combat_bounds({"cls": "healer", "lvl": 1, "gear": [], "skills": {}, "pets": ["nopet"]})["dps"]
      == h["base"] / h["cooldown"])
rolled = gamedata.perfect_id("ironsword")
br = gamedata.combat_bounds({"cls": "warrior", "lvl": 1, "gear": [rolled], "skills": {}, "pets": []})
check("a Perfect roll's damage is the rolled damage", br["max_hit"] == w["base"] + top(rolled)
      and br["max_hit"] > w["base"] + top("ironsword"), br)
older = gamedata.combat_bounds({"cls": "warrior", "lvl": 1})
check("an older ticket with no gear and no skills only loosens them",
      older["max_hit"] >= held["max_hit"] and older["dps"] >= held["dps"], older)
check("a class nobody knows gets the loosest of the four",
      gamedata.combat_bounds({"cls": "bard", "lvl": 1, "gear": [], "skills": {}, "pets": []})["max_hit"]
      == max(r["base"] for r in cls_rows.values()))


# =============================================================================
section("CB-2 AN HONEST FIGHT AGREES")
# =============================================================================
book, lead = fresh_field()
sprite = spot_of("darksprite")
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(1, "darksprite"), record(2, "firesprite")]}, 0.5)
check("a scene loading is taken whole, with no flags", set(book.monsters) == {1, 2}
      and not book.take()[1], book.monsters)
hp = gamedata.ENEMIES["darksprite"]["max_hp"]
lead.moved("field", sprite["x"] + 30, sprite["y"], 1.0, fresh=True)
helper = warrior(2, slot=3)
helper.moved("field", sprite["x"] - 30, sprite["y"], 1.0, fresh=True)
now = fight(book, lead, 1, hp // 2, 1.0)
now = fight(book, helper, 1, hp - hp // 2, now)
check("  the books count it down with every hit", book.monsters[1].hp == 0, book.monsters[1].hp)
book.world(lead, {"ev": [{"k": "die", "id": 1, "x": sprite["x"], "y": sprite["y"]}]}, now)
book.tick(now + 0.5)
check("a death waits for late hits", not book.kills)
book.tick(now + combatbook.HOLD_SECONDS)
kills, flags = book.take()
check("then it is judged: agreed", [k["verdict"] for k in kills] == ["agreed", "agreed"], kills)
by_user = {k["user_id"]: k for k in kills}
check("  one row per player who hit it, with their own damage and character",
      by_user[1]["damage"] == hp // 2 and by_user[2]["damage"] == hp - hp // 2
      and by_user[2]["slot"] == 3 and by_user[1]["enemy_id"] == "darksprite"
      and by_user[1]["origin"] == sprite["o"] and by_user[1]["leader_id"] == 1, kills)
check("  and no flags for an honest fight", not flags and not lead.take_flags() and not helper.take_flags(),
      flags)

book.world(lead, {"hits": [[2, 5, 0]], "ev": [{"k": "die", "id": 2, "x": 0, "y": 0}]}, now + 2)
book.tick(now + 4)
kills, _ = book.take()
check("the leader's own hits inside its world are booked before the death they caused",
      kills and kills[0]["user_id"] == 1 and kills[0]["damage"] == 5, kills)


# =============================================================================
section("CB-3 A DEATH NOBODY CAUSED IS SHORT")
# =============================================================================
book, lead = fresh_field()
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(1, "darksprite"), record(2, "firesprite")]}, 0.5)
book.world(lead, {"ev": [{"k": "die", "id": 1, "x": 0, "y": 0}]}, 1.0)
book.tick(3.0)
kills, _ = book.take()
check("a monster the leader kills with no hits is SHORT, on the leader, with all its health left",
      len(kills) == 1 and kills[0]["verdict"] == "short" and kills[0]["user_id"] == 1
      and kills[0]["damage"] == 0 and kills[0]["hp_left"] == hp, kills)
fight(book, lead, 2, gamedata.ENEMIES["firesprite"]["max_hp"] // 2, 3.0)
book.world(lead, {"ev": [{"k": "die", "id": 2, "x": 0, "y": 0}]}, 10.0)
book.tick(12.0)
kills, _ = book.take()
check("  and one killed with half its health is short by the other half",
      kills[0]["verdict"] == "short"
      and kills[0]["hp_left"] == gamedata.ENEMIES["firesprite"]["max_hp"] - gamedata.ENEMIES["firesprite"]["max_hp"] // 2,
      kills)


book, lead = fresh_field()
lead.reports_hits = False
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(1, "darksprite"), record(2, "darkslimelarge")]}, 0.5)
book.world(lead, {"ev": [{"k": "die", "id": 1, "x": 0, "y": 0}, {"k": "die", "id": 2, "x": 0, "y": 0}]}, 1.0)
book.tick(5.0)
kills, flags = book.take()
check("a leader from before 0.10.0 (no own hits) is not judged: no row, not a false SHORT",
      not kills and not flags, (kills, flags))
book.world(lead, {"ev": [{"k": "spawn", "r": record(9, gamedata.ENEMIES["darkslimelarge"]["splits_into"],
                                                    origin="", sm=True)}]}, 1.2)
check("  though its large slime's smalls still have a split to come from", not book.take()[1])


# =============================================================================
section("CB-4 ONE HIT TOO BIG")
# =============================================================================
book, lead = fresh_field()
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(1, "darksprite"), record(2, "firesprite")]}, 0.5)
cap = int(lead.bounds["max_hit"] * combatbook.SLACK)
book.hit(lead, 1, hp, 1.0)
check("a hit bigger than the character could land is booked at the most it could",
      book.monsters[1].damage[1] == cap and book.monsters[1].hp == hp - cap, book.monsters[1].damage)
lead.tick(1.0 + combatbook.PENDING_SECONDS - 0.1)
check("  and waits for a fresh ticket before it is called too big", not lead.take_flags())
lead.tick(1.0 + combatbook.PENDING_SECONDS)
flags = lead.take_flags()
check("  then it is written down, with the numbers", kinds(flags) == ["hit_too_big"]
      and str(hp) in flags[0][3], flags)

big = gamedata.combat_bounds({"cls": "warrior", "lvl": 30, "gear": ["embersword"], "skills": {}, "pets": []})
book.hit(lead, 2, big["max_hit"], 5.0)
check("a hit that fits a sword equipped a moment ago is capped meanwhile",
      book.monsters[2].damage[1] == cap)
lead.set_bounds(big, 5.5)
lead.tick(9.0)
check("  and booked in full when the fresh ticket arrives, with no flag",
      book.monsters[2].damage[1] == big["max_hit"] and not lead.take_flags(), book.monsters[2].damage)


# =============================================================================
section("CB-5 TOO FAST")
# =============================================================================
book, lead = fresh_field()
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(1, "darkslimelarge"), record(2, "firesprite")]}, 0.5)
big_hp = gamedata.ENEMIES["darkslimelarge"]["max_hp"]
burst = lead.bounds["dps"] * combatbook.SLACK * combatbook.BURST_SECONDS
unit = lead.bounds["max_hit"]
n = int(burst // unit) + 10
for i in range(n):
    book.hit(lead, 1, unit, 1.0)
m1 = book.monsters[1]
check("hits faster than the character's rate are refused past the burst it may land",
      m1.damage[1] <= burst + unit and m1.refused.get(1, 0) >= 9 * unit, (m1.damage, m1.refused, burst))
flags = lead.take_flags()
check("  and each one over is written down", "too_fast" in kinds(flags)
      and all(k == "too_fast" for k in kinds(flags)), flags)
book.hit(lead, 2, unit, 1.0)
check("  another monster has its own allowance", book.monsters[2].damage.get(1) == unit
      and not lead.take_flags())
before = m1.damage[1]
book.hit(lead, 1, unit, 1.0 + unit / (lead.bounds["dps"] * combatbook.SLACK) + 0.01)
check("  and the allowance refills at the character's rate", m1.damage[1] == before + unit, m1.damage)


# =============================================================================
section("CB-6 SPAWNS AGAINST THE MAP")
# =============================================================================
book, lead = fresh_field()
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(1, "darksprite"), record(2, "firesprite")]}, 0.5)
book.take()
spot = spot_of("darksprite")


def spawn(book, r, now):
    book.world(lead, {"ev": [{"k": "spawn", "r": r}]}, now)
    return kinds(book.take()[1])


check("a monster the catalogue does not have", spawn(book, dict(record(10, "darksprite", origin=""),
      p={"ed": "res://data/enemies/nothing.tres"}), 1.0) == ["unknown_monster"])
check("a spot the map does not hold", spawn(book, record(11, "darksprite", origin="ysortworld/enemies/elsewhere"),
                                            1.0) == ["not_on_map"])
check("the wrong monster at a spot",
      spawn(book, record(12, "firesprite", origin=spot["o"]), 1.0) == ["wrong_monster"])
check("two at one spot", spawn(book, record(13, "darksprite"), 1.0) == ["unaccounted_spawn"])
check("a different maximum health", spawn(book, record(14, "lightsprite",
      mh=gamedata.ENEMIES["lightsprite"]["max_hp"] * 10), 1.0) == ["max_hp_differs"])
check("  which the books ignore: they count from the catalogue's",
      book.monsters[14].hp == gamedata.ENEMIES["lightsprite"]["max_hp"])
check("a monster from nowhere", spawn(book, record(15, "firesprite", origin=""), 1.0) == ["unaccounted_spawn"])
spawn(book, record(16, "windsprite", hp=1), 1.0)
check("a monster that spawns is counted from the catalogue's health, whatever the leader says it has",
      book.monsters[16].hp == gamedata.ENEMIES["windsprite"]["max_hp"], book.monsters[16].hp)
check("  and every one of those deaths would be NOT DUE",
      all(book.monsters[i].not_due for i in (10, 11, 12, 13, 15)) and not book.monsters[14].not_due)
book.world(lead, {"ev": [{"k": "gone", "id": 12}, {"k": "gone", "id": 13}]}, 1.0)

fight(book, lead, 1, hp, 2.0)
book.world(lead, {"ev": [{"k": "die", "id": 1, "x": 0, "y": 0}]}, 3.0)
respawn = FIELD["respawn_seconds"]
check("one back before the area's respawn",
      spawn(book, record(20, "darksprite"), 3.0 + respawn - combatbook.RESPAWN_SLACK - 1) == ["back_too_soon"])
book.world(lead, {"ev": [{"k": "die", "id": 20, "x": 0, "y": 0}]}, 3.0 + respawn)
check("  one back on time passes", spawn(book, record(21, "darksprite"), 3.0 + respawn + respawn) == [])
book.tick(200.0)
kills, _ = book.take()
check("  the early one's death was NOT DUE", [k["verdict"] for k in kills if k["enemy_id"] == "darksprite"]
      == ["agreed", "not_due"], kills)

book.world(lead, {"ev": [{"k": "die", "id": 21, "x": 0, "y": 0}]}, 300.0)
lead.moved("elusion", 0, 0, 301.0, fresh=True)
book.emptied(301.0)
book.tick(301.0 + combatbook.EMPTY_KEEP)
lead.moved("field", 0, 0, 305.0 + combatbook.EMPTY_KEEP)
T = 306.0 + combatbook.EMPTY_KEEP
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1, "spawn": [record(1, "darksprite", hp=3)]}, T)
check("walking back in brings every monster back, as it always has: not judged",
      not book.take()[1] and not book.monsters[1].not_due)
check("  and a scene loading is counted from the catalogue's health too", book.monsters[1].hp == hp, book.monsters[1].hp)
book.world(lead, {"ev": [{"k": "die", "id": 1, "x": 0, "y": 0}]}, T + 1)
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1, "spawn": [record(1, "darksprite")]},
           T + 1 + combatbook.RESET_GRACE + 2)
check("  but a reload long after arriving is judged like a respawn",
      kinds(book.take()[1]) == ["back_too_soon"])


# =============================================================================
section("CB-7 SLIMES")
# =============================================================================
book, lead = fresh_field()
large = "darkslimelarge"
small = gamedata.ENEMIES[large]["splits_into"]
count = int(gamedata.ENEMIES[large]["split_count"])
lhp = gamedata.ENEMIES[large]["max_hp"]
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(1, large), record(2, "earthslimelarge")]}, 0.5)
book.take()
twin = record(3, large, origin="", du=True, sm=False, sp=False)
check("a large near a player makes one twin", spawn(book, twin, 1.0) == [])
check("  and only one", spawn(book, dict(twin, id=4), 1.0) == ["unaccounted_spawn"])
check("  a twin with no large to make it", spawn(book, record(5, "windslimelarge", origin="", du=True), 1.0)
      == ["unaccounted_spawn"])
fight(book, lead, 1, lhp // 2 + 1, 2.0)
book.world(lead, {"ev": [{"k": "die", "id": 1, "x": 0, "y": 0}]}, 5.0)
out = [spawn(book, record(100 + i, small, origin="", sm=True, du=True, sp=True), 5.4) for i in range(count)]
check("a large split at half its health releases its %d smalls" % count, all(f == [] for f in out), out)
check("  and no more", spawn(book, record(200, small, origin="", sm=True), 5.5) == ["unaccounted_spawn"])
book.world(lead, {"ev": [{"k": "die", "id": 2, "x": 0, "y": 0}]}, 6.0)
check("a large split with most of its health is written down", kinds(book.take()[1]) == ["split_early"])
check("  and its smalls are from nowhere",
      spawn(book, record(300, "earthslime", origin="", sm=True), 6.2) == ["unaccounted_spawn"])
book.tick(20.0)
kills, _ = book.take()
check("a large's split pays nobody, so it is no row", all(k["enemy_id"] != large for k in kills), kills)


# =============================================================================
section("CB-8 LATE HITS")
# =============================================================================
book, lead = fresh_field()
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1, "spawn": [record(1, "darksprite")]}, 0.5)
fight(book, lead, 1, hp, 1.0)
book.world(lead, {"ev": [{"k": "die", "id": 1, "x": 0, "y": 0}]}, 20.0)
helper = warrior(2)
book.hit(helper, 1, 10, 20.5)
book.tick(20.0 + combatbook.HOLD_SECONDS)
book.hit(helper, 1, 10, 21.5)
kills, _ = book.take()
check("a hit that crossed the death in the air counts as helping",
      {k["user_id"]: k["damage"] for k in kills} == {1: hp, 2: 10}, kills)
check("  one after the books closed counts for nothing", book.stray == 1, book.stray)


# =============================================================================
section("CB-9 JOINED PART-WAY")
# =============================================================================
book = AreaBook("field", FIELD)
lead = warrior(1)
book.world(lead, {"snap": [[7, 10.0, 10.0, "idledown", 300]]}, 1.0)
check("a monster the books never saw makes them ask the leader for everything", book.needs_full(1.0))
check("  once in a while, not every message", not book.needs_full(1.5) and book.needs_full(4.1))
half = gamedata.ENEMIES["firesprite"]["max_hp"] // 2
book.world(lead, {"full": True, "reset": False, "part": 0, "parts": 1,
                  "spawn": [record(7, "firesprite", hp=half)]}, 5.0)
check("  the answer is taken, and the asking stops", 7 in book.monsters and not book.needs_full(10.0))
check("  a monster joined part-way is counted from the leader's word", book.monsters[7].hp == half,
      book.monsters[7].hp)
fight(book, lead, 7, half, 6.0)
book.world(lead, {"ev": [{"k": "die", "id": 7, "x": 0, "y": 0}]}, 30.0)
book.tick(32.0)
kills, flags = book.take()
check("  and agrees when the hits since cover it", kills and kills[0]["verdict"] == "agreed"
      and not flags, (kills, flags))


# =============================================================================
section("CB-10 A NEW LEADER'S WORD")
# =============================================================================
book, lead = fresh_field()
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(1, "darksprite"), record(2, "firesprite")]}, 0.5)
heir = warrior(2)
book.world(heir, {"full": True, "reset": False, "part": 0, "parts": 1,
                  "spawn": [record(1, "darksprite", hp=hp - 40), record(2, "firesprite", hp=999999)]}, 10.0)
check("a new leader's first word is taken where it is lower (hits the old one never sent)",
      book.monsters[1].hp == hp - 40, book.monsters[1].hp)
check("  never where it is higher", book.monsters[2].hp == gamedata.ENEMIES["firesprite"]["max_hp"])
book.world(heir, {"full": True, "reset": False, "part": 0, "parts": 1,
                  "spawn": [record(1, "darksprite", hp=1)]}, 20.0)
check("  and only its first word", book.monsters[1].hp == hp - 40)


book, lead = fresh_field()
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(1, "darksprite"), record(2, "firesprite")]}, 0.5)
fight(book, lead, 1, 100, 1.0)
book.emptied(5.0)
book.tick(5.0 + combatbook.EMPTY_KEEP - 1)
lead.moved("field", 0, 0, 6.0, fresh=True)
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(1, "darksprite", hp=hp - 140), record(2, "firesprite")]}, 6.5)
check("a leader whose link dropped and came back carries on with the books' count",
      book.monsters[1].damage.get(1) == 100, book.monsters[1].damage)
check("  taken where its own count is lower (its hits while the link was down), never higher",
      book.monsters[1].hp == hp - 140 and book.monsters[2].hp == gamedata.ENEMIES["firesprite"]["max_hp"])
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(3, "darkslimelarge")]}, 7.0)
book.world(lead, {"ev": [{"k": "spawn", "r": record(4, "darkslimelarge", origin="", du=True)}]}, 7.5)
book.take()
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1,
                  "spawn": [record(3, "darkslimelarge", du=False)]}, 8.0)
check("  and a large the reload made whole may make its twin again",
      book.monsters[3].twins_left == 1, book.monsters[3].twins_left)
book.emptied(10.0)
book.tick(10.0 + combatbook.EMPTY_KEEP)
check("an area left empty longer than that forgets its living", not book.monsters)


# =============================================================================
section("CB-11 WALKING")
# =============================================================================
f = warrior(1)
speed = f.bounds["speed"]
f.moved("field", 0.0, 0.0, 0.0, fresh=True)
x, now = 0.0, 0.0
for _ in range(80):
    now += 0.1
    x += speed * 0.1
    f.moved("field", x, 0.0, now)
check("sprinting flat out for eight seconds is honest", not f.take_flags())
for _ in range(50):
    now += 0.1
    x += speed * 0.1 * 1.8
    f.moved("field", x, 0.0, now)
flags = f.take_flags()
check("  faster than the character can is written down", "too_quick" in kinds(flags)
      and "jumped" not in kinds(flags), flags)
now += 0.1
f.moved("field", x + 2000.0, 0.0, now)
check("a teleport-sized step is a jump", kinds(f.take_flags()) == ["jumped"])
now += 30.0
f.moved("field", x + 2000.0 + 50.0, 0.0, now)
check("  standing still and walking on is not", not f.take_flags())
f.moved("bigfield", 9000.0, 9000.0, now + 0.1, fresh=True)
check("  nor is arriving in another area", not f.take_flags())
f2 = warrior(2)
f2.moved("field", 0.0, 0.0, 0.0, fresh=True)
t = 0.0
for i in range(60):
    # Bunched: nothing for 0.7 s, then three states at once.
    t += 0.7 if i % 4 == 0 else 0.005
    f2.moved("field", speed * (i + 1) * 0.1 * 0.8, 0.0, t)
check("states the network bunched together are not a burst of speed", not f2.take_flags())


# =============================================================================
section("CB-12 RANGE")
# =============================================================================
book, lead = fresh_field()
book.world(lead, {"full": True, "reset": True, "part": 0, "parts": 1, "spawn": [record(1, "darksprite")]}, 0.5)
lead.moved("field", sprite["x"] + combatbook.REACH + 500, sprite["y"], 1.0, fresh=True)
book.hit(lead, 1, 10, 1.0)
flags = lead.take_flags()
check("a hit from beyond any screen is written down", kinds(flags) == ["too_far"], flags)
check("  and still booked: how far honest hits land is what the watch week measures",
      book.monsters[1].damage.get(1) == 10, book.monsters[1].damage)
lead.moved("field", sprite["x"] + 100, sprite["y"], 2.0, fresh=True)
book.hit(lead, 1, 10, 2.0)
check("  one from beside it is not", not lead.take_flags())


# =============================================================================
section("CB-13 NOTHING A LEADER SENDS BREAKS THE BOOKS")
# =============================================================================
rng = random.Random(7)
shapes = [None, True, 1, -1, 2 ** 40, 1.5, float("nan"), "x", [], {}, [1, 2], [[1, "a", 3]],
          {"id": "1"}, {"id": 1, "p": "x"}, {"id": 1, "p": {"ed": 5}, "x": "a"}]


def junk(depth=0):
    if depth > 2 or rng.random() < 0.4:
        return rng.choice(shapes)
    if rng.random() < 0.5:
        return [junk(depth + 1) for _ in range(rng.randint(0, 4))]
    keys = ["full", "reset", "part", "parts", "spawn", "ev", "snap", "hits", "k", "r", "id", "x", "y"]
    return {rng.choice(keys): junk(depth + 1) for _ in range(rng.randint(0, 5))}


book, lead = fresh_field()
raised = []
for i in range(3000):
    d = junk()
    if not isinstance(d, dict):
        d = {"ev": [{"k": rng.choice(["spawn", "die", "gone"]), "r": junk(), "id": junk()}],
             "snap": [junk()], "hits": junk(), "full": rng.choice([True, False]), "spawn": [junk()]}
    try:
        book.world(lead, d, float(i))
        book.tick(float(i))
        book.needs_full(float(i))
    except Exception as exc:  # noqa: BLE001
        raised.append((d, repr(exc)))
check("three thousand malformed world messages raise nothing", not raised, raised[:2])
for i in range(200):
    try:
        f = Fighter(1)
        f.set_bounds(gamedata.combat_bounds({"cls": junk(), "lvl": junk(), "gear": junk(),
                                             "skills": junk(), "pets": junk()}), 0.0)
        book.hit(f, rng.randint(0, 5), rng.randint(1, 100000), float(i))
        f.tick(float(i) + 10)
    except Exception as exc:  # noqa: BLE001
        raised.append(repr(exc))
check("  nor do malformed tickets", not raised, raised[:2])


print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
if failures:
    for name in failures:
        print("  - " + name)
sys.exit(1 if failed else 0)
