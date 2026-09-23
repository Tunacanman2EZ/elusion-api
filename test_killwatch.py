#!/usr/bin/env python3
"""
test_killwatch.py - proof that the E-3 fraud watch fires on what it must and
stays silent on honest play. Self-contained: builds throwaway databases in
memory (and a couple on disk for the exit-code contract), seeds honest and
cheating patterns from the REAL enemy roster, and asserts.

    python3 test_killwatch.py

No server, no real database, no network. Enemy ids are the live ones so the
spawn-ceiling and reward-less checks run against the same numbers the server
uses.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import killwatch
import gamedata

# Real enemies, chosen for what they prove:
#   boss              placed=2, 5200 hp  -> a boss; ceiling 22 / 300s
#   bushmage          placed=1,  175 hp  -> ordinary; ceiling 11 / 300s
#   poisonslimesmall  placed=0           -> ceiling-EXEMPT (runtime-spawned)
#   poisonslimelarge  placed=0, rewards=false -> the reward-less enemy
PLACED = [eid for eid, e in gamedata.ENEMIES.items()
          if int(e.get("placed_count", 0)) > 0]

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  ok   %s" % name)
    else:
        _failed += 1
        print("  FAIL %s  %s" % (name, detail))


def fresh():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    con.execute(
        "CREATE TABLE kill_reports ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, slot INTEGER,"
        " enemy_id TEXT, xp INTEGER DEFAULT 0, attack_xp INTEGER DEFAULT 0,"
        " level_at INTEGER DEFAULT 0, at INTEGER)")
    return con


def add_user(con, uid, name):
    con.execute("INSERT INTO users (id, username) VALUES (?, ?)", (uid, name))


def kill(con, uid, enemy, at, level=40, slot=0):
    con.execute(
        "INSERT INTO kill_reports (user_id, slot, enemy_id, level_at, at)"
        " VALUES (?, ?, ?, ?, ?)", (uid, slot, enemy, level, at))


def signals(dossier, uid):
    return set(s for _t, s, _x in dossier.get(uid, {"findings": []})["findings"])


def tiers(dossier, uid):
    return set(t for t, _s, _x in dossier.get(uid, {"findings": []})["findings"])


# --------------------------------------------------------------------------- #
print("KILLWATCH TESTS")
print("gamedata loaded: %s | placed enemies: %d" % (killwatch._GAMEDATA_OK, len(PLACED)))
print("-" * 68)

# 1. HONEST PLAYER -> nothing. Bushmage every ~40s, 30 kills over 20 minutes.
con = fresh(); add_user(con, 1, "honest")
for i in range(30):
    kill(con, 1, "bushmage", 100000 + i * 45)
d = killwatch.analyze(con)
check("honest player raises no finding", 1 not in d, "found %s" % signals(d, 1))

# 2. SPAWN-CEILING BREACH (H1). 15 bushmage inside 60s; ceiling is 11 / 300s.
con = fresh(); add_user(con, 2, "burst")
for i in range(15):
    kill(con, 2, "bushmage", 200000 + i * 4)
d = killwatch.analyze(con)
check("ceiling breach -> IMPOSSIBLE", "IMPOSSIBLE" in tiers(d, 2))
check("ceiling breach names the right signal", "spawn ceiling breached" in signals(d, 2),
      "got %s" % signals(d, 2))

# 3. REWARD-LESS ENEMY (H2). One poisonslimelarge row cannot exist on a correct
#    server (handler 400s before writing).
con = fresh(); add_user(con, 3, "rewardless")
kill(con, 3, "poisonslimelarge", 300000)
d = killwatch.analyze(con)
check("reward-less claim -> IMPOSSIBLE", "IMPOSSIBLE" in tiers(d, 3))
check("reward-less names the right signal", "reward-less enemy paid" in signals(d, 3),
      "got %s" % signals(d, 3))

# 4. FAST FARMER (S1) WITHOUT tripping H1. Round-robin across every placed enemy
#    so no single enemy's window breaches, but the overall rate is ~5x honest.
con = fresh(); add_user(con, 4, "farmer")
t = 400000
n = 250
for i in range(n):
    kill(con, 4, PLACED[i % len(PLACED)], t + i * 2)   # 250 kills over 500s
d = killwatch.analyze(con)
check("fast farmer -> SUSPICIOUS", "SUSPICIOUS" in tiers(d, 4))
check("fast farmer flagged for rate", "sustained kill rate" in signals(d, 4),
      "got %s" % signals(d, 4))
check("fast farmer did NOT trip a false ceiling breach",
      "spawn ceiling breached" not in signals(d, 4), "got %s" % signals(d, 4))

# 5. BOSS FARMED FAST (S2). 30 boss over ~1000s = 108/hr (> 90), spaced so the
#    22/300s ceiling is never breached.
con = fresh(); add_user(con, 5, "bossfarm")
for i in range(30):
    kill(con, 5, "boss", 500000 + i * 34, level=55)
d = killwatch.analyze(con)
check("boss farmed fast -> SUSPICIOUS", "boss farmed fast" in signals(d, 5),
      "got %s" % signals(d, 5))
check("boss farm did NOT trip a ceiling breach",
      "spawn ceiling breached" not in signals(d, 5), "got %s" % signals(d, 5))

# 6. EXEMPT-ENEMY DOMINANCE (S3). 150 poisonslimesmall over 3h - exempt, so no
#    H1; low rate, so no S1; but 100% of kills are the ceiling blind spot.
con = fresh(); add_user(con, 6, "blindspot")
for i in range(150):
    kill(con, 6, "poisonslimesmall", 600000 + i * 72)   # ~50/hr
d = killwatch.analyze(con)
check("exempt dominance -> SUSPICIOUS", "farms the ceiling blind spot" in signals(d, 6),
      "got %s" % signals(d, 6))
check("exempt farming is NOT an IMPOSSIBLE (it is legal, just watched)",
      "IMPOSSIBLE" not in tiers(d, 6), "got %s" % tiers(d, 6))

# 7. UNDER-LEVELLED BOSS (S4). Five honest killers at ~level 50 set the median;
#    one killer reports the boss at level 4.
con = fresh()
for uid in range(10, 15):
    add_user(con, uid, "boss%d" % uid)
    for i in range(3):
        kill(con, uid, "boss", 700000 + uid * 1000 + i * 200, level=50)
add_user(con, 99, "cheat")
kill(con, 99, "boss", 720000, level=4)
d = killwatch.analyze(con)
check("under-levelled boss -> SUSPICIOUS for the cheat",
      "boss killed under-levelled" in signals(d, 99), "got %s" % signals(d, 99))
check("the honest level-50 killers are NOT flagged under-levelled",
      all("boss killed under-levelled" not in signals(d, u) for u in range(10, 15)))

# 8. HONEST BURSTS DO NOT FALSE-POSITIVE.
#    (a) 30 poisonslimesmall in one second - a real split - is exempt.
con = fresh(); add_user(con, 7, "split")
for i in range(30):
    kill(con, 7, "poisonslimesmall", 800000)
d = killwatch.analyze(con)
check("30 exempt kills in one second -> no ceiling breach",
      "spawn ceiling breached" not in signals(d, 7), "got %s" % signals(d, 7))
#    (b) both placed boss instances killed seconds apart - legal (2 <= 22).
con = fresh(); add_user(con, 8, "twoboss")
kill(con, 8, "boss", 900000); kill(con, 8, "boss", 900003)
d = killwatch.analyze(con)
check("two boss instances killed together -> no finding", 8 not in d,
      "got %s" % signals(d, 8))

# --------------------------------------------------------------------------- #
# EXIT-CODE CONTRACT (the cron promise), run end-to-end through the CLI.
print("-" * 68)


def run_cli(rows, extra=()):
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    con.execute(
        "CREATE TABLE kill_reports (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER, slot INTEGER, enemy_id TEXT, xp INTEGER DEFAULT 0,"
        " attack_xp INTEGER DEFAULT 0, level_at INTEGER DEFAULT 0, at INTEGER)")
    for uid, name, enemy, at, lvl in rows:
        con.execute("INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)", (uid, name))
        con.execute("INSERT INTO kill_reports (user_id, slot, enemy_id, level_at, at)"
                    " VALUES (?, 0, ?, ?, ?)", (uid, enemy, lvl, at))
    con.commit(); con.close()
    r = subprocess.run([sys.executable, os.path.join(HERE, "killwatch.py"),
                        "--db", path, *extra], capture_output=True, text=True)
    os.remove(path)
    return r.returncode, r.stdout

clean_rows = [(1, "ok", "bushmage", 100000 + i * 45, 30) for i in range(10)]
breach_rows = [(2, "bad", "bushmage", 200000 + i * 4, 30) for i in range(15)]
soft_rows = [(4, "farmer", PLACED[i % len(PLACED)], 400000 + i * 2, 40) for i in range(250)]

rc, out = run_cli(clean_rows)
check("CLI exit 0 on a clean database", rc == 0, "rc=%d" % rc)

rc, out = run_cli(breach_rows)
check("CLI exit 1 on an IMPOSSIBLE finding (alarm)", rc == 1, "rc=%d" % rc)

rc, out = run_cli(soft_rows, extra=("--quiet",))
check("CLI --quiet exits 0 and is silent on soft-only findings",
      rc == 0 and out.strip() == "", "rc=%d out=%r" % (rc, out[:120]))

rc, out = run_cli(breach_rows, extra=("--quiet",))
check("CLI --quiet still speaks and exits 1 on an alarm",
      rc == 1 and out.strip() != "", "rc=%d out=%r" % (rc, out[:120]))

# --------------------------------------------------------------------------- #
print("-" * 68)
print("PASSED %d  FAILED %d" % (_passed, _failed))
sys.exit(1 if _failed else 0)
