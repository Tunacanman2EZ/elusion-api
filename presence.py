"""
presence.py - shows players to each other: where they stand, over a WebSocket.

    python presence.py        listens on 127.0.0.1:5001, path /ws/presence

A SECOND PROCESS BESIDE THE API, AND WHY. Until now the server knew which area a
character was in and nothing finer, so nobody could see anybody. Seeing each
other means every game sending where it stands several times a second and
hearing where everyone else does - and the Flask API is the wrong shape for
that: gunicorn runs two workers that share no memory, each request borrows one
of eight threads, and a socket held open for a whole session would hold a
thread with it. This is one small asyncio process that keeps every connection
in one place, and in memory: a position is worth something for a tenth of a
second, so none of it touches the database.

IT NEVER SEES A LOGIN. The game asks the API for a ticket (POST
/api/presence/ticket, behind require_auth), and hands the ticket to this socket.
So every rule about who may sign in - bans, a session ended elsewhere, a stale
token - stays in app.py, where it already is, and this file reads one table the
API writes: presence_tickets. Who a player IS on screen - name, rank, colour,
guild, class, level, and which pets they hold - is in the ticket, written by the
API from its own rows. The game only says where it stands and how it is moving.

A TICKET LIVES TWO MINUTES and the game renews it every minute, so identity
follows a level-up or a new guild. It is tied to the login that asked for it:
every SWEEP_SECONDS this checks that each connection's ticket is in date and its
login still exists, and drops the ones that are not. Logging out, a ban, a kick,
a password change, a login from another computer - all of them delete sessions,
so all of them reach here within seconds without app.py having to know this
file exists.

WHAT IT RELAYS. Position, the body's animation (walking, idle, attacking,
facing), the tank's aura and the pet a player has out - and, since 0.7.0, the
MONSTERS, so everyone in an area fights the same ones. Since game 0.19.0, the
ATTACKS a player makes (a picture of each - see "ATTACKS AND LEVERS" below) and
the LEVERS they pull, so a gate one player opens is open for everyone.

SHARED MONSTERS: ONE GAME RUNS THEM, AND THIS ONLY PASSES THE NOTES. The server
does not simulate a monster; it has no map, no collision and no AI, and growing
those here is E3_SCOPE.md's option C, "a season". Instead each area has a
LEADER - the game that has been in it longest - whose game runs the area's
monsters exactly as a lone player's always has, and says what they are doing
("w", world). Everyone else in the area draws those monsters where the leader
says and sends their hits to the leader ("h"); the leader's game applies them.
When a monster dies, every game that hit it reports its own kill to the API, so
each helper gets their own XP and their own loot bag (the owner's words: "shared
monsters separate loot bags"). The leader leaving hands the area to the next
game in line, which takes the monsters over where they stand.

This file decides only WHO LEADS and WHO HEARS WHAT: a world message from
anybody but the area's leader is dropped, hits go to the leader and nowhere
else, and a game joining the area makes the leader send it everything ("need").
What a world message says is the games' business - see monstersync.gd.

ONLY GAMES THAT SAY THEY CAN. A game that speaks the shared-monster wire says so
in its hello ("v": 2). One that does not (a build from before 0.7.0) is never
made leader and never sent monsters: it fights its own, as before, while still
seeing everybody walk about.

THE WIRE (JSON text frames):

  game -> here
    {"t": "hello", "ticket": "...", "v": 2}              first, within HELLO_SECONDS; "v" 2
                                                         = speaks shared monsters
    {"t": "s", "a": "field", "x": 1471.0, "y": 1090.5,
     "m": "walkdown", "fx": ["ring"], "pet": "petsniper",
     "ts": 81234}                                        where I am, when it changes; "ts" (0.19.0,
                                                         optional) is the game's own clock in ms,
                                                         passed on so others can play it back
                                                         evenly
    {"t": "renew", "ticket": "..."}                      a fresh ticket, every minute
    {"t": "sync"}                                        tell me again who is here (the
                                                         game rebuilt its world: same area,
                                                         new scene, every body gone)
    {"t": "w", "d": {...}, "to": 12}                     LEADER ONLY: the area's monsters, to
                                                         everyone in it, or one game ("to");
                                                         since 0.10.0 "d" may carry "hits", the
                                                         leader's own, for the books
    {"t": "h", "p": [[monster, damage, element], ...]}   a follower's hits, for the leader
    {"t": "x", "e": [[kind, ts, ox, oy, tx, ty, delay, flags], ...]}
                                                         attacks I made (0.19.0): a picture for
                                                         the others, never a hit - see below
    {"t": "l", "n": "ysortworld/interactables/levergatesout", "on": true}
                                                         I pulled a lever (0.19.0)

  here -> game
    {"t": "welcome", "id": 12, "v": 2, "books": true, "x": 1}
                                                         you are in; this server shares monsters,
                                                         and keeps books on them (0.10.0): send
                                                         the world even alone, own hits inside it;
                                                         and passes on attacks and levers ("x")
    {"t": "join", "p": [{id, name, cls, lvl, role, hue, guild, x, y, m, fx, pet, ts}]}
                                                         people now in your area
    {"t": "moves", "p": [[id, x, y, m, fx, pet, ts], ...]}
                                                         who moved this tick, every step each sent
                                                         in it, in order (yourself included; skip
                                                         it); ts -1 for a game that sends none
    {"t": "x", "id": 40, "e": [[kind, ts, ...], ...]}    40's attacks, checked, to everyone else
    {"t": "l", "id": 40, "n": "...", "on": true}         40 pulled a lever, to everyone else
    {"t": "levers", "p": [["...", true], ...]}           (arriving, or sync) the levers pulled in
                                                         this area since it was last empty, the
                                                         latest pulled last
    {"t": "leave", "ids": [12, 40]}                      gone from your area
    {"t": "lead", "a": "field", "id": 12, "n": 2}        who runs this area's monsters, and how
                                                         many others share them (-1: nobody)
    {"t": "need", "id": 40}                              (to the leader) send 40 everything;
                                                         id 0 is the server's books
    {"t": "w", "d": {...}}                               the leader's monsters, passed on as sent
    {"t": "h", "from": 40, "p": [[m, dmg, el], ...]}     (to the leader) 40's hits
    {"t": "bye", "why": "..."}                           and then the socket closes

THE BOOKS (0.10.0). Every world message from a leader and every hit from a
follower is also read into combatbook.py's books on the area's monsters - the
server's own count of each one's health, every hit held to what that character
could do, every spawn held to the area's map, every death judged - and what
they find is written to combat_kills and combat_flags every FLUSH_SECONDS, on a
connection that writes nothing else. NOTHING IN PLAY CHANGES: the relay is
exactly what it was, a book that cannot read a message passes it on anyway,
and kills are still paid by the API as before. E3_SCOPE.md, option C, step 1;
killwatch.py reads the rows. ELUSION_BOOKS=off turns them off.

ATTACKS AND LEVERS (game 0.19.0). The owner, after his first game with
somebody else: "i could not see their attacks but they could see mine the had
to lower the gate to boss". A body's swing was always relayed - it is the
body's animation - but a meteor, a thrown axe, a stick of dynamite or a
healer's orb is a thing in the world, and nothing carried those. Now the game
that makes one says so ("x") and every other game in the area draws a COPY that
cannot hurt anything: the monsters are the leader's, hits still go through "h",
and an attack picture changes no number anywhere. The server checks each one's
shape - a kind it knows, positions in range, the origin near where that player
last said it stood, the target within ATTACK_REACH of it - and passes it on to
the others in the area, never back.

Levers were each game's own: a gate opened on one screen stayed shut on the
next. A pull ("l") is passed to everyone in the area and REMEMBERED for the
area, in the order pulled, so a game that walks in later is told ("levers")
and opens what is open. Forgotten when the area empties, as the monsters are:
the next game in loads the scene afresh. At most MAX_LEVERS_PER_AREA names per
area, each a node path in the scene (LEVER_PATTERN) that each game looks up in
its own copy.

test_presence.py holds every rule here, P-8 the books, P-9 attacks and levers;
test_combatbook.py the books themselves.
"""
import envfile
envfile.load()

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import time

