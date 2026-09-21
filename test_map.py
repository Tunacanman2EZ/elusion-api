"""
The map: the fog rules, and the column that carries them.

Run with:  python3 test_map.py

The first half is a transcription of worldmap.gd's bitmask — the same indexing,
the same circular reveal, the same compress-and-base64 — so a rule that is
wrong here is wrong there. The second half posts at the real server.
"""

import base64
import json
import math
import os
import tempfile
import zlib

passed = failed = 0


def check(label, ok, detail=None):
    global passed, failed
    if ok:
        passed += 1
        print("  ok   %s" % label)
    else:
        failed += 1
        print("  FAIL %s   %r" % (label, detail))


def section(title):
    print("\n%s\n%s" % (title, "-" * len(title)))


# =============================================================================
# worldmap.gd, in Python
# =============================================================================

SEEN_RADIUS = 7
MAX_AREA_TILES = 262144


class Area:
    def __init__(self, origin, size, tile=(16, 16)):
        self.origin = origin
        self.size = size
        self.tile = tile
        self.bits = bytearray((size[0] * size[1] + 7) // 8)

    def bit_index(self, tile):
        x = tile[0] - self.origin[0]
        y = tile[1] - self.origin[1]
        if x < 0 or y < 0 or x >= self.size[0] or y >= self.size[1]:
            return -1
        return y * self.size[0] + x

    def tile_at(self, world):
        return (math.floor(world[0] / self.tile[0]),
                math.floor(world[1] / self.tile[1]))

    def is_seen(self, tile):
        i = self.bit_index(tile)
        return i >= 0 and bool(self.bits[i >> 3] & (1 << (i & 7)))

    def reveal_around(self, world):
        centre = self.tile_at(world)
        changed = False
        for dy in range(-SEEN_RADIUS, SEEN_RADIUS + 1):
            for dx in range(-SEEN_RADIUS, SEEN_RADIUS + 1):
                if dx * dx + dy * dy > SEEN_RADIUS * SEEN_RADIUS:
                    continue
                i = self.bit_index((centre[0] + dx, centre[1] + dy))
                if i < 0:
                    continue
                byte, mask = i >> 3, 1 << (i & 7)
                if self.bits[byte] & mask:
                    continue
                self.bits[byte] |= mask
                changed = True
        return changed

    def explored_fraction(self):
        total = self.size[0] * self.size[1]
        if total <= 0:
            return 0.0
        count = 0
        for b in self.bits:
            while b:
                b &= b - 1
                count += 1
        return count / total

    def to_save(self):
        return {"w": self.size[0], "h": self.size[1],
                "ox": self.origin[0], "oy": self.origin[1],
                "bits": base64.b64encode(zlib.compress(bytes(self.bits))).decode()}


def from_save(entry):
    w, h = entry["w"], entry["h"]
    expected = (w * h + 7) // 8
    try:
        raw = zlib.decompress(base64.b64decode(entry["bits"]))
    except Exception:
        return None
    if len(raw) != expected:
        return None
    area = Area((entry["ox"], entry["oy"]), (w, h))
    area.bits = bytearray(raw)
    return area


# =============================================================================
section("THE BITMASK")
# =============================================================================
# The field is the biggest area in the game at 126 x 108. Its whole exploration
# state is one bit per tile.

field = Area((-16, -14), (126, 108))
check("one bit per tile, and no more",
      len(field.bits) == (126 * 108 + 7) // 8 == 1701, len(field.bits))
check("which is under two kilobytes for the largest area in the game",
      len(field.bits) < 2048, len(field.bits))
check("a fresh area has been seen nowhere", field.explored_fraction() == 0.0)

check("a tile inside the bounds has an index",
      field.bit_index((0, 0)) >= 0)
check("and one outside does not",
      field.bit_index((-17, 0)) == -1 and field.bit_index((110, 0)) == -1,
      [field.bit_index((-17, 0)), field.bit_index((110, 0))])
check("the top-left tile is index zero",
      field.bit_index((-16, -14)) == 0)
check("and the bottom-right is the last one",
      field.bit_index((-16 + 125, -14 + 107)) == 126 * 108 - 1)

# THE ORIGIN IS NOT ZERO, AND THAT IS THE WHOLE POINT. Every area in this game
# has tiles at negative coordinates — the field runs from x = -16. An
# implementation that assumed the world started at 0 would index every tile in
# the left-hand quarter of the map out of range and quietly refuse to reveal it.
check("negative world coordinates map to real tiles",
      field.bit_index((-16, -14)) == 0 and field.bit_index((-1, -1)) > 0)


# =============================================================================
section("REVEALING")
# =============================================================================

area = Area((0, 0), (60, 60))
check("standing still the first time reveals something",
      area.reveal_around((30 * 16, 30 * 16)) is True)
check("standing still a second time reveals nothing",
      area.reveal_around((30 * 16, 30 * 16)) is False)

# The signal that redraws the open map panel rides on that False. `moved` fires
# every physics frame while a key is held; if standing still reported a change,
# the panel would rebuild its image sixty times a second.
check("you can see where you are standing",
      area.is_seen((30, 30)))
check("and seven tiles away", area.is_seen((30 + 7, 30)))
check("but not eight", not area.is_seen((30 + 8, 30)))

# CIRCLES, NOT SQUARES. The diagonal at (5,5) is 7.07 tiles away and must be
# dark, or the reveal is a square wearing a radius.
check("the corner of the square is not revealed",
      not area.is_seen((35, 35)), "distance %.2f" % math.hypot(5, 5))
check("but a point at the same distance along an axis is",
      area.is_seen((37, 30)))

fresh = Area((0, 0), (60, 60))
fresh.reveal_around((30 * 16, 30 * 16))
lit = sum(1 for y in range(60) for x in range(60) if fresh.is_seen((x, y)))
check("one step lights a disc, not a block",
      lit == 149, lit)

# Walking off the edge is a normal thing to do at an area boundary.
edge = Area((0, 0), (20, 20))
edge.reveal_around((0, 0))
check("revealing at the corner clips rather than wrapping",
      edge.is_seen((0, 0)) and not edge.is_seen((19, 19)))

# floori, not int(). int() truncates toward zero, so world x = -8 with a 16px
# tile would give tile 0 instead of -1, and everything in the column left of
# the origin would light the wrong tile.
check("a world position left of the origin rounds down, not toward zero",
      Area((-5, -5), (10, 10)).tile_at((-8.0, -8.0)) == (-1, -1),
      Area((-5, -5), (10, 10)).tile_at((-8.0, -8.0)))


# =============================================================================
section("WHAT GETS STORED")
# =============================================================================

walked = Area((-16, -14), (126, 108))
x, y = 0, 40
for step in range(200):
    walked.reveal_around((x * 16, y * 16))
    x += 1 if step % 3 else 0
    y -= 1 if step % 5 else 0

saved = walked.to_save()
raw_bits = len(walked.bits)
stored = len(saved["bits"])
check("a well-walked field compresses to a fraction of its bitmask",
      stored < raw_bits, "%d bytes of base64 from %d bytes of bits"
      % (stored, raw_bits))
check("and is nowhere near the 64 KB the server allows",
      len(json.dumps({"field": saved})) < 8192,
      len(json.dumps({"field": saved})))

back = from_save(saved)
check("it comes back byte for byte", back is not None and back.bits == walked.bits)
check("with its origin intact", back.origin == (-16, -14), back.origin)
check("and the same tiles lit",
      all(back.is_seen((tx, ty)) == walked.is_seen((tx, ty))
          for tx in range(-16, 110, 7) for ty in range(-14, 94, 7)))

check("a truncated blob is refused rather than half-read",
      from_save({"w": 126, "h": 108, "ox": 0, "oy": 0,
                 "bits": saved["bits"][:40]}) is None)
check("a blob that decompresses to the wrong size is refused too",
      from_save({"w": 10, "h": 10, "ox": 0, "oy": 0,
                 "bits": saved["bits"]}) is None)
check("and so is something that is not compressed at all",
      from_save({"w": 10, "h": 10, "ox": 0, "oy": 0,
                 "bits": base64.b64encode(b"hello").decode()}) is None)

# A WORLD THAT CHANGED SHAPE. The bits are indexed by position within the
# bounds, so an area that gained a row shifts every one of them. worldmap.gd
# compares the stored size against the rebuilt one and starts the fog again
# rather than drawing a map displaced by one tile.
check("a saved map records the shape it was written for",
      saved["w"] == 126 and saved["h"] == 108)
check("so a world that has since grown can be detected",
      (saved["w"], saved["h"]) != (127, 108))


# =============================================================================
section("HOW OFTEN THE MAP COSTS A REQUEST")
# =============================================================================
# The map changes every few steps, and ServerStorage pushes /api/save whenever
# the save body differs from the last one it sent. Putting the map straight in
# that body turned walking in a straight line into a PUT every three or four
# seconds, which is what it looked like in the server log:
#
#     16:51:21 "PUT /api/save HTTP/1.1" 200
#     16:51:26 "PUT /api/save HTTP/1.1" 200
#     16:51:30 "PUT /api/save HTTP/1.1" 200
#
# WorldMap.save_revision() counts up at most once every forty-five seconds and
# only when something was uncovered. ServerStorage fingerprints on that instead
# of on the map. These are the three cases that have to hold.

SAVE_REVISION_SECONDS = 45


class Revision:
    """WorldMap.save_revision()."""

    def __init__(self):
        self.revision = 0
        self.pending = False
        self.at_ms = 0

    def reveal(self, changed):
        if changed:
            self.pending = True

    def value(self, now_ms):
        if self.pending and now_ms - self.at_ms >= SAVE_REVISION_SECONDS * 1000:
            self.revision += 1
            self.pending = False
            self.at_ms = now_ms
        return self.revision


def pushes(script):
    """script: (second, walking, a save fired this second)"""
    wm, last, count = Revision(), 0, 0
    for t, walking, saved in script:
        wm.reveal(walking)
        if saved:
            rev = wm.value(t * 1000)
            if rev != last:
                last, count = rev, count + 1
    return count


# gain_agility_xp() batches per 1000px walked and ends in
# save_character_state(), which is the save that fires while simply walking.
def walking(minutes):
    return [(t, True, t % 4 == 0) for t in range(minutes * 60)]


half_hour = pushes(walking(30))
check("half an hour of walking costs under a request a minute",
      half_hour <= 40, half_hour)
check("which is more than a tenfold cut on one push per save",
      half_hour * 10 < len([1 for t in range(30 * 60) if t % 4 == 0]),
      [half_hour, len([1 for t in range(30 * 60) if t % 4 == 0])])

check("one minute of walking is one request",
      pushes(walking(1)) == 1, pushes(walking(1)))

# THE IDLE CASE. Saves keep firing while standing in a shop — the map has not
# changed, so it must cost nothing at all. A revision that ticked on a clock
# rather than on a change would push forever.
idle = pushes([(t, False, t % 4 == 0) for t in range(1800)])
check("standing still for half an hour costs nothing", idle == 0, idle)

# THE SHORT WALK. Two seconds of walking is under the interval, so it must not
# be dropped — it has to land on the next save once the interval has passed,
# or a player who takes three steps and stops loses them.
short = pushes([(t, t < 2, t % 4 == 0) for t in range(600)])
check("a two-second walk is still saved, just not immediately",
      short == 1, short)


# =============================================================================
section("THE COLUMN, AGAINST THE REAL SERVER")
# =============================================================================

os.environ.setdefault("ELUSION_OWNER", "mapowner")
_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["ELUSION_DB"] = _db

import app  # noqa: E402

app.app.config["TESTING"] = True
client = app.app.test_client()
registered = client.post("/api/auth/register",
                         json={"username": "mapcheck",
                               "password": "Sufficiently-Long-1"})
H = {"Authorization": "Bearer %s" % registered.get_json()["token"]}


def save(**fields):
    body = {"slot": 0, "class_id": "warrior", "name": "mapper"}
    body.update(fields)
    return client.put("/api/save", headers=H, json=body)


def slot_zero():
    body = client.get("/api/save", headers=H).get_json()
    rows = body if isinstance(body, list) else (body.get("saves")
                                               or body.get("slots") or [])
    for row in rows:
        if row.get("slot") == 0:
            return row
    return {}


REAL = {"elusion": Area((-42, -14), (88, 90)).to_save(), "field": saved}

check("a real two-area map is accepted", save(explored=REAL).status_code == 200)
row = slot_zero()
check("both areas came back", sorted(row.get("explored", {})) == ["elusion", "field"],
      sorted(row.get("explored", {})))
check("and the bits survived the round trip",
      row["explored"]["field"]["bits"] == saved["bits"])
check("as did the origin, which the bits are meaningless without",
      row["explored"]["field"]["ox"] == -16 and row["explored"]["field"]["oy"] == -14,
      row["explored"]["field"])

# THE KEY-PRESENT RULE, the same one equipment and the hotbar ride. Several
# callers write this slot and none of them know where you have walked.
check("a save that says nothing about the map leaves it alone",
      save(area="field").status_code == 200
      and len(slot_zero().get("explored", {})) == 2,
      slot_zero().get("explored"))

# Refusals. There is nothing here to cheat, so these are all about the column
# staying a bounded amount of JSON of a known shape.
for label, payload in [
    ("a map that is a list", ["elusion"]),
    ("an area that is not an object", {"elusion": "everywhere"}),
    ("an area with no size", {"elusion": {"bits": "AAA="}}),
    ("an area of negative size", {"elusion": {"w": -5, "h": 10, "bits": ""}}),
    ("an area larger than any world", {"elusion": {"w": 99999, "h": 99999, "bits": ""}}),
    ("a size that is a bool", {"elusion": {"w": True, "h": 10, "bits": ""}}),
    ("an origin that is a string", {"elusion": {"w": 10, "h": 10, "ox": "left", "bits": ""}}),
]:
    check("%s is refused" % label, save(explored=payload).status_code == 400,
          save(explored=payload).status_code)

# THE CEILING. Not a cheat guard — a storage guard.
huge = {"elusion": {"w": 10, "h": 10, "ox": 0, "oy": 0, "bits": "A" * 70000}}
check("a blob past the ceiling is refused", save(explored=huge).status_code == 400)
check("and not one refusal disturbed what was stored",
      len(slot_zero().get("explored", {})) == 2, slot_zero().get("explored"))

check("forgetting the map is a deliberate act, and allowed",
      save(explored={}).status_code == 200
      and slot_zero().get("explored") == {}, slot_zero().get("explored"))

# Verdict first, housekeeping after, and housekeeping cannot change the verdict
# - see the note at the end of test_healing.py, which is where deleting the
# scratch file before printing the result turned thirty passing checks into a
# failed suite with no summary line at all.
print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)

import gc  # noqa: E402  - wanted only for the teardown below
gc.collect()
try:
    os.unlink(_db)
except OSError as exc:
    print("  note: could not remove %s (%s)" % (_db, exc))

raise SystemExit(1 if failed else 0)
