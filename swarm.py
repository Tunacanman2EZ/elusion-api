#!/usr/bin/env python3
"""
swarm.py - a crowd of bots playing at once, against a SCRATCH copy of the whole
server: the API and the presence socket together, the way a game uses them.

    # 8, 16, 32, 64 and 128 players in one area, 30 s each, on a scratch server
    # pinned to ONE core (the live box's), the bots on another:
    python3 swarm.py --launch

    python3 swarm.py --launch --levels 128 --duration 120 --areas 3

    # spread over a Big Field, so each game is sent only who is near (0.89.0):
    python3 swarm.py --launch --levels 128 --spread 2400

    # every bot on a compressed socket, as the browser build is:
    python3 swarm.py --launch --levels 128 --deflate 1

    # a before and after on the same box: an older presence.py, and the API
    # with a connection a request (THE CONNECTION IS KEPT, app.py):
    python3 swarm.py --launch --levels 128 --presence old/presence.py --env ELUSION_DB_FRESH=1

    # a TEST server you started yourself (never production):
    python3 swarm.py --api http://127.0.0.1:5000 --ws ws://127.0.0.1:5001/ws/presence

WHY THIS EXISTS. The owner, 22 Oct, of a 16x stress test: "i need absolute
masterclass help to get my server to accomplish this". loadtest.py hammers two
routes as fast as they go - the right tool for a route's ceiling, and blind to
what PLAYERS cost: a socket held open, ten steps a second each relayed to
everyone in the area, the area's monsters streamed by its leader and read into
the books, and an HTTP request about every second besides. This plays that.

WHAT A BOT DOES - what a game in the world does, at its own rates (0.76.0):
  over the presence socket (presence.py):
    hello with a ticket (POST /api/presence/ticket) - saying it takes attacks
    inside the tick ("xt"), as game 0.76.0 does, unless it is one of the
    --old-share playing an older build - then where it stands ten times a
    second ("s": walking a circle, with its own clock in "ts"), an attack
    picture after the step on --attacks a second of those beats ("x", as the
    game sends what it made since its last beat), its ticket renewed every
    minute.
    THE AREA'S LEADER (the server names it, "lead") answers every "need" as
    monstersync.gd does - what changed, then every authored spot in
    gamedata.json, 60 a message - and sends what changed ten times a second
    ("w" snap, --movers of them) with its own hits; every other bot sends its
    hits to the leader twice a second ("h").
  over HTTP (the API), each on its own jittered clock:
    GET  /api/chat?channel=world         every 3 s     (chatpanel.gd)
    GET  /api/server/broadcasts          every 10 s    (characterhud.gd)
    GET  /api/auth/session               every 15 s    (api.gd heartbeat)
    POST /api/skill/train                every 20 s    (skilltrainer.gd)
    PUT  /api/save                       every 10 s    (characterdata.gd's ceiling)
    POST /api/combat/kill                every --kill-seconds

WHAT IT MEASURES, for each level:
  relay   from one bot's socket to another's: a step's "ts" against the clock
          when a listening bot reads it in "moves" (ms; the server's 100 ms
          tick is inside it), and how many steps a second it carried; and the
          same for the attack pictures, in the moves or on their own. The
          games play both back 200 ms behind (remoteplayer.gd PLAYBACK_DELAY):
          a p99 past that is a picture that came too late to play smoothly.
  http    each route's p50 / p95 / p99 and what came back that was not 2xx -
          429 from the kill ceiling (killwatch's spawn limit) is the limit
          working, counted apart from errors.
  procs   CPU % and memory of the presence process, the API's workers and
          this swarm itself, over the level - memory that climbs from one
          level to the next with the bots gone is a leak. A swarm near 100%
          of its core is measuring itself, and says so.
  drops   any bot the server sent away ("bye", and why) over the whole level,
          the crowd walking in included - how 0.76.0 found a leader dropped
          "too fast" for answering everybody who arrived.

SAFETY. With --launch everything is built in a temp dir and deleted on the way
out. --api/--ws MUTATE what they point at (accounts, kills, saves): a TEST
server you started, NEVER production - the same rule as loadtest.py.
"""
import argparse
import asyncio
import json
import math
import os
import random
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
    import websockets
except ImportError:
    sys.exit("swarm.py needs requests and websockets:  pip install requests websockets")
try:
    import psutil
