"""
combatbook.py - the server's own count of every monster's health (0.10.0).

E3_SCOPE.md, option C, STEP 1: THE SERVER WATCHES. Until now the server took a
game's word that a monster died (E-3): /api/combat/kill rolls the rewards and
caps how many kills a minute can claim, but nothing on this side ever saw the
fight. Since 0.7.0 every area's monsters run on ONE game, the area's leader,
which tells presence.py what they are doing ("w"), and every other game's hits
travel through presence.py to it ("h"). So the notes for the whole fight
already pass through this server. This module reads them and keeps books:

  * EVERY MONSTER, by the leader's id for it: what it is (its EnemyData, read
    from the record the leader sent), where it was authored (its origin, the
    path the area scene gives it), where it stands, and THE HEALTH THE SERVER
    COUNTS - the catalogue's max_hp, less every hit the server has seen.
  * EVERY HIT, from every game, the leader's own included (0.10.0 games send
    theirs inside "w"): held to what that character could really do - its
    biggest hit and its damage a second, from what it holds and its skills
    (gamedata.combat_bounds()), with room to spare (SLACK).
  * EVERY SPAWN, against the area's map (gamedata.AREAS): a monster the map
    does not hold at that spot, a different monster at it, one back sooner than
    the area's respawner allows, two at one spot, a small slime no split
    released, a twin no large slime made.
  * EVERY DEATH, held a moment for hits still on their way (HOLD_SECONDS), then
    judged: AGREED (the server's count reached zero too), SHORT (the leader
    said it died with health the server still counted), or NOT DUE (it should
    never have been there). One row per player who hit it, with what they did
    - which is what a kill report is matched against (killwatch.py).

AND NOTHING IN PLAY CHANGES. Every kill is still paid exactly as before; this
only writes down what it saw (presence.py, the writer: combat_kills and
combat_flags). The point of the first week is to learn whether honest play
ever trips a check before any check is allowed to refuse anything - the E-2
interim skill bound, picked without data, is what a guess costs. Step 2 makes
a kill need an AGREED row to be paid.

WHAT IS NOT CHECKED, and why:
  * Walking out of an area and back in brings every monster back, because the
    game reloads the scene - honest play has always done it, so a reload
    ("reset", within RESET_GRACE of the leader arriving or saying "sync") is
    not judged against the respawn clock. One that comes at any other time is.
  * How fast a monster moves, and whether it is gated. The leader is still
    trusted with where monsters are (presence.py's header), and a gated
    boss's hits are refused by every game before they are sent.
  * A hit from further away than REACH is written down, never refused: a pet's
    arrow flies ten seconds, and nobody has measured yet how far honest hits
    land. The watch week is how that gets measured.

PURE: no sockets, no database, no clock of its own - every call is given
`now`, so test_combatbook.py drives it to the hundredth of a second.
"""
import math

import gamedata

