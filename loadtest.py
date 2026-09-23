#!/usr/bin/env python3
"""
loadtest.py - measure the Elusion API under concurrent load.

    # benchmark a server you already started (ANY platform, your real config):
    python3 loadtest.py --url http://127.0.0.1:5000

    # or let it launch a throwaway server on a scratch DB (POSIX only, gunicorn):
    python3 loadtest.py --launch --workers 4

WHY THIS EXISTS
---------------
"Faster than most" is a claim you can only earn with numbers. security_bot.py
proves the server is hard to break; canary.py proves the economy stays sound;
this proves it stays FAST, and - run before and after a change - proves a change
made it faster instead of quietly slower. It is the regression benchmark a
server you care about should never be without.

WHAT IT MEASURES
----------------
It seeds throwaway accounts, then sweeps concurrency (1..N simultaneous clients)
against two paths that matter:

  * READ  - GET /api/player/status?slot=0   the HUD heartbeat, the most-called
            endpoint in a live game. Pure read; SQLite serves these in parallel.
  * WRITE - POST /api/combat/kill            the heaviest write: XP, level, the
            gold ledger, the kill log, skill XP and a loot roll, all in one
            transaction. This is the path that decides the server's write ceiling.

For each concurrency level it reports throughput (req/s) and p50/p95/p99/max
latency, plus the share of 2xx responses. Reads should scale with CPU cores and
flatten; writes are bounded by SQLite's single writer, so watch whether req/s
RISES with concurrency (good) or stays FLAT while latency climbs (the writer
lock - the signal that WAL / a shorter transaction / eventually Postgres is the
next move).

READING THE WRITE NUMBERS. At high concurrency the spawn ceiling (a real rate
limit: a placed enemy can only be killed so often) starts returning 429 as the
same few test accounts out-kill what the world could respawn. That is the limit
working, not the server slowing - so the honest write ceiling is read at the
LOWEST concurrency where 2xx is still ~100%, not at the top of the sweep.

SAFETY
------
With --url it MUTATES what it points at: it registers accounts and reports
kills. Point it at a TEST server, NEVER production. With --launch it builds its
own scratch database in a temp dir and deletes it on the way out, so it can
never touch real data - the same discipline as security_bot.py.
"""

import argparse
import json
import os
import platform
import random
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time

try:
    import requests
except ImportError:
    sys.exit("loadtest.py needs the 'requests' package:  pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def new_session():
    # trust_env=False so a machine-wide HTTP proxy never intercepts localhost.
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    return s


class Bench:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.s = new_session()

    def call(self, method, path, token=None, body=None, timeout=20):
        headers = {"Authorization": "Bearer " + token} if token else {}
        t0 = time.perf_counter()
        try:
            r = self.s.request(method, self.base + path, json=body,
                               headers=headers, timeout=timeout)
            return (time.perf_counter() - t0) * 1000.0, r.status_code
        except Exception:
            return (time.perf_counter() - t0) * 1000.0, -1

    def wait_until_up(self, tries=120):
        for _ in range(tries):
            _, code = self.call("GET", "/api/status", timeout=1)
            if code != -1:
                return True
            time.sleep(0.25)
        return False


def load_placed_enemies():
    """Reward-granting, placed enemies to rotate kills across, so the spawn
    ceiling stays far away for as long as possible. Falls back to a single id if
    gamedata.json isn't beside this script."""
    path = os.environ.get("ELUSION_GAMEDATA", os.path.join(HERE, "gamedata.json"))
    try:
        raw = json.load(open(path, encoding="utf-8"))
        ids = [e["enemy_id"] for e in raw.get("enemies", [])
               if int(e.get("placed_count", 0)) > 0 and e.get("grants_rewards", True)]
        return ids or ["bushmage"]
    except Exception:
        return ["bushmage"]


def seed(bench, n):
    """Register n accounts, each with a warrior in slot 0, and return their
    tokens. Re-logs-in if a name already exists, so a re-run is harmless."""
    tag = "".join(random.choice(string.ascii_lowercase) for _ in range(6))
    tokens = []
    for i in range(n):
        name = "lt_%s_%d" % (tag, i)
        r = bench.s.post(bench.base + "/api/auth/register",
                         json={"username": name, "password": "password123"}, timeout=20)
        if r.status_code in (200, 201):
            tok = r.json()["token"]
        else:
            lr = bench.s.post(bench.base + "/api/auth/login",
                              json={"username": name, "password": "password123"}, timeout=20)
            if lr.status_code != 200:
                raise RuntimeError("could not seed %s: %s / %s" % (name, r.status_code, lr.status_code))
            tok = lr.json()["token"]
        bench.s.put(bench.base + "/api/save",
                    json={"slot": 0, "class_id": "warrior", "name": "Bench"},
                    headers={"Authorization": "Bearer " + tok}, timeout=20)
        tokens.append(tok)
    return tokens


def sweep(label, make_req, levels, duration):
    print("\n%s" % label)
    print("  conc |   req/s |  p50 ms |  p95 ms |  p99 ms |  max ms |  2xx% | non-2xx")
    print("  -----+---------+---------+---------+---------+---------+-------+---------")
    for c in levels:
        lat = []
        codes = {}
        lock = threading.Lock()
        stop = threading.Event()
        start = threading.Barrier(c + 1)

        def worker():
            local, lc = [], {}
            start.wait()
            while not stop.is_set():
                ms, code = make_req()
                local.append(ms)
                lc[code] = lc.get(code, 0) + 1
            with lock:
                lat.extend(local)
                for k, v in lc.items():
                    codes[k] = codes.get(k, 0) + v

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(c)]
        for t in threads:
            t.start()
        start.wait()
        t0 = time.perf_counter()
        time.sleep(duration)
        stop.set()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0

        lat.sort()
        n = len(lat)
        pct = lambda p: lat[min(n - 1, int(n * p))] if n else 0.0
        two = sum(v for k, v in codes.items() if 200 <= k < 300)
        nonok = ", ".join("%s:%d" % (("ERR" if k < 0 else k), v)
                          for k, v in sorted(codes.items()) if not (200 <= k < 300))
        print("  %5d | %7.0f | %7.1f | %7.1f | %7.1f | %7.1f | %5.1f | %s"
              % (c, n / elapsed if elapsed else 0, pct(.50), pct(.95), pct(.99),
                 lat[-1] if lat else 0.0, 100.0 * two / max(1, n), nonok or "-"))