except ImportError:
    psutil = None

HERE = os.path.dirname(os.path.abspath(__file__))
GAMEDATA = json.load(open(os.environ.get("ELUSION_GAMEDATA", os.path.join(HERE, "gamedata.json")),
                          encoding="utf-8"))
RESOURCE = {e["enemy_id"]: e.get("resource", "") for e in GAMEDATA["enemies"]}
MAX_HP = {e["enemy_id"]: int(e.get("max_hp", 1) or 1) for e in GAMEDATA["enemies"]}
PLACED = [e["enemy_id"] for e in GAMEDATA["enemies"]
          if int(e.get("placed_count", 0)) > 0 and e.get("grants_rewards", True)] or ["bushmage"]

STEP_SECONDS = 0.1          # presence.gd SEND_SECONDS
HITS_SECONDS = 0.5
RENEW_SECONDS = 60.0        # presence.gd RENEW_SECONDS
RECORDS_PER_PART = 60       # monstersync.gd: a full world, 60 monsters a message
HTTP_EVERY = [              # (name, method, path, seconds) - see the docstring
    ("chat", "GET", "/api/chat?channel=world&since=0", 3.0),
    ("broadcasts", "GET", "/api/server/broadcasts?since=0&slot=0", 10.0),
    ("session", "GET", "/api/auth/session", 15.0),
    ("train", "POST", "/api/skill/train", 20.0),
    ("save", "PUT", "/api/save", 10.0),
]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def new_session():
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=64)
    s.mount("http://", adapter)
    return s