# Room on every bound while the server only watches. A bound is the most the
# game's own arithmetic allows (gamedata.combat_bounds()); the room is for
# whatever that arithmetic missed. Measured, then tightened, in step 2.
SLACK = 1.5
# How much damage a character may put into one monster at once: this many
# seconds of its rate. A mage's two meteors, a tank's two sticks, a warrior's
# swing and wave all land in the same instant.
BURST_SECONDS = 3.0
# A hit landing further than this from the player who made it is written down.
# Beyond any screen: the camera shows 427 x 240 pixels of the world at 1280 x
# 720, a little more on a larger monitor.
REACH = 1200.0
# A death waits this long for hits still on their way: two players' hits on
# one monster cross in the air, and the second helped kill it too.
HOLD_SECONDS = 1.0
# A death the books leave this much health on, as a share of the monster's
# maximum, still agrees.
AGREED_LEFT = 0.02
# A large slime splits at half its health (poisonslime.gd split_hp_ratio).
SPLIT_AT = 0.55
# How long the smalls a split released may take to appear (the large flashes
# for hitflash_duration first), and a twin after its large.
ALLOWANCE_SECONDS = 10.0
# Seconds early a respawn may come and still be on time: the respawner's clock
# and this one are not the same clock.
RESPAWN_SLACK = 2.0
# A scene reload is honest within this long of the leader arriving in the
# area or saying it rebuilt its world ("sync").
RESET_GRACE = 10.0
# A hit over the per-hit bound waits this long for a fresh ticket before it is
# called too big: an equip is answered by the API first and the ticket the
# game renews right after reaches here a moment later.
PENDING_SECONDS = 3.0
# A step of the player's this long is a jump (a teleporter, "go to"), not a
# walk. Walking speed is the distance over the last SPEED_WINDOW seconds of
# states: long enough that the network holding a few back and delivering them
# together cannot read as a burst of speed.
STEP_JUMP = 400.0
SPEED_WINDOW = 3.0
# How long an area nobody is in keeps its living monsters: a leader whose link
# dropped and came straight back (every change of area opens a new socket, and
# a socket can drop) carries on with the books' count, not a fresh one.
EMPTY_KEEP = 30.0
# How much a book holds, whatever a leader says.
MAX_MONSTERS = 1000