from websockets.asyncio.server import broadcast, serve
from websockets.exceptions import ConnectionClosed

# THE BOOKS ARE OPTIONAL. gamedata.py raises at import without a gamedata.json,
# and the socket must still show players to each other without one.
try:
    import combatbook
    import gamedata
except Exception as exc:  # noqa: BLE001
    combatbook = gamedata = None
    print("[PRESENCE] no books on the monsters: %s" % exc, flush=True)

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ELUSION_DB", os.path.join(HERE, "elusion.db"))
HOST = os.environ.get("ELUSION_PRESENCE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ELUSION_PRESENCE_PORT", "5001") or 5001)
PATH = "/ws/presence"

# How often everyone in an area hears who moved. Ten a second is what the game
# sends at most, and what it smooths between.
TICK_SECONDS = 0.1
# How often every connection's ticket and login are checked. Seconds, not
# minutes: this is how long a banned player stays on other people's screens.
SWEEP_SECONDS = 5.0
# A socket that has not said hello by then is not a game.
HELLO_SECONDS = 5.0
# One state message is under 200 bytes; nothing a game sends is near this -
# except a leader's world message, which may describe every monster in Big
# Field (128 of them) to a game that just walked in.
MAX_MESSAGE_BYTES = 2048
MAX_WORLD_BYTES = 65536
# What the socket itself accepts: the larger of the two. Anything over
# MAX_MESSAGE_BYTES that is not a world message is closed with 1009 by hand.
MAX_SOCKET_BYTES = MAX_WORLD_BYTES
# Messages a connection may send: a bucket of BURST, refilled RATE a second.
# A game sends ten states a second while moving, and a leader ten world
# messages or a follower ten batches of hits beside them; half again on top of
# that is a bug or a script.
#
# 40, not 30, since game 0.19.0: a follower healer fires ten orbs a second, and
# the pictures of them go out batched with the state, ten messages a second on
# top of its ten states and ten batches of hits. 30 was exactly that, with no
# room for a renew.
RATE_PER_SECOND = 40.0
BURST = 80.0
MAX_CONNECTIONS = 1000