def launch_server(workers, db_path, port):
    """POSIX convenience: start gunicorn on a scratch DB. On Windows, start your
    own server (waitress-serve ... wsgi:application) and use --url instead."""
    env = dict(os.environ)
    for k in ("ELUSION_DEBUG", "FLASK_DEBUG", "FLASK_ENV"):
        env.pop(k, None)
    env["ELUSION_DB"] = db_path
    env["ELUSION_OWNER"] = "loadtestowner"
    env["ELUSION_TRUSTED_PROXIES"] = "0"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    if platform.system() == "Windows":
        sys.exit("--launch is POSIX-only (gunicorn). On Windows, start the server "
                 "yourself (waitress-serve --listen=127.0.0.1:PORT wsgi:application) "
                 "and run with --url http://127.0.0.1:PORT instead.")
    proc = subprocess.Popen(
        ["gunicorn", "-w", str(workers), "-b", "127.0.0.1:%d" % port,
         "wsgi:application", "--log-level", "error", "--timeout", "60"],
        cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    return proc


def main():
    ap = argparse.ArgumentParser(description="Concurrent load benchmark for the Elusion API.")
    ap.add_argument("--url", help="benchmark an already-running server, e.g. http://127.0.0.1:5000")
    ap.add_argument("--launch", action="store_true",
                    help="launch a throwaway gunicorn server on a scratch DB (POSIX only)")
    ap.add_argument("--workers", type=int, default=4, help="gunicorn workers when --launch (default 4)")
    ap.add_argument("--accounts", type=int, default=64, help="test accounts to seed (default 64)")
    ap.add_argument("--duration", type=float, default=5.0, help="seconds per concurrency level (default 5)")
    ap.add_argument("--levels", default="1,2,4,8,16,32,64", help="comma list of concurrency levels")
    args = ap.parse_args()

    if not args.url and not args.launch:
        ap.error("give --url <base> to benchmark a running server, or --launch to start one")

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    enemies = load_placed_enemies()

    proc = None
    tmpdir = None
    try:
        if args.launch:
            port = free_port()
            base = "http://127.0.0.1:%d" % port
            tmpdir = tempfile.mkdtemp(prefix="elusion_loadtest_")
            db_path = os.path.join(tmpdir, "loadtest.db")
            print("launching gunicorn -w %d on a scratch DB ..." % args.workers)
            proc = launch_server(args.workers, db_path, port)
        else:
            base = args.url

        bench = Bench(base)
        if not bench.wait_until_up():
            sys.exit("server at %s never answered /api/status" % base)

        print("seeding %d accounts against %s ..." % (args.accounts, base))
        tokens = seed(bench, args.accounts)
        print("seeded %d accounts; rotating kills across %d placed enemies" % (len(tokens), len(enemies)))

        # warm the caches / JIT the paths before measuring
        for _ in range(50):
            bench.call("GET", "/api/player/status?slot=0", token=random.choice(tokens))

        sweep("READ PATH  - GET /api/player/status?slot=0",
              lambda: bench.call("GET", "/api/player/status?slot=0", token=random.choice(tokens)),
              levels, args.duration)
        sweep("WRITE PATH - POST /api/combat/kill  (heaviest transaction; read the "
              "ceiling where 2xx is still ~100%%)",
              lambda: bench.call("POST", "/api/combat/kill", token=random.choice(tokens),
                                 body={"slot": 0, "enemy_id": random.choice(enemies)}),
              levels, args.duration)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
