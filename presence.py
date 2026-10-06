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

WHAT IT RELAYS, AND WHAT IT DOES NOT. Position, the body's animation (walking,
idle, attacking, facing), the tank's aura and the pet a player has out. Not
damage, not enemies: each game still fights its own enemies, so a player swinging
at something you cannot see is swinging at their own monster. This is presence,
not combat (see E3_SCOPE.md and "Decided, not built: PvP and the world boss").

THE WIRE (JSON text frames):

  game -> here
    {"t": "hello", "ticket": "..."}                      first, within HELLO_SECONDS
    {"t": "s", "a": "field", "x": 1471.0, "y": 1090.5,
     "m": "walkdown", "fx": ["ring"], "pet": "petsniper"}   where I am, when it changes
    {"t": "renew", "ticket": "..."}                      a fresh ticket, every minute
    {"t": "sync"}                                        tell me again who is here (the
                                                         game rebuilt its world: same area,
                                                         new scene, every body gone)

  here -> game
    {"t": "welcome", "id": 12}                           you are in
    {"t": "join", "p": [{id, name, cls, lvl, role, hue, guild, x, y, m, fx, pet}]}
                                                         people now in your area
    {"t": "moves", "p": [[id, x, y, m, fx, pet], ...]}   who moved this tick (yourself included; skip it)
    {"t": "leave", "ids": [12, 40]}                      gone from your area
    {"t": "bye", "why": "..."}                           and then the socket closes

test_presence.py holds every rule here.
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
# One state message is under 200 bytes; nothing a game sends is near this.
MAX_MESSAGE_BYTES = 2048
# Messages a connection may send: a bucket of BURST, refilled RATE a second.
# The game sends ten a second while moving; double that is a bug or a script.
RATE_PER_SECOND = 20.0
BURST = 40.0
MAX_CONNECTIONS = 1000

# What a body may claim to be doing: the player scenes' own animation names.
ANIM_PATTERN = re.compile(r"^(idle|walk|attack|death|hitflash)(up|down|left|right)$")
AREA_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
# The tank's aura rings, the only effect drawn on the body itself.
EFFECTS = ("ring", "firering")
POSITION_LIMIT = 100000.0


def _hash(raw):
    return hashlib.sha256(str(raw or "").encode("utf-8")).hexdigest()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def lookup_ticket(raw, now=None):
    """(user_id, identity dict, expires_at) for a ticket that is in date and
    whose login still exists, else None. The API wrote it; this only reads."""
    raw = str(raw or "")
    if not raw or len(raw) > 128:
        return None
    now = int(now if now is not None else time.time())
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT p.user_id, p.identity, p.expires_at FROM presence_tickets p"
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
    return int(row["user_id"]), identity, int(row["expires_at"])


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

    def __init__(self, ws, user_id, ticket_hash, identity, expires_at):
        self.ws = ws
        self.user_id = user_id
        self.ticket_hash = ticket_hash
        self.identity = identity
        self.expires_at = expires_at
        self.area = ""
        self.x = 0.0
        self.y = 0.0
        self.anim = "idledown"
        self.fx = []
        self.pet = ""
        self.dirty = False
        self.dropped = False
        self.tokens = BURST
        self.stamp = time.monotonic()

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
        }

    def move(self):
        return [self.user_id, self.x, self.y, self.anim, list(self.fx), self.pet]


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
    return out


class PresenceServer:
    def __init__(self, sweep_seconds=None, tick_seconds=None):
        self.players = {}     # user_id -> Player
        self.rooms = {}       # area -> set of Player
        self.sweep_seconds = SWEEP_SECONDS if sweep_seconds is None else sweep_seconds
        self.tick_seconds = TICK_SECONDS if tick_seconds is None else tick_seconds
        self._tasks = []
        self._closing = set()

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
        room = self.rooms.get(player.area)
        if room is not None:
            room.discard(player)
            if not room:
                del self.rooms[player.area]
        self._room_broadcast(player.area, {"t": "leave", "ids": [player.user_id]})
        player.area = ""

    async def _enter_room(self, player, area):
        room = self.rooms.setdefault(area, set())
        others = [p.entry() for p in room if p is not player]
        room.add(player)
        player.area = area
        if others:
            await self._send(player, {"t": "join", "p": others})
        self._room_broadcast(area, {"t": "join", "p": [player.entry()]}, skip=player)

    async def apply_state(self, player, msg):
        state = clean_state(msg, player.identity.get("pets") or [])
        if state is None:
            return False
        moved_area = state["area"] != player.area
        player.x, player.y = state["x"], state["y"]
        player.anim, player.fx, player.pet = state["anim"], state["fx"], state["pet"]
        if moved_area:
            self._leave_room(player)
            await self._enter_room(player, state["area"])
            player.dirty = False
        else:
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
        _uid, identity, expires = found
        changed = identity != player.identity
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

        user_id, identity, expires = found
        # ONE GAME PER ACCOUNT, as at login: a second connection replaces the
        # first rather than drawing the same player twice.
        old = self.players.get(user_id)
        if old is not None:
            await self.drop(old, "replaced")
        player = Player(ws, user_id, _hash(msg.get("ticket")), identity, expires)
        self.players[user_id] = player
        await self._send(player, {"t": "welcome", "id": user_id})

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
                if kind == "s":
                    await self.apply_state(player, message)
                elif kind == "renew":
                    await self.renew(player, message)
                elif kind == "sync" and player.area:
                    others = [p.entry() for p in self.rooms.get(player.area, ()) if p is not player]
                    await self._send(player, {"t": "join", "p": others})
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
            for p in movers:
                p.dirty = False
            self._room_broadcast(area, {"t": "moves", "p": [p.move() for p in movers]})

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

    def stop_clocks(self):
        for task in self._tasks:
            task.cancel()
        self._tasks = []


async def main():
    server = PresenceServer()
    server.start_clocks()
    async with serve(server.handler, HOST, PORT, max_size=MAX_MESSAGE_BYTES,
                     ping_interval=20, ping_timeout=20) as ws_server:
        print("[PRESENCE] listening on ws://%s:%d%s (database %s)" % (HOST, PORT, PATH, DB_PATH), flush=True)
        await ws_server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