def pct(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


class Http:
    """Blocking requests, run on a thread pool so a slow answer never holds up
    a socket. Every answer is timed and counted by route."""

    def __init__(self, base, threads):
        self.base = base.rstrip("/")
        self.local = threading.local()
        self.pool = ThreadPoolExecutor(max_workers=threads)
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.times = {}
            self.codes = {}

    def session(self):
        if not hasattr(self.local, "s"):
            self.local.s = new_session()
        return self.local.s

    def call(self, name, method, path, token=None, body=None, timeout=20, record=True):
        headers = {"Authorization": "Bearer " + token} if token else {}
        t0 = time.perf_counter()
        try:
            r = self.session().request(method, self.base + path, json=body, headers=headers, timeout=timeout)
            code, data = r.status_code, (r.json() if r.headers.get("content-type", "").startswith(
                "application/json") else None)
        except Exception:
            code, data = -1, None
        ms = (time.perf_counter() - t0) * 1000.0
        if record:
            with self.lock:
                self.times.setdefault(name, []).append(ms)
                per = self.codes.setdefault(name, {})
                per[code] = per.get(code, 0) + 1
        return code, data

    async def acall(self, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(self.pool, lambda: self.call(*args, **kwargs))


def seed(http, n, tag):
    """n accounts with a warrior in slot 0; their tokens."""
    tokens = []
    for i in range(n):
        name = "sw_%s_%d" % (tag, i)
        code, data = http.call("register", "POST", "/api/auth/register",
                               body={"username": name, "password": "password123"}, record=False)
        if code not in (200, 201):
            code, data = http.call("login", "POST", "/api/auth/login",
                                   body={"username": name, "password": "password123"}, record=False)
        if code not in (200, 201) or not isinstance(data, dict) or "token" not in data:
            raise RuntimeError("could not seed %s (%s)" % (name, code))
        token = data["token"]
        http.call("save", "PUT", "/api/save", token=token,
                  body={"slot": 0, "class_id": "warrior", "name": "Bot"}, record=False)
        tokens.append(token)
    return tokens


def area_world(area):
    """Every authored monster of `area`, as a leader's game numbers and sends
    them in a full world (monstersync.gd: {id, o, s, pp, x, y, a, hp, mh, p})."""
    spec = GAMEDATA["areas"][area]
    records = []
    for i, spot in enumerate(spec["spawns"]):
        enemy = spot["e"]
        hp = MAX_HP.get(enemy, 50)
        records.append({"id": i + 1, "o": spot["o"], "s": "", "pp": "ysortworld/enemies",
                        "x": spot["x"], "y": spot["y"], "a": "idledown", "hp": hp, "mh": hp,
                        "p": {"ed": RESOURCE.get(enemy, "")}})
    return records


class Stats:
    def __init__(self):
        # Over the whole level, ramp included: a bot dropped while the crowd
        # walked in is the finding, not noise before the window.
        self.errors = {}
        self.byes = {}
        self.reset()

    def reset(self):
        self.relay = []
        self.attack_relay = []
        self.steps_seen = 0
        self.sent = 0
        self.received = 0
        self.bytes_in = 0
        self.dropped = 0
        self.connected = 0
        self.moves_frames = 0
        self.steps_sent = 0

    def error(self, exc):
        name = type(exc).__name__
        self.errors[name] = self.errors.get(name, 0) + 1


class Bot:
    def __init__(self, index, token, area, http, ws_url, stats, listen, args):
        self.index, self.token, self.area = index, token, area
        self.http, self.ws_url, self.stats, self.listen, self.args = http, ws_url, stats, listen, args
        self.user_id = None
        self.leader = False
        self.world = area_world(area)
        # Where its circle is: within --spread of the middle, across and up and
        # down (400, everyone on one screen; ONLY WHAT IS NEAR, 0.89.0, sends a
        # game only the players near it, so a spread crowd costs less).
        half = args.spread / 2.0
        self.centre = (random.uniform(-half, half), random.uniform(-half, half))
        self.phase = random.uniform(0, math.tau)
        self.stop = False
        self.frames = 0
        self.trace = []
        self.deflate = random.random() < args.deflate
        self.old = random.random() < args.old_share

    async def run(self):
        code, data = await self.http.acall("ticket", "POST", "/api/presence/ticket",
                                           token=self.token, body={"slot": 0})
        if code != 200:
            return
        url = self.ws_url or data.get("socket_url")
        try:
            # A BROWSER offers permessage-deflate and Godot's own socket does
            # not: --deflate is the share of bots that play the browser build.
            async with websockets.connect(url, max_size=2 ** 20, open_timeout=20, ping_interval=None,
                                          compression="deflate" if self.deflate else None) as ws:
                hello = {"t": "hello", "ticket": data["ticket"], "v": 3}
                if not self.old:
                    hello["xt"] = 1
                await ws.send(json.dumps(hello))
                welcome = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if welcome.get("t") != "welcome":
                    return
                self.user_id = welcome.get("id")
                self.stats.connected += 1
                tasks = [asyncio.create_task(t) for t in
                         (self._read(ws), self._steps(ws), self._hits(ws),
                          self._renew(ws), self._http_loop(), self._kills())]
                while not self.stop:
                    await asyncio.sleep(0.2)
                    for t in tasks:
                        if t.done() and not t.cancelled() and t.exception() is not None:
                            # A bot that stopped reading or sending is no
                            # longer playing: counted, and dropped.
                            self.stats.error(t.exception())
                            self.stop = True
                for t in tasks:
                    t.cancel()
        except Exception as exc:
            self.stats.dropped += 1
            self.stats.error(exc)

    # ---- the socket ---------------------------------------------------------
    async def _read(self, ws):
        async for raw in ws:
            self.stats.received += 1
            self.stats.bytes_in += len(raw)
            # Only what this bot must act on is parsed; the rest is read and
            # dropped, as cheaply as a frame can be, so the swarm measures the
            # server and not its own JSON.
            if raw.startswith('{"t":"moves"'):
                if self.listen:
                    self.frames += 1
                    self.stats.moves_frames += 1
                    now = time.time() * 1000.0
                    msg = json.loads(raw)
                    for row in msg.get("p", []):
                        if row[0] != self.user_id and isinstance(row[6], (int, float)) and row[6] > 0:
                            self.stats.relay.append(now - row[6])
                            self.stats.steps_seen += 1
                    for maker, attacks in msg.get("x", []):
                        if maker != self.user_id:
                            self.stats.attack_relay.extend(now - a[1] for a in attacks)
            elif raw.startswith('{"t":"x"'):
                if self.listen:
                    now = time.time() * 1000.0
                    self.stats.attack_relay.extend(now - a[1] for a in json.loads(raw).get("e", []))
            elif raw.startswith('{"t":"lead"'):
                msg = json.loads(raw)
                self.leader = msg.get("a") == self.area and msg.get("id") == self.user_id
                self.trace.append(("lead", round(time.time() % 1000, 2), msg.get("id")))
            elif raw.startswith('{"t":"need"') and self.leader:
                self.trace.append(("need", round(time.time() % 1000, 2), json.loads(raw).get("id", 0)))
                await self._send_full(ws, json.loads(raw).get("id", 0))
                self.trace.append(("sent", round(time.time() % 1000, 2)))
            elif raw.startswith('{"t":"bye"'):
                why = str(json.loads(raw).get("why", ""))
                self.stats.byes[why] = self.stats.byes.get(why, 0) + 1
                self.trace.append(("bye", round(time.time() % 1000, 2), why))
            elif raw.startswith('{"t":"join"') or raw.startswith('{"t":"leave"'):
                self.trace.append((raw[6:10], round(time.time() % 1000, 2), raw[:80]))

    async def _send_full(self, ws, to):
        # As monstersync.gd's _on_need(): what changed first, to everyone,
        # then everything to the newcomer, RECORDS_PER_PART monsters a message
        # - the Big Field's 366 are seven, eight messages a newcomer.
        await ws.send(json.dumps({"t": "w", "d": {"snap": []}}, separators=(",", ":")))
        self.stats.sent += 1
        parts = [self.world[i:i + RECORDS_PER_PART] for i in range(0, len(self.world), RECORDS_PER_PART)] or [[]]
        for i, chunk in enumerate(parts):
            d = {"full": True, "reset": False, "part": i, "parts": len(parts), "spawn": chunk, "wave": 0}
            msg = {"t": "w", "d": d}
            if to:
                msg["to"] = to
            await ws.send(json.dumps(msg, separators=(",", ":")))
            self.stats.sent += 1

    async def _steps(self, ws):
        await asyncio.sleep(random.uniform(0, STEP_SECONDS))
        while True:
            t = time.time()
            angle = self.phase + t * 0.8
            x = self.centre[0] + math.cos(angle) * 120.0
            y = self.centre[1] + math.sin(angle) * 120.0
            await ws.send(json.dumps({"t": "s", "a": self.area, "x": round(x, 1), "y": round(y, 1),
                                      "m": "walkdown", "fx": [], "pet": "",
                                      "ts": int(t * 1000.0), "aim": 0, "hp": 100}, separators=(",", ":")))
            self.stats.sent += 1
            self.stats.steps_sent += 1
            if random.random() < self.args.attacks * STEP_SECONDS:
                # What it made since the last beat, after the state - the
                # server checks it against where that state put it.
                await ws.send(json.dumps({"t": "x", "e": [["slash", int(t * 1000.0), round(x, 1), round(y, 1),
                                                           round(x + 30.0, 1), round(y, 1), 0, 0]]},
                                         separators=(",", ":")))
                self.stats.sent += 1
            if self.leader:
                movers = random.sample(self.world, min(self.args.movers, len(self.world)))
                snap = [[r["id"], round(r["x"] + random.uniform(-4, 4), 1),
                         round(r["y"] + random.uniform(-4, 4), 1), "walkdown", r["hp"]] for r in movers]
                d = {"snap": snap}
                if self.world and random.random() < 0.5:
                    d["hits"] = [[random.choice(self.world)["id"], 1, 0]]
                await ws.send(json.dumps({"t": "w", "d": d}, separators=(",", ":")))
                self.stats.sent += 1
            await asyncio.sleep(STEP_SECONDS)

    async def _hits(self, ws):
        await asyncio.sleep(random.uniform(0, HITS_SECONDS))
        while True:
            # Nothing to hit in an area with no monsters (the town).
            if not self.leader and self.world:
                await ws.send(json.dumps({"t": "h", "p": [[random.choice(self.world)["id"], 1, 0]]},
                                         separators=(",", ":")))
                self.stats.sent += 1
            await asyncio.sleep(HITS_SECONDS)

    async def _renew(self, ws):
        while True:
            await asyncio.sleep(RENEW_SECONDS * random.uniform(0.9, 1.0))
            code, data = await self.http.acall("ticket", "POST", "/api/presence/ticket",
                                               token=self.token, body={"slot": 0})
            if code == 200:
                await ws.send(json.dumps({"t": "renew", "ticket": data["ticket"]}))
                self.stats.sent += 1

    # ---- HTTP ---------------------------------------------------------------
    async def _http_loop(self):
        due = {name: time.time() + random.uniform(0, every) for name, _, _, every in HTTP_EVERY}
        while True:
            now = time.time()
            for name, method, path, every in HTTP_EVERY:
                if now >= due[name]:
                    due[name] = now + every * random.uniform(0.9, 1.1)
                    body = None
                    if name == "train":
                        body = {"slot": 0, "agility": 3, "defense": 10, "magic": 0}
                    elif name == "save":
                        body = {"slot": 0, "class_id": "warrior", "name": "Bot"}
                    asyncio.create_task(self.http.acall(name, method, path, token=self.token, body=body))
            await asyncio.sleep(0.25)

    async def _kills(self):
        await asyncio.sleep(random.uniform(0, self.args.kill_seconds))
        while True:
            asyncio.create_task(self.http.acall("kill", "POST", "/api/combat/kill", token=self.token,
                                                body={"slot": 0, "enemy_id": random.choice(PLACED)}))
            await asyncio.sleep(self.args.kill_seconds * random.uniform(0.8, 1.2))


class Procs:
    """CPU and memory of the server's processes (and this one) over a level."""

    def __init__(self, presence_pid, api_pid):
        self.presence_pid, self.api_pid = presence_pid, api_pid

    def _tree(self, pid):
        if psutil is None or pid is None:
            return []
        try:
            p = psutil.Process(pid)
            return [p] + p.children(recursive=True)
        except psutil.Error:
            return []

    def snapshot(self):
        out = {}
        for name, pid in (("presence", self.presence_pid), ("api", self.api_pid), ("swarm", os.getpid())):
            procs = self._tree(pid) if name != "swarm" else ([psutil.Process(pid)] if psutil else [])
            cpu, rss = 0.0, 0
            for p in procs:
                try:
                    t = p.cpu_times()
                    cpu += t.user + t.system
                    rss += p.memory_info().rss
                except psutil.Error:
                    pass
            out[name] = (cpu, rss)
        return out


def launch(tmp, cores, presence_file=None, extra_env=()):
    """A scratch API (gunicorn, the live box's -w 2 --threads 4) and presence,
    on one database in `tmp`, pinned to `cores` when taskset is here.
    `presence_file` runs another presence.py (an older copy, for a before and
    after) with this folder's modules; `extra_env` is KEY=VALUE for both."""
    db = os.path.join(tmp, "swarm.db")
    api_port, ws_port = free_port(), free_port()
    env = dict(os.environ)
    for k in ("ELUSION_DEBUG", "FLASK_DEBUG", "FLASK_ENV", "ELUSION_RATE_LIMIT"):
        env.pop(k, None)
    env.update({"ELUSION_DB": db, "ELUSION_OWNER": "swarmowner", "ELUSION_TRUSTED_PROXIES": "0",
                "ELUSION_PRESENCE_PORT": str(ws_port), "NO_PROXY": "127.0.0.1,localhost",
                "ELUSION_PRESENCE_URL": "ws://127.0.0.1:%d/ws/presence" % ws_port})
    for pair in extra_env:
        key, _, value = pair.partition("=")
        env[key.strip()] = value
    pin = ["taskset", "-c", cores] if cores and shutil.which("taskset") else []
    api = subprocess.Popen(pin + ["gunicorn", "--preload", "-w", "2", "--threads", "4",
                                  "-b", "127.0.0.1:%d" % api_port, "wsgi:application",
                                  "--log-level", "error", "--timeout", "60"],
                           cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    http = Http("http://127.0.0.1:%d" % api_port, 4)
    for _ in range(240):
        if http.call("status", "GET", "/api/status", timeout=1, record=False)[0] != -1:
            break
        time.sleep(0.25)
    run_presence = [sys.executable, "presence.py"]
    if presence_file:
        run_presence = [sys.executable, "-c", "import runpy, sys; sys.path.insert(0, %r); "
                        "runpy.run_path(%r, run_name='__main__')" % (HERE, os.path.abspath(presence_file))]
    presence = subprocess.Popen(pin + run_presence, cwd=HERE, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", ws_port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.25)
    return api, presence, "http://127.0.0.1:%d" % api_port, "ws://127.0.0.1:%d/ws/presence" % ws_port


async def run_level(n, tokens, areas, http, ws_url, args, procs):
    stats = Stats()
    # THE LISTENERS ARE THE LAST TO ARRIVE: the first in an area leads it,
    # and a leader's own load is not what the others see.
    bots = [Bot(i, tokens[i], areas[i % len(areas)], http, ws_url, stats,
                i >= n - min(n, args.listeners), args) for i in range(n)]
    runs = []
    for bot in bots:
        runs.append(asyncio.create_task(bot.run()))
        await asyncio.sleep(args.ramp / max(1, n))
    await asyncio.sleep(args.warmup)
    print("  measuring %d players for %g s ..." % (n, args.duration), flush=True)
    stats.reset()
    for bot in bots:
        bot.frames = 0
    http.reset()
    before = procs.snapshot()
    t0 = time.time()
    await asyncio.sleep(args.duration)
    elapsed = time.time() - t0
    after = procs.snapshot()
    leaders = sum(1 for b in bots if b.leader and not b.stop)
    stats.connected = sum(1 for b in bots if b.user_id is not None and not b.stop)
    if os.environ.get("SWARM_DEBUG"):
        print("  frames/s by listener: %s  (leader %s)" % (
            " ".join("%d:%.1f" % (b.index, b.frames / elapsed) for b in bots if b.listen),
            [b.index for b in bots if b.leader]), flush=True)
    deaf = [b.index for b in bots if b.listen and b.frames < elapsed * 2]
    if deaf and os.environ.get("SWARM_DEBUG"):
        for b in bots:
            if b.index in deaf or b.leader:
                print("  bot %d (id %s) trace: %s" % (b.index, b.user_id, b.trace[:12] + ["..."] + b.trace[-6:]))
    if deaf:
        print("  !! listeners that heard (almost) no moves: %s - leader: %s" % (
            deaf, [b.index for b in bots if b.leader]), flush=True)
    for bot in bots:
        bot.stop = True
    await asyncio.gather(*runs, return_exceptions=True)
    return stats, elapsed, before, after, leaders


def report(n, stats, elapsed, before, after, leaders, http, args):
    print("\n== %d players  (%d still connected at the end, %d leading an area)" % (
        n, stats.connected, leaders))
    if stats.byes or stats.dropped:
        print("  !! the server dropped bots: %s%s" % (
            ", ".join("%s:%d" % kv for kv in sorted(stats.byes.items())),
            ("  (%d sockets failed)" % stats.dropped) if stats.dropped else ""))
    r = stats.relay
    print("  relay   p50 %6.1f  p95 %6.1f  p99 %6.1f  max %7.1f ms   %7.0f steps/s seen by listeners" % (
        pct(r, .5), pct(r, .95), pct(r, .99), max(r) if r else 0.0, stats.steps_seen / elapsed))
    # Of the steps the OTHER bots in a listener's area sent over the level,
    # the share a listener read: every listener should read every one.
    listeners = max(1, min(args.listeners, n))
    per_area = max(1.0, n / max(1, min(args.areas, n)))
    expected = listeners * stats.steps_sent / max(1, min(args.areas, n)) * (per_area - 1) / per_area
    print("          %5.1f moves frames/s a listener (the tick is 10/s)   %5.1f%% of the steps sent arrived" % (
        stats.moves_frames / elapsed / listeners, 100.0 * stats.steps_seen / max(1.0, expected)))
    if args.spread > 2 * 480.0:
        print("          (spread %d px: each game is sent only who is near it - ONLY WHAT IS NEAR in"
              " presence.py - so under 100%% is the saving, not a loss)" % args.spread)
    a = stats.attack_relay
    print("  attacks p50 %6.1f  p95 %6.1f  p99 %6.1f  max %7.1f ms   %7.0f pictures/s seen by listeners" % (
        pct(a, .5), pct(a, .95), pct(a, .99), max(a) if a else 0.0, len(a) / elapsed))
    if stats.errors:
        print("  errors  " + ", ".join("%s:%d" % kv for kv in sorted(stats.errors.items())))
    print("  socket  %7.0f msgs/s sent  %7.0f msgs/s received  %6.2f MB/s in" % (
        stats.sent / elapsed, stats.received / elapsed, stats.bytes_in / elapsed / 1e6))
    print("  http    route        n/s    p50     p95     p99   non-2xx")
    for name in sorted(http.times):
        t = http.times[name]
        bad = ", ".join("%s:%d" % ("ERR" if c < 0 else c, k) for c, k in sorted(http.codes[name].items())
                        if not 200 <= c < 300)
        print("          %-10s %6.1f %6.1f %7.1f %7.1f   %s" % (name, len(t) / elapsed, pct(t, .5),
                                                               pct(t, .95), pct(t, .99), bad or "-"))
    for name in ("presence", "api", "swarm"):
        if name in before and name in after:
            cpu = (after[name][0] - before[name][0]) / elapsed * 100.0
            print("  %-8s %5.0f%% of a core   %6.0f MB -> %6.0f MB" % (
                name, cpu, before[name][1] / 1e6, after[name][1] / 1e6))
    if "swarm" in after and (after["swarm"][0] - before["swarm"][0]) / elapsed > 0.85:
        print("  !! the swarm itself was near a full core - its relay numbers include its own wait")


def main():
    ap = argparse.ArgumentParser(description="A crowd of bots playing at once against a scratch server.")
    ap.add_argument("--launch", action="store_true", help="launch a scratch API and presence (POSIX)")
    ap.add_argument("--api", help="a TEST API you started, e.g. http://127.0.0.1:5000 - never production")
    ap.add_argument("--ws", help="its presence socket, e.g. ws://127.0.0.1:5001/ws/presence")
    ap.add_argument("--levels", default="8,16,32,64,128", help="players at each level")
    ap.add_argument("--duration", type=float, default=30.0, help="seconds measured at each level")
    ap.add_argument("--warmup", type=float, default=8.0, help="seconds before measuring")
    ap.add_argument("--ramp", type=float, default=6.0, help="seconds to bring a level's bots in")
    ap.add_argument("--areas", type=int, default=1, help="how many areas the bots spread across")
    ap.add_argument("--spread", type=float, default=400.0,
                    help="how far apart the bots stand, px across and down (Big Field is about 2400)")
    ap.add_argument("--movers", type=int, default=60, help="monsters a leader says moved each tick")
    ap.add_argument("--kill-seconds", type=float, default=4.0, help="each bot reports a kill this often")
    ap.add_argument("--listeners", type=int, default=8, help="bots that time the relay")
    ap.add_argument("--attacks", type=float, default=2.0,
                    help="attack pictures each bot sends a second - a warrior holding attack is about 2")
    ap.add_argument("--old-share", type=float, default=0.0,
                    help="share of bots playing a build from before 0.76.0: no \"xt\" (0-1)")
    ap.add_argument("--deflate", type=float, default=0.0,
                    help="share of bots that compress their socket, as a browser does (0-1)")
    ap.add_argument("--presence", help="with --launch: run this presence.py instead (a before and after)")
    ap.add_argument("--env", action="append", default=[],
                    help="with --launch: KEY=VALUE for the scratch server, e.g. ELUSION_DB_FRESH=1")
    ap.add_argument("--server-cores", default="0", help="cores for the scratch server (taskset), '' for any")
    ap.add_argument("--swarm-cores", default="1", help="cores for the bots (taskset), '' for any")
    args = ap.parse_args()
    if not args.launch and not (args.api and args.ws):
        ap.error("--launch, or --api and --ws for a TEST server you started")

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    areas = ["bigfield", "field", "elusion", "boss", "bossarena", "easteregg"][:max(1, args.areas)]
    tmp, api_proc, presence_proc = None, None, None
    try:
        if args.launch:
            tmp = tempfile.mkdtemp(prefix="elusion_swarm_")
            api_proc, presence_proc, base, ws_url = launch(tmp, args.server_cores, args.presence, args.env)
            print("scratch server: API %s (pid %d), presence %s (pid %d)%s" % (
                base, api_proc.pid, ws_url, presence_proc.pid,
                (" - pinned to core(s) %s" % args.server_cores) if args.server_cores else ""))
        else:
            base, ws_url = args.api, args.ws
        if args.swarm_cores and hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(0, {int(c) for c in args.swarm_cores.split(",")})
        http = Http(base, 64)
        tag = "".join(random.choice(string.ascii_lowercase) for _ in range(5))
        print("seeding %d accounts ..." % max(levels))
        tokens = seed(http, max(levels), tag)
        procs = Procs(presence_proc.pid if presence_proc else None, api_proc.pid if api_proc else None)
        for n in levels:
            stats, elapsed, before, after, leaders = asyncio.run(
                run_level(n, tokens, areas, http, ws_url, args, procs))
            report(n, stats, elapsed, before, after, leaders, http, args)
    finally:
        for proc in (presence_proc, api_proc):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