# The shared-monster wire. A game says which it speaks in its hello.
SHARED_VERSION = 2
# A game that sends its own player's hits inside its world (0.10.0). An area
# led by an older one cannot be judged by the books.
BOOKS_VERSION = 3
# A follower's hits arrive batched, about ten batches a second. A tank's aura
# touching every monster around it four times a second is the most a game
# sends; this is several times that.
MAX_HITS_PER_MESSAGE = 64
# No single hit in the game comes near this; a number over it is not a hit.
MAX_HIT = 100000
MAX_MONSTER_ID = 2 ** 31 - 1
MAX_ELEMENT = 64

# THE BOOKS (combatbook.py): on when the catalogue has the areas' maps and the
# classes' combat numbers, and nobody switched them off.
BOOKS = (combatbook is not None and bool(gamedata.AREAS) and gamedata.COMBAT_EXPORTED
         and os.environ.get("ELUSION_BOOKS", "on").strip().lower() not in ("off", "0", "no", "false"))
# How often what the books found is written down, and how long it is kept.
FLUSH_SECONDS = 2.0
BOOKS_KEPT_SECONDS = 14 * 86400

# What a body may claim to be doing: the player scenes' own animation names.
ANIM_PATTERN = re.compile(r"^(idle|walk|attack|death|hitflash)(up|down|left|right)$")
AREA_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
# The tank's aura rings, the only effect drawn on the body itself.
EFFECTS = ("ring", "firering")
POSITION_LIMIT = 100000.0
# A game's own clock, in milliseconds since it started (Godot's ticks): what
# "ts" may be. Past this it is not a clock a game has been running.
MAX_CLOCK = 2 ** 42
# The steps one game's state may put in one tick's moves: a game sends ten a
# second and a tick is a tenth, so more than this is a burst, of which the
# newest are kept.
MAX_STEPS_PER_TICK = 4

# ATTACKS (game 0.19.0): the pictures of attacks, and what each may be.
#   slash   a warrior's slash wave: from where it starts, toward (tx, ty)
#   axe     the Double Axe thrown at (tx, ty); flags 2 wide, 4 bloody
#   recall  the Double Axe called back
#   stalag  a mage's stalagmite at (tx, ty)
#   meteor  a Meteorite meteor at (tx, ty), `delay` ms after the cast; flag 1 pulls
#   dyn     a stick of Dynamite from (ox, oy) to (tx, ty), `delay` ms after the throw
#   orb     a healer's orb from (ox, oy) toward (tx, ty)
ATTACK_KINDS = ("slash", "axe", "recall", "stalag", "meteor", "dyn", "orb")
MAX_ATTACKS_PER_MESSAGE = 16
# How far from its origin an attack may land: a cursor across the screen.
ATTACK_REACH = 1000.0
# How far an attack's origin may be from where its player last said it stood:
# a state is a tenth of a second old at most, and nobody runs this far in one.
ATTACK_ORIGIN_SLACK = 400.0
MAX_ATTACK_DELAY_MS = 3000
MAX_ATTACK_FLAGS = 255
# LEVERS (game 0.19.0): a node path in the area's scene, and how many one area
# may remember.
LEVER_PATTERN = re.compile(r"^[A-Za-z0-9_/-]{1,128}$")
MAX_LEVERS_PER_AREA = 32
# Said in the welcome: this server passes attacks and levers on.
RELAY_VERSION = 1


def _hash(raw):
    return hashlib.sha256(str(raw or "").encode("utf-8")).hexdigest()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def lookup_ticket(raw, now=None):
    """(user_id, identity dict, expires_at, slot) for a ticket that is in date
    and whose login still exists, else None. The API wrote it; this only reads."""
    raw = str(raw or "")
    if not raw or len(raw) > 128:
        return None
    now = int(now if now is not None else time.time())
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT p.user_id, p.identity, p.expires_at, p.slot FROM presence_tickets p"
            " JOIN sessions s ON s.token_hash = p.session_hash"
            " WHERE p.token_hash = ? AND p.expires_at > ? AND s.expires_at > ?",
            (_hash(raw), now, now),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        identity = json.loads(row["identity"])
    except (TypeError, ValueError):
        return None
    if not isinstance(identity, dict):
        return None
    return int(row["user_id"]), identity, int(row["expires_at"]), int(row["slot"])


# The two tables the books write, and the only ones this process ever writes:
# app.py creates them (its schema block), and a database from before them is
# not an error - the rows are dropped and the boot log says so once.
_BOOK_TABLES_MISSING = []


def write_books(kills, flags, prune_before=None):
    """Write what the books found: kills as combat_kills rows, flags as
    combat_flags rows ((user_id, area, kind, count, detail, first_at, last_at)),
    and drop rows older than prune_before. Runs in a thread."""
    if not kills and not flags and prune_before is None:
        return
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        if kills:
            conn.executemany(
                "INSERT INTO combat_kills (at, area, enemy_id, origin, leader_id, user_id, slot,"
                " verdict, damage, refused, hp_left, max_hp, seconds)"
                " VALUES (:at, :area, :enemy_id, :origin, :leader_id, :user_id, :slot,"
                " :verdict, :damage, :refused, :hp_left, :max_hp, :seconds)", kills)
        if flags:
            conn.executemany(
                "INSERT INTO combat_flags (user_id, area, kind, count, detail, first_at, last_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)", flags)
        if prune_before is not None:
            conn.execute("DELETE FROM combat_kills WHERE at < ?", (int(prune_before),))
            conn.execute("DELETE FROM combat_flags WHERE last_at < ?", (int(prune_before),))
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        if not _BOOK_TABLES_MISSING:
            _BOOK_TABLES_MISSING.append(str(exc))
            print("[PRESENCE] the books cannot be written (%s): this database is from "
                  "before them - start app.py once to add the tables" % exc, flush=True)
    finally:
        conn.close()