VERDICTS = ("agreed", "short", "not_due")
# Every kind of flag, and whose it is: a hitter's, a walker's or a leader's.
FLAG_KINDS = {
    "hit_too_big": "one hit larger than the character could land",
    "too_fast": "more damage a second than the character could deal",
    "too_far": "a hit from further away than any screen",
    "too_quick": "walking faster than the character can",
    "jumped": "a single step the length of a teleport",
    "unknown_monster": "a monster the catalogue does not have",
    "not_on_map": "a monster at a spot the area's map does not hold",
    "wrong_monster": "a different monster at an authored spot",
    "max_hp_differs": "a monster with a different maximum health",
    "back_too_soon": "a monster back sooner than the area's respawn",
    "unaccounted_spawn": "a monster no spawn, split or twin explains",
    "split_early": "a large slime split while it still had over half its health",
}


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _whole(value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not (low <= value <= high):
        return None
    return value


def clean_hit_list(batch, limit=64, max_hit=100000):
    """[[monster, damage, element], ...] with every number a whole number in
    range, or None. The same rule as presence.clean_hits, for the leader's own
    hits inside its world message; an empty list is allowed here."""
    if not isinstance(batch, list) or len(batch) > limit:
        return None
    out = []
    for hit in batch:
        if not isinstance(hit, list) or len(hit) != 3:
            return None
        monster = _whole(hit[0], 0, 2 ** 31 - 1)
        damage = _whole(hit[1], 1, max_hit)
        element = _whole(hit[2], 0, 64)
        if monster is None or damage is None or element is None:
            return None
        out.append((monster, damage, element))
    return out


class Fighter:
    """One player as the books see them: who, which character, how hard they
    can hit and how fast they walk, where they stand, and their rate buckets.
    presence.py keeps one on every Player."""

    def __init__(self, user_id, slot=-1):
        self.user_id = int(user_id)
        self.slot = int(slot)
        self.bounds = None
        self.area = ""
        self.x = None
        self.y = None
        self.buckets = {}       # (area, monster id) -> [tokens, stamp]
        self.scene_at = -1e9    # when this game last loaded a scene (an area, a "sync")
        # Whether its game sends its own hits inside its world (a 0.10.0 game,
        # hello "v" 3). An area led by one that does not cannot be judged: its
        # player's hits never reach the books.
        self.reports_hits = True
        self.pending = []       # [at, book, monster id, damage, booked]
        self.flags = []         # (user_id, area, kind, detail, at)
        self._walk = []         # [(at, x, y)] since the last jump
        self._walked = 0.0
        self._walk_flagged_at = -1e9

    # ---- who they are --------------------------------------------------------

    def set_bounds(self, bounds, now):
        """A new ticket (a renewal): new bounds. A hit waiting to be called too
        big that fits the new ones is booked in full instead."""
        self.bounds = bounds
        if not bounds or not self.pending:
            return
        cap = bounds["max_hit"] * SLACK
        keep = []
        for entry in self.pending:
            at, book, monster_id, damage, booked = entry
            if damage <= cap:
                book.credit(self, monster_id, damage - booked)
            else:
                keep.append(entry)
        self.pending = keep

    # ---- where they are ------------------------------------------------------

    def reloaded(self, now):
        """The game rebuilt its world in the same area ("sync"): a scene loaded,
        and the next position starts a fresh walk."""
        self.scene_at = now
        self.x = self.y = None

    def moved(self, area, x, y, now, fresh=False):
        """A state from the game. `fresh` is a new scene - another area, or the
        same one rebuilt - where nothing about the last position counts."""
        if fresh or area != self.area or self.x is None:
            if fresh or area != self.area:
                self.scene_at = now
            self.area, self.x, self.y = area, x, y
            self._walk = [(now, x, y)]
            self._walked = 0.0
            return
        step = math.hypot(x - self.x, y - self.y)
        last_at = self._walk[-1][0] if self._walk else now
        gap = now - last_at
        speed = (self.bounds or {}).get("speed") or 0.0
        self.x, self.y = x, y
        if step > max(STEP_JUMP, speed * SLACK * min(gap, 1.0) + 100.0):
            self.flags.append((self.user_id, area, "jumped", "%d px in one step" % step, now))
            self._walk = [(now, x, y)]
            self._walked = 0.0
            return
        self._walk.append((now, x, y))
        self._walked += step
        while len(self._walk) > 2 and now - self._walk[1][0] >= SPEED_WINDOW:
            first, second = self._walk[0], self._walk[1]
            self._walked -= math.hypot(second[1] - first[1], second[2] - first[2])
            self._walk.pop(0)
        span = now - self._walk[0][0]
        if speed and span >= SPEED_WINDOW and self._walked / span > speed * SLACK \
                and now - self._walk_flagged_at >= SPEED_WINDOW:
            self._walk_flagged_at = now
            self.flags.append((self.user_id, area, "too_quick",
                               "%d px/s, the most is %d" % (self._walked / span, speed), now))

    # ---- the clock -------------------------------------------------------------

    def tick(self, now):
        keep = []
        for entry in self.pending:
            at, book, monster_id, damage, booked = entry
            if now - at >= PENDING_SECONDS:
                most = int((self.bounds or {}).get("max_hit", 0))
                self.flags.append((self.user_id, book.area, "hit_too_big",
                                   "%d, the most is %d" % (damage, most), now))
            else:
                keep.append(entry)
        self.pending = keep
        if len(self.buckets) > 64:
            for key in [k for k, (_t, stamp) in self.buckets.items() if now - stamp > 10.0]:
                del self.buckets[key]

    def take_flags(self):
        out, self.flags = self.flags, []
        return out


class Monster:
    __slots__ = ("id", "enemy_id", "origin", "max_hp", "hp", "x", "y", "born", "not_due",
                 "damage", "refused", "slots", "dead_at", "rewards", "splitter", "twins_left")

    def __init__(self, monster_id, enemy_id, origin, max_hp, hp, x, y, born):
        self.id = monster_id
        self.enemy_id = enemy_id
        self.origin = origin
        self.max_hp = max_hp
        self.hp = hp
        self.x = x
        self.y = y
        self.born = born
        self.not_due = False
        self.damage = {}        # user id -> damage the books took from them
        self.refused = {}       # user id -> damage over their rate
        self.slots = {}         # user id -> the character that hit
        self.dead_at = None
        self.rewards = True
        self.splitter = None    # (smalls, what they are) for a large slime
        self.twins_left = 0


class AreaBook:
    """The books on one area's monsters, kept from what its leader says."""

    def __init__(self, area, spec, catalogue=gamedata):
        self.area = area
        self.respawn = float(spec.get("respawn_seconds", 0.0) or 0.0)
        self.spawns = {}
        for spot in spec.get("spawns") or []:
            if isinstance(spot, dict) and isinstance(spot.get("o"), str):
                self.spawns[spot["o"]] = spot
        self.enemies = catalogue.ENEMIES
        self.by_resource = {str(row.get("resource", "")): row for row in self.enemies.values()
                            if row.get("resource")}
        self.monsters = {}      # leader's id -> Monster, alive
        self.dying = {}         # leader's id -> Monster, dead and held
        self.deaths = {}        # origin -> when its monster last died
        self.smalls = []        # [at, enemy id] a split released, not yet seen
        self.leader_id = None
        self.led_since = 0.0
        self.leader_reports = True
        self.reset_by = None    # the leader whose reset is being read
        self._before = {}       # the living, while a reset is read: same id, same spot, same monster
        self.empty_since = None
        self.want_full = False
        self.asked_at = -1e9
        self.kills = []
        self.flags = []
        self.stray = 0

    # ---- what the leader says ------------------------------------------------

    def world(self, leader, d, now):
        """A world message from the area's leader (a Fighter), already known to
        be the leader's. Its own hits first - they happened before the deaths
        they caused - then what changed."""
        self.empty_since = None
        if leader.user_id != self.leader_id:
            self.leader_id = leader.user_id
            self.led_since = now
        self.leader_reports = bool(getattr(leader, "reports_hits", True))
        hits = clean_hit_list(d.get("hits", []))
        for monster_id, damage, _element in hits or ():
            self.hit(leader, monster_id, damage, now)
        if d.get("full") is True:
            reset = d.get("reset") is True
            self.reset_by = leader
            part = _whole(d.get("part", 0), 0, 10 ** 6)
            parts = _whole(d.get("parts", 1), 1, 10 ** 6)
            if reset and part == 0:
                self._before, self.monsters = self.monsters, {}
                self.smalls = []
                self.want_full = False
            spawn = d.get("spawn")
            for record in spawn if isinstance(spawn, list) else ():
                self._record(record, now, "reset" if reset else "full")
            last = part is not None and parts is not None and part == parts - 1
            if last:
                self._before = {}
                if not reset:
                    self.want_full = False
        events = d.get("ev")
        for event in events if isinstance(events, list) else ():
            if not isinstance(event, dict):
                continue
            kind = event.get("k")
            if kind == "spawn":
                self._record(event.get("r"), now, "spawn")
            elif kind == "die":
                self._died(event.get("id"), now)
            elif kind == "gone":
                self._gone(event.get("id"))
        snap = d.get("snap")
        for row in snap if isinstance(snap, list) else ():
            if not isinstance(row, list) or len(row) != 5:
                continue
            m = self.monsters.get(row[0]) if isinstance(row[0], int) else None
            if m is None:
                self.want_full = True
                continue
            x, y = _num(row[1]), _num(row[2])
            if x is not None and y is not None:
                m.x, m.y = x, y

    def needs_full(self, now):
        """True (once every few seconds) when the leader has talked about a
        monster these books never saw - a server restart, a link that came
        back - and should be asked for everything."""
        if self.want_full and now - self.asked_at >= 3.0:
            self.asked_at = now
            return True
        return False

    def _flag(self, user_id, kind, detail, now):
        self.flags.append((user_id, self.area, kind, detail, now))

    def _record(self, record, now, kind):
        if not isinstance(record, dict):
            return
        monster_id = _whole(record.get("id"), 0, 2 ** 31 - 1)
        if monster_id is None:
            return
        props = record.get("p") if isinstance(record.get("p"), dict) else {}
        x, y = _num(record.get("x")), _num(record.get("y"))
        known = self.monsters.get(monster_id)
        if known is not None:
            if x is not None and y is not None:
                known.x, known.y = x, y
            # A NEW LEADER'S FIRST WORD ON A MONSTER: its game applied hits
            # the old leader's may not have sent before it went. Its count is
            # taken where it is lower; never where it is higher.
            hp = _whole(record.get("hp"), 0, 10 ** 9)
            if kind == "full" and hp is not None and now - self.led_since <= 5.0:
                known.hp = min(known.hp, hp)
            return
        if kind == "full" and monster_id in self.dying:
            return
        if kind == "reset":
            # A RESET OF MONSTERS THE BOOKS ALREADY HAD - the same id, at the
            # same spot, the same monster - is the leader's link coming back,
            # not a scene loading: they carry on as counted.
            before = self._before.pop(monster_id, None)
            props_ed = str(props.get("ed", ""))
            if before is not None and before.origin == (record.get("o") if isinstance(record.get("o"), str) else "") \
                    and self.by_resource.get(props_ed, {}).get("enemy_id") == before.enemy_id:
                if x is not None and y is not None:
                    before.x, before.y = x, y
                # Taken where the leader's count is lower - its own hits while
                # the link was down never reached the books - never higher.
                hp = _whole(record.get("hp"), 0, 10 ** 9)
                if hp is not None:
                    before.hp = min(before.hp, hp)
                # Whether it may still make a twin is the record's to say: a
                # scene loaded afresh has its larges whole again.
                before.twins_left = 1 if before.splitter and before.origin and not props.get("du") else 0
                self.monsters[monster_id] = before
                return
        if len(self.monsters) >= MAX_MONSTERS:
            return
        leader = self.leader_id if self.leader_id is not None else 0
        origin = record.get("o") if isinstance(record.get("o"), str) else ""
        enemy = self.by_resource.get(str(props.get("ed", "")))
        sent_max = _whole(record.get("mh"), 1, 10 ** 9)
        if enemy is None:
            name = str(props.get("ed", "")).rsplit("/", 1)[-1].replace(".tres", "")[:64]
            m = Monster(monster_id, name or "?", origin, sent_max or 1, sent_max or 1,
                        x or 0.0, y or 0.0, now)
            m.not_due = True
            self._flag(leader, "unknown_monster", "%r" % str(props.get("ed", ""))[:80], now)
        else:
            max_hp = max(1, int(enemy.get("max_hp", 1) or 1))
            m = Monster(monster_id, str(enemy["enemy_id"]), origin, max_hp, max_hp,
                        x or 0.0, y or 0.0, now)
            m.rewards = bool(enemy.get("grants_rewards", True))
            if int(enemy.get("split_count", 0) or 0) > 0 and enemy.get("splits_into"):
                m.splitter = (int(enemy["split_count"]), str(enemy["splits_into"]))
            if sent_max is not None and sent_max != max_hp:
                self._flag(leader, "max_hp_differs", "%s %d, the catalogue %d"
                           % (m.enemy_id, sent_max, max_hp), now)
            if kind == "full":
                # JOINED PART-WAY (the books started after the leader did):
                # its health is the leader's word, the only one there is.
                hp = _whole(record.get("hp"), 0, 10 ** 9)
                m.hp = min(max_hp, hp) if hp is not None else max_hp
            self._judge_spawn(m, enemy, props, now, kind)
        self.monsters[monster_id] = m

    def _judge_spawn(self, m, enemy, props, now, kind):
        leader = self.leader_id if self.leader_id is not None else 0
        if m.origin:
            spot = self.spawns.get(m.origin)
            if spot is None:
                m.not_due = True
                self._flag(leader, "not_on_map", "%s at %r" % (m.enemy_id, m.origin[:80]), now)
                return
            if str(spot.get("e", "")) != m.enemy_id:
                m.not_due = True
                self._flag(leader, "wrong_monster", "%s where the map has %s"
                           % (m.enemy_id, spot.get("e", "")), now)
                return
            if any(other.origin == m.origin for other in self.monsters.values()):
                m.not_due = True
                self._flag(leader, "unaccounted_spawn", "a second %s at one spot" % m.enemy_id, now)
                return
            # A RESET IS A SCENE LOADING when it comes soon after the leader's
            # game loaded one (arrived, or said "sync"); any other is judged.
            loaded = self.reset_by.scene_at if self.reset_by is not None else -1e9
            judged = kind == "spawn" or (kind == "reset" and now - loaded > RESET_GRACE)
            died = self.deaths.get(m.origin)
            if judged and died is not None and now - died < self.respawn - RESPAWN_SLACK:
                m.not_due = True
                self._flag(leader, "back_too_soon", "%s after %.1f s, the respawn is %d s"
                           % (m.enemy_id, now - died, self.respawn), now)
            if m.splitter and not props.get("du"):
                m.twins_left = 1
            return
        if kind == "full":
            # Made before these books began: nothing to check it against.
            return
        if m.splitter and props.get("du") is True and not props.get("sm"):
            parent = self._twin_parent(m, now)
            if parent is None:
                m.not_due = True
                self._flag(leader, "unaccounted_spawn", "a %s twin no large made" % m.enemy_id, now)
            return
        self.smalls = [s for s in self.smalls if now - s[0] <= ALLOWANCE_SECONDS]
        for i, (_at, small) in enumerate(self.smalls):
            if small == m.enemy_id:
                del self.smalls[i]
                return
        m.not_due = True
        self._flag(leader, "unaccounted_spawn", "a %s from nowhere" % m.enemy_id, now)

    def _twin_parent(self, twin, now):
        best = None
        for other in self.monsters.values():
            if other.enemy_id == twin.enemy_id and other.twins_left > 0:
                if best is None or math.hypot(other.x - twin.x, other.y - twin.y) \
                        < math.hypot(best.x - twin.x, best.y - twin.y):
                    best = other
        if best is not None:
            best.twins_left -= 1
        return best

    def _died(self, monster_id, now):
        m = self.monsters.pop(monster_id, None) if isinstance(monster_id, int) else None
        if m is None:
            self.stray += 1
            if isinstance(monster_id, int) and monster_id not in self.dying:
                self.want_full = True
            return
        m.dead_at = now
        if m.origin in self.spawns:
            self.deaths[m.origin] = now
        if m.splitter:
            # A LARGE SLIME'S "DEATH" IS ITS SPLIT (poisonslime.gd emits died
            # as it begins), and only one at half its health or less may.
            smalls, kind = m.splitter
            if m.hp <= m.max_hp * SPLIT_AT:
                self.smalls.extend([now, kind] for _ in range(smalls))
            elif self.leader_reports:
                self._flag(self.leader_id or 0, "split_early", "%s at %d of %d"
                           % (m.enemy_id, max(0, int(m.hp)), m.max_hp), now)
        if not self.leader_reports:
            # A LEADER FROM BEFORE 0.10.0 never sent its own player's hits, so
            # nothing it killed can be judged: no row, rather than a false SHORT.
            if m.splitter:
                smalls, kind = m.splitter
                self.smalls.extend([now, kind] for _ in range(smalls))
            return
        self.dying[monster_id] = m

    def _gone(self, monster_id):
        if isinstance(monster_id, int):
            self.monsters.pop(monster_id, None)

    def emptied(self, now):
        """Nobody is left in the area. The next game in loads the scene again,
        so after EMPTY_KEEP the living go - kept that long for a leader whose
        link only dropped. Deaths stay: a spot's clock outlives its visitors."""
        self.empty_since = now
        self.smalls = []
        self.leader_id = None

    # ---- hits ----------------------------------------------------------------

    def hit(self, fighter, monster_id, damage, now):
        """One hit from one player. Booked, held to their bounds, and counted
        as theirs."""
        m = self.monsters.get(monster_id)
        late = False
        if m is None:
            m = self.dying.get(monster_id)
            late = True
        if m is None:
            self.stray += 1
            return
        bounds = fighter.bounds
        if fighter.area == self.area and fighter.x is not None:
            far = math.hypot(m.x - fighter.x, m.y - fighter.y)
            if far > REACH:
                fighter.flags.append((fighter.user_id, self.area, "too_far",
                                      "%d px from %s" % (far, m.enemy_id), now))
        booked = damage
        if bounds:
            cap = bounds["max_hit"] * SLACK
            if damage > cap:
                booked = int(cap)
                fighter.pending.append([now, self, monster_id, damage, booked])
            rate = bounds["dps"] * SLACK
            key = (self.area, monster_id)
            bucket = fighter.buckets.get(key)
            if bucket is None:
                bucket = fighter.buckets[key] = [rate * BURST_SECONDS, now]
            bucket[0] = min(rate * BURST_SECONDS, bucket[0] + (now - bucket[1]) * rate)
            bucket[1] = now
            over = max(0, booked - int(bucket[0]))
            bucket[0] = max(0.0, bucket[0] - booked)
            if over:
                booked -= over
                m.refused[fighter.user_id] = m.refused.get(fighter.user_id, 0) + over
                fighter.flags.append((fighter.user_id, self.area, "too_fast",
                                      "%d over in one hit on %s" % (over, m.enemy_id), now))
        m.slots[fighter.user_id] = fighter.slot
        m.damage[fighter.user_id] = m.damage.get(fighter.user_id, 0) + booked
        if not late:
            m.hp -= booked

    def credit(self, fighter, monster_id, extra):
        """The rest of a hit a fresh ticket showed was possible after all."""
        if extra <= 0:
            return
        m = self.monsters.get(monster_id)
        if m is not None:
            m.hp -= extra
        else:
            m = self.dying.get(monster_id)
        if m is not None:
            m.damage[fighter.user_id] = m.damage.get(fighter.user_id, 0) + extra

    # ---- the clock -------------------------------------------------------------

    def tick(self, now):
        """Judge every death that has waited long enough for its late hits."""
        for monster_id in [i for i, m in self.dying.items() if now - m.dead_at >= HOLD_SECONDS]:
            m = self.dying.pop(monster_id)
            if not m.rewards:
                continue
            if m.not_due:
                verdict = "not_due"
            elif m.hp <= m.max_hp * AGREED_LEFT:
                verdict = "agreed"
            else:
                verdict = "short"
            helpers = sorted(set(m.damage) | set(m.refused))
            if not helpers:
                helpers = [self.leader_id or 0]
            for user_id in helpers:
                self.kills.append({
                    "at": int(m.dead_at), "area": self.area, "enemy_id": m.enemy_id,
                    "origin": m.origin, "leader_id": int(self.leader_id or 0),
                    "user_id": int(user_id), "slot": int(m.slots.get(user_id, -1)),
                    "verdict": verdict, "damage": int(m.damage.get(user_id, 0)),
                    "refused": int(m.refused.get(user_id, 0)),
                    "hp_left": max(0, int(math.ceil(m.hp))), "max_hp": int(m.max_hp),
                    "seconds": round(m.dead_at - m.born, 2),
                })
        if self.empty_since is not None and now - self.empty_since >= EMPTY_KEEP:
            self.monsters.clear()
            self.empty_since = None
        cutoff = now - max(self.respawn * 2.0, 60.0)
        for origin in [o for o, at in self.deaths.items() if at < cutoff]:
            del self.deaths[origin]

    def take(self):
        """(kills, flags) found since the last call."""
        kills, flags = self.kills, self.flags
        self.kills, self.flags = [], []
        return kills, flags