def still_valid(ticket_hashes, now=None):
    """Which of these ticket hashes are in date with their login alive."""
    hashes = list(ticket_hashes)
    if not hashes:
        return set()
    now = int(now if now is not None else time.time())
    conn = _connect()
    try:
        marks = ",".join("?" * len(hashes))
        rows = conn.execute(
            "SELECT p.token_hash FROM presence_tickets p"
            " JOIN sessions s ON s.token_hash = p.session_hash"
            " WHERE p.token_hash IN (%s) AND p.expires_at > ? AND s.expires_at > ?" % marks,
            (*hashes, now, now),
        ).fetchall()
    finally:
        conn.close()
    return {r["token_hash"] for r in rows}


class Player:
    """One connected game."""

    def __init__(self, ws, user_id, ticket_hash, identity, expires_at, slot=-1):
        self.ws = ws
        self.user_id = user_id
        self.ticket_hash = ticket_hash
        self.identity = identity
        self.expires_at = expires_at
        # What the books know of this player: which character, how hard it can
        # hit and how fast it walks (the ticket), where it stands (its states).
        self.fighter = combatbook.Fighter(user_id, slot) if BOOKS else None
        if self.fighter is not None:
            self.fighter.set_bounds(gamedata.combat_bounds(identity), time.time())
        self.area = ""
        self.x = 0.0
        self.y = 0.0
        self.anim = "idledown"
        self.fx = []
        self.pet = ""
        # The game's clock at its newest state (-1: it sends none), and every
        # step since the last tick, for the moves.
        self.clock = -1
        self.steps = []
        self.dirty = False
        self.dropped = False
        self.tokens = BURST
        self.stamp = time.monotonic()
        # The shared-monster wire this game speaks (0: none), and when it
        # walked into its current area - the leader is the earliest.
        self.version = 0
        self.joined_at = 0.0

    @property
    def shares(self):
        return self.version >= SHARED_VERSION

    def allow(self):
        """Token bucket: False once the game sends faster than it ever would."""
        now = time.monotonic()
        self.tokens = min(BURST, self.tokens + (now - self.stamp) * RATE_PER_SECOND)
        self.stamp = now
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True

    def entry(self):
        """Who this is and where, for a join."""
        ident = self.identity
        return {
            "id": self.user_id,
            "name": str(ident.get("name", "")),
            "cls": str(ident.get("cls", "")),
            "lvl": int(ident.get("lvl", 1) or 1),
            "role": str(ident.get("role", "player")),
            "hue": ident.get("hue"),
            "guild": str(ident.get("guild", "")),
            "x": self.x, "y": self.y, "m": self.anim, "fx": list(self.fx), "pet": self.pet,
            "ts": self.clock,
            # Whether this game shares monsters: one that does not cannot be
            # hit by them, so a leader's monsters do not chase it.
            "v": self.version,
        }

    def move(self):
        return [self.user_id, self.x, self.y, self.anim, list(self.fx), self.pet, self.clock]

    def take_steps(self):
        """Every step since the last tick, oldest first - or the newest alone
        when none were kept. A game's ten states a second and the server's ten
        ticks are two clocks, so two states often land in one tick: sending
        only the newest left a hole a fifth of a second wide in what the
        others had to play back, and they lurched across it."""
        steps, self.steps = self.steps, []
        return steps or [self.move()]


def clean_state(msg, allowed_pets):
    """The state a game sent, checked, or None when it is not one. Every field
    has one shape; anything else is a modified client or a bug, and either way
    it is not drawn on somebody else's screen."""
    area = msg.get("a")
    if not isinstance(area, str) or not AREA_PATTERN.match(area):
        return None
    out = {"area": area}
    for key in ("x", "y"):
        value = msg.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if not math.isfinite(value) or abs(value) > POSITION_LIMIT:
            return None
        out[key] = round(value, 1)
    anim = msg.get("m", "idledown")
    if not isinstance(anim, str) or not ANIM_PATTERN.match(anim):
        return None
    out["anim"] = anim
    fx = msg.get("fx", [])
    if not isinstance(fx, list) or len(fx) > len(EFFECTS) or any(f not in EFFECTS for f in fx):
        return None
    out["fx"] = sorted(set(fx))
    # A PET YOU HOLD, OR NONE. The ticket lists the pets the account holds (the
    # character's bag and the bank); a game claiming another one is shown with
    # no pet rather than refused - a summon the server has not heard about yet
    # is more likely than a forgery, and nothing is lost by drawing nothing.
    pet = msg.get("pet", "")
    out["pet"] = pet if isinstance(pet, str) and pet in allowed_pets else ""
    # THE GAME'S CLOCK, optional: a game from before 0.19.0 sends none.
    out["ts"] = -1
    if "ts" in msg:
        clock = clean_clock(msg.get("ts"))
        if clock is None:
            return None
        out["ts"] = clock
    return out


def clean_clock(value):
    """A game's clock in whole milliseconds, or None when it is not one."""
    return _whole(value, 0, MAX_CLOCK)


def _whole(value, low, high):
    """A whole number in [low, high], or None. A whole float is taken as the
    number it is: JSON has no integer type, and Godot may send 3 as 3.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None
        value = int(value)
    return value if low <= value <= high else None


def _spot(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or abs(value) > POSITION_LIMIT:
        return None
    return round(value, 1)


def clean_attacks(msg, at):
    """A game's attacks, checked: [[kind, ts, ox, oy, tx, ty, delay, flags]],
    or None when the message is not that. `at` is where the player last said
    it stood. Each is passed to other players' games to draw, so every field
    has one shape, as a state's does - and the whole message is refused for one
    bad entry, as a batch of hits is."""
    batch = msg.get("e")
    if not isinstance(batch, list) or not batch or len(batch) > MAX_ATTACKS_PER_MESSAGE:
        return None
    out = []
    for attack in batch:
        if not isinstance(attack, list) or len(attack) != 8:
            return None
        kind = attack[0]
        if not isinstance(kind, str) or kind not in ATTACK_KINDS:
            return None
        clock = clean_clock(attack[1])
        spots = [_spot(v) for v in attack[2:6]]
        delay = _whole(attack[6], 0, MAX_ATTACK_DELAY_MS)
        flags = _whole(attack[7], 0, MAX_ATTACK_FLAGS)
        if clock is None or None in spots or delay is None or flags is None:
            return None
        ox, oy, tx, ty = spots
        if math.hypot(ox - at[0], oy - at[1]) > ATTACK_ORIGIN_SLACK:
            return None
        if math.hypot(tx - ox, ty - oy) > ATTACK_REACH:
            return None
        out.append([kind, clock, ox, oy, tx, ty, delay, flags])
    return out


def clean_lever(msg):
    """(name, on) for a lever pull, or None."""
    name = msg.get("n")
    on = msg.get("on")
    if not isinstance(name, str) or not LEVER_PATTERN.match(name) or not isinstance(on, bool):
        return None
    return name, on


def clean_hits(msg):
    """A follower's hits, checked: [[monster id, damage, element], ...], or
    None when it is not that. Whole numbers only, each in range - a hit is
    passed to another player's game, and that game should never have to ask
    whether "damage" is a number."""
    batch = msg.get("p")
    if not isinstance(batch, list) or not batch or len(batch) > MAX_HITS_PER_MESSAGE:
        return None
    out = []
    for hit in batch:
        if not isinstance(hit, list) or len(hit) != 3:
            return None
        if any(isinstance(v, bool) or not isinstance(v, int) for v in hit):
            return None
        monster, damage, element = hit
        if not (0 <= monster <= MAX_MONSTER_ID and 1 <= damage <= MAX_HIT
                and 0 <= element <= MAX_ELEMENT):
            return None
        out.append([monster, damage, element])
    return out


class PresenceServer:
    def __init__(self, sweep_seconds=None, tick_seconds=None):
        self.players = {}     # user_id -> Player
        self.rooms = {}       # area -> set of Player
        self.leaders = {}     # area -> Player running its monsters
        self.levers = {}      # area -> {lever name: on}, the latest pulled last
        self.sweep_seconds = SWEEP_SECONDS if sweep_seconds is None else sweep_seconds
        self.tick_seconds = TICK_SECONDS if tick_seconds is None else tick_seconds
        self.flush_seconds = FLUSH_SECONDS
        self._tasks = []
        self._closing = set()
        # THE BOOKS: one per area that has a map, kept while the process runs.
        self.books = {}       # area -> combatbook.AreaBook
        self._kills = []      # combat_kills rows not yet written
        self._flags = {}      # (user_id, area, kind) -> [count, detail, first_at, last_at]
        self._pruned_at = 0.0
        self._book_errors = 0

    # ---- sending -------------------------------------------------------------

    async def _send(self, player, message):
        try:
            await player.ws.send(json.dumps(message, separators=(",", ":")))
        except ConnectionClosed:
            pass

    def _room_broadcast(self, area, message, skip=None):
        members = [p.ws for p in self.rooms.get(area, ()) if p is not skip]
        if members:
            broadcast(members, json.dumps(message, separators=(",", ":")))

    # ---- rooms -------------------------------------------------------------

    def _leave_room(self, player):
        if not player.area:
            return
        area = player.area
        room = self.rooms.get(area)
        if room is not None:
            room.discard(player)
            if not room:
                del self.rooms[area]
        self._room_broadcast(area, {"t": "leave", "ids": [player.user_id]})
        player.area = ""
        if area not in self.rooms and area in self.books:
            # NOBODY LEFT: the next game in loads the scene afresh.
            self.books[area].emptied(time.time())
        if area not in self.rooms:
            # And its levers with it: a fresh scene has every lever as built.
            self.levers.pop(area, None)
        if self.leaders.get(area) is player:
            # THE NEXT IN LINE TAKES THE MONSTERS OVER, where they stand: its
            # game has been drawing them all along, so it already knows where
            # every one is and how hurt. The earliest arrival, so the order is
            # one anybody could predict.
            del self.leaders[area]
            heirs = [p for p in self.rooms.get(area, ()) if p.shares]
            if heirs:
                self.leaders[area] = min(heirs, key=lambda p: (p.joined_at, p.user_id))
        if player.shares:
            self._announce_lead(area)

    async def _enter_room(self, player, area):
        room = self.rooms.setdefault(area, set())
        others = [p.entry() for p in room if p is not player]
        room.add(player)
        player.area = area
        player.joined_at = time.monotonic()
        if others:
            await self._send(player, {"t": "join", "p": others})
        await self._send_levers(player)
        self._room_broadcast(area, {"t": "join", "p": [player.entry()]}, skip=player)
        if not player.shares:
            return
        leader = self.leaders.get(area)
        if leader is None or leader.area != area:
            self.leaders[area] = player
        self._announce_lead(area)
        self._ask_for_world(area, player)

    # ---- shared monsters ------------------------------------------------------

    def _announce_lead(self, area):
        """Everyone in the area who shares monsters hears who runs them now,
        and how many others share them (the leader sends nothing to nobody)."""
        if not area:
            return
        sharers = [p for p in self.rooms.get(area, ()) if p.shares]
        if not sharers:
            return
        leader = self.leaders.get(area)
        message = {"t": "lead", "a": area, "id": leader.user_id if leader is not None else -1,
                   "n": len(sharers) - 1 if leader is not None else 0}
        broadcast([p.ws for p in sharers], json.dumps(message, separators=(",", ":")))

    def _ask_for_world(self, area, player):
        """The leader sends `player` everything: the monsters as they stand."""
        leader = self.leaders.get(area)
        if leader is None or leader is player or not player.shares:
            return
        broadcast([leader.ws], json.dumps({"t": "need", "id": player.user_id}, separators=(",", ":")))

    # ---- the books (combatbook.py) --------------------------------------------

    def _book(self, area):
        """The books on this area's monsters, or None: books off, or an area
        the catalogue has no map of."""
        if not BOOKS or not area or area not in gamedata.AREAS:
            return None
        book = self.books.get(area)
        if book is None:
            book = self.books[area] = combatbook.AreaBook(area, gamedata.AREAS[area])
        return book

    def _book_safely(self, what, *args):
        """THE BOOKS NEVER BREAK THE RELAY. Whatever a book cannot read, the
        message is still passed on exactly as it would have been."""
        try:
            what(*args)
        except Exception as exc:  # noqa: BLE001
            self._book_errors += 1
            if self._book_errors <= 5 or self._book_errors % 1000 == 0:
                print("[PRESENCE] the books could not read a message (%d so far): %r"
                      % (self._book_errors, exc), flush=True)

    def _book_world(self, player, data):
        book = self._book(player.area)
        if book is None or player.fighter is None:
            return
        now = time.time()
        book.world(player.fighter, data, now)
        if book.needs_full(now):
            # The leader talked about monsters these books never saw (they
            # began after it did): it sends everything to "game 0", which is
            # nobody's - only the books read it.
            broadcast([player.ws], json.dumps({"t": "need", "id": 0}, separators=(",", ":")))

    def _book_hits(self, player, hits):
        book = self._book(player.area)
        if book is None or player.fighter is None:
            return
        now = time.time()
        for monster, damage, _element in hits:
            book.hit(player.fighter, monster, damage, now)

    def relay_world(self, player, raw, msg):
        """A world message from the area's leader, passed on exactly as sent -
        to one game ("to") or to everyone else in the area who shares monsters.
        From anyone else, nothing: a game that was leader a moment ago and has
        not heard yet does not get to move monsters on screens it no longer
        runs."""
        area = player.area
        if not area or self.leaders.get(area) is not player:
            return False
        if not isinstance(msg.get("d"), dict):
            return False
        self._book_safely(self._book_world, player, msg["d"])
        to = msg.get("to")
        room = self.rooms.get(area, ())
        if to is not None:
            if isinstance(to, bool) or not isinstance(to, int):
                return False
            targets = [p.ws for p in room if p.user_id == to and p.shares and p is not player]
        else:
            targets = [p.ws for p in room if p.shares and p is not player]
        if targets:
            broadcast(targets, raw)
        return True

    def relay_hits(self, player, msg):
        """A follower's hits, to the game running the monsters and no other."""
        area = player.area
        leader = self.leaders.get(area) if area else None
        if leader is None or leader is player or not player.shares:
            return False
        hits = clean_hits(msg)
        if hits is None:
            return False
        self._book_safely(self._book_hits, player, hits)
        broadcast([leader.ws], json.dumps({"t": "h", "from": player.user_id, "p": hits},
                                          separators=(",", ":")))
        return True

    # ---- attacks and levers (game 0.19.0) ---------------------------------------

    def relay_attacks(self, player, msg):
        """A player's attacks, checked, to everyone else in the area - never
        back to the player, and never to the books: a picture is not a hit."""
        if not player.area:
            return False
        attacks = clean_attacks(msg, (player.x, player.y))
        if attacks is None:
            return False
        self._room_broadcast(player.area, {"t": "x", "id": player.user_id, "e": attacks}, skip=player)
        return True

    def pull_lever(self, player, msg):
        """A lever pulled: remembered for the area, the latest pulled last,
        and passed to everyone else in it."""
        area = player.area
        pulled = clean_lever(msg)
        if not area or pulled is None:
            return False
        name, on = pulled
        book = self.levers.setdefault(area, {})
        if name not in book and len(book) >= MAX_LEVERS_PER_AREA:
            return False
        # Moved to the end: a game walking in is told them in the order they
        # were pulled, so two levers on one gate end where the last pull left it.
        book.pop(name, None)
        book[name] = on
        self._room_broadcast(area, {"t": "l", "id": player.user_id, "n": name, "on": on}, skip=player)
        return True

    async def _send_levers(self, player):
        book = self.levers.get(player.area)
        if book:
            await self._send(player, {"t": "levers", "p": [[name, on] for name, on in book.items()]})

    async def apply_state(self, player, msg):
        state = clean_state(msg, player.identity.get("pets") or [])
        if state is None:
            return False
        moved_area = state["area"] != player.area
        player.x, player.y = state["x"], state["y"]
        if player.fighter is not None:
            self._book_safely(player.fighter.moved, state["area"], state["x"], state["y"],
                              time.time(), moved_area)
        player.anim, player.fx, player.pet = state["anim"], state["fx"], state["pet"]
        player.clock = state["ts"]
        if moved_area:
            player.steps = []
            self._leave_room(player)
            await self._enter_room(player, state["area"])
            player.dirty = False
        else:
            player.steps.append(player.move())
            if len(player.steps) > MAX_STEPS_PER_TICK:
                del player.steps[0]
            player.dirty = True
        return True

    # ---- the connection ------------------------------------------------------

    async def drop(self, player, why, code=1000):
        if player.dropped:
            return
        player.dropped = True
        await self._send(player, {"t": "bye", "why": why})
        self.forget(player)
        # THE CLOSE IS NOT AWAITED HERE. A close waits for the game's answer,
        # and that answer queues behind whatever the game already sent - which
        # only the handler's own loop drains. A flooding game dropped from
        # inside that loop held its socket open for the whole close timeout,
        # ten seconds, because nothing was reading. The loop keeps reading (and
        # ignoring) until the close completes.
        task = asyncio.create_task(self._close(player.ws, code, why))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    @staticmethod
    async def _close(ws, code, why):
        try:
            await ws.close(code, why)
        except ConnectionClosed:
            pass

    def forget(self, player):
        if self.players.get(player.user_id) is player:
            del self.players[player.user_id]
        self._leave_room(player)

    async def renew(self, player, msg):
        found = await asyncio.to_thread(lookup_ticket, msg.get("ticket"))
        if found is None or found[0] != player.user_id:
            return False
        _uid, identity, expires, slot = found
        changed = identity != player.identity
        if player.fighter is not None and changed:
            # A new weapon, a skill level, a pet: the books' bounds follow.
            player.fighter.slot = slot
            self._book_safely(player.fighter.set_bounds, gamedata.combat_bounds(identity), time.time())
        player.ticket_hash = _hash(msg.get("ticket"))
        player.identity = identity
        player.expires_at = expires
        if changed and player.area:
            # A level, a guild, a colour or a pet list changed: everyone in the
            # area redraws the plate.
            self._room_broadcast(player.area, {"t": "join", "p": [player.entry()]}, skip=player)
            if player.pet and player.pet not in (identity.get("pets") or []):
                player.pet = ""
        return True

    async def handler(self, ws):
        if ws.request is None or ws.request.path.split("?")[0] != PATH:
            await ws.close(1008, "not here")
            return
        if len(self.players) >= MAX_CONNECTIONS:
            await ws.close(1013, "full")
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), HELLO_SECONDS)
            msg = json.loads(raw)
        except (asyncio.TimeoutError, ConnectionClosed, ValueError, TypeError):
            await ws.close(1008, "hello first")
            return
        if not isinstance(msg, dict) or msg.get("t") != "hello":
            await ws.close(1008, "hello first")
            return
        found = await asyncio.to_thread(lookup_ticket, msg.get("ticket"))
        if found is None:
            try:
                await ws.send(json.dumps({"t": "bye", "why": "ticket"}))
            except ConnectionClosed:
                pass
            await ws.close(1008, "ticket")
            return

        user_id, identity, expires, slot = found
        # ONE GAME PER ACCOUNT, as at login: a second connection replaces the
        # first rather than drawing the same player twice.
        old = self.players.get(user_id)
        if old is not None:
            await self.drop(old, "replaced")
        player = Player(ws, user_id, _hash(msg.get("ticket")), identity, expires, slot)
        version = msg.get("v", 0)
        player.version = version if isinstance(version, int) and not isinstance(version, bool) \
            and 0 <= version <= 1000 else 0
        if player.fighter is not None:
            player.fighter.reports_hits = player.version >= BOOKS_VERSION
        self.players[user_id] = player
        await self._send(player, {"t": "welcome", "id": user_id, "v": SHARED_VERSION, "books": BOOKS,
                                  "x": RELAY_VERSION})

        try:
            async for raw in ws:
                if player.dropped:
                    continue
                if not player.allow():
                    await self.drop(player, "too fast", 1008)
                    continue
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue
                kind = message.get("t")
                # ONLY A WORLD MESSAGE MAY BE BIG. The socket takes up to
                # MAX_SOCKET_BYTES so a leader can describe Big Field to a game
                # that walked in; anything else that size is not a game.
                if kind != "w" and len(raw) > MAX_MESSAGE_BYTES:
                    await self.drop(player, "too big", 1009)
                    continue
                if kind == "s":
                    await self.apply_state(player, message)
                elif kind == "renew":
                    await self.renew(player, message)
                elif kind == "sync" and player.area:
                    others = [p.entry() for p in self.rooms.get(player.area, ()) if p is not player]
                    await self._send(player, {"t": "join", "p": others})
                    await self._send_levers(player)
                    # The game rebuilt its world: tell it again who leads, and
                    # have the leader send it the monsters again.
                    if player.fighter is not None:
                        player.fighter.reloaded(time.time())
                    if player.shares:
                        self._announce_lead(player.area)
                        self._ask_for_world(player.area, player)
                elif kind == "w":
                    self.relay_world(player, raw, message)
                elif kind == "h":
                    self.relay_hits(player, message)
                elif kind == "x":
                    self.relay_attacks(player, message)
                elif kind == "l":
                    self.pull_lever(player, message)
        except ConnectionClosed:
            pass
        finally:
            self.forget(player)

    # ---- the clocks ----------------------------------------------------------

    async def tick_loop(self):
        while True:
            await asyncio.sleep(self.tick_seconds)
            self.tick()

    def tick(self):
        for area, room in list(self.rooms.items()):
            movers = [p for p in room if p.dirty]
            if not movers:
                continue
            steps = []
            for p in movers:
                p.dirty = False
                steps.extend(p.take_steps())
            self._room_broadcast(area, {"t": "moves", "p": steps})
        if BOOKS:
            self._book_safely(self.tick_books, time.time())

    def tick_books(self, now):
        """Judge the deaths that have waited, and gather what every book and
        every player's fighter found for the next write."""
        for book in self.books.values():
            book.tick(now)
            kills, flags = book.take()
            self._kills.extend(kills)
            self._gather(flags)
        for player in self.players.values():
            if player.fighter is not None:
                player.fighter.tick(now)
                self._gather(player.fighter.take_flags())

    def _gather(self, flags):
        # ONE ROW PER PLAYER, AREA AND KIND PER WRITE, with a count: a cheat
        # trips the same check hundreds of times a minute.
        for user_id, area, kind, detail, at in flags:
            key = (int(user_id), str(area), str(kind))
            entry = self._flags.get(key)
            if entry is None:
                self._flags[key] = [1, str(detail)[:200], at, at]
            else:
                entry[0] += 1
                entry[1] = str(detail)[:200]
                entry[3] = at

    def take_books(self):
        """(kills, flag rows) gathered since the last call, for write_books()."""
        kills, self._kills = self._kills, []
        flags = [(k[0], k[1], k[2], e[0], e[1], int(e[2]), int(e[3])) for k, e in self._flags.items()]
        self._flags = {}
        return kills, flags

    async def flush_loop(self):
        while True:
            await asyncio.sleep(self.flush_seconds)
            await self.flush_books()

    async def flush_books(self):
        kills, flags = self.take_books()
        now = time.time()
        prune = None
        if now - self._pruned_at >= 3600.0:
            self._pruned_at = now
            prune = now - BOOKS_KEPT_SECONDS
        if not kills and not flags and prune is None:
            return
        try:
            await asyncio.to_thread(write_books, kills, flags, prune)
        except Exception as exc:  # noqa: BLE001
            print("[PRESENCE] could not write the books: %r" % exc, flush=True)

    async def sweep_loop(self):
        while True:
            await asyncio.sleep(self.sweep_seconds)
            await self.sweep()

    async def sweep(self):
        """Drop every connection whose ticket ran out or whose login is gone."""
        players = list(self.players.values())
        if not players:
            return
        valid = await asyncio.to_thread(still_valid, [p.ticket_hash for p in players])
        for p in players:
            if p.ticket_hash not in valid and self.players.get(p.user_id) is p:
                await self.drop(p, "signed out", 1008)

    def start_clocks(self):
        self._tasks = [asyncio.create_task(self.tick_loop()), asyncio.create_task(self.sweep_loop())]
        if BOOKS:
            self._tasks.append(asyncio.create_task(self.flush_loop()))

    def stop_clocks(self):
        for task in self._tasks:
            task.cancel()
        self._tasks = []


async def main():
    server = PresenceServer()
    server.start_clocks()
    async with serve(server.handler, HOST, PORT, max_size=MAX_SOCKET_BYTES,
                     ping_interval=20, ping_timeout=20) as ws_server:
        print("[PRESENCE] listening on ws://%s:%d%s (database %s)" % (HOST, PORT, PATH, DB_PATH), flush=True)
        print("[PRESENCE] books on the monsters: %s" % (
            "on, %d areas mapped" % len(gamedata.AREAS) if BOOKS else "off"), flush=True)
        await ws_server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
