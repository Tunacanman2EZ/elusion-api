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
#   boss              a boss
#   bushmage          ordinary; its ceiling is read from gamedata.json below
#   poisonslimesmall  placed=0           -> ceiling-EXEMPT (runtime-spawned)
#   poisonslimelarge  placed=0, rewards=false -> the reward-less enemy
#
# THE BURST IS THE CEILING PLUS FOUR, NOT A NUMBER. This said "15 bushmage;
# ceiling is 11" while one bush mage stood in the world. Day 2 the Big Field
# placed four more, the ceiling became 55, fifteen kills breached nothing, and
# four checks failed on a world that had simply grown.
BURST_ENEMY = "bushmage"
BURST_CEILING = killwatch._ceiling_for(
    int(gamedata.ENEMIES[BURST_ENEMY]["placed_count"]),
    killwatch.KILL_WINDOW_SECONDS, killwatch.KILL_RESPAWN_FLOOR_SECONDS)
BURST = BURST_CEILING + 4
# THE GAP IS WORKED OUT TOO, so the whole burst sits inside one window. It was
# 4 s, and 0.63.0 doubling the Big Field's fields made the burst 103 kills -
# 412 s, longer than the 300 s window.
BURST_GAP = max(1, (killwatch.KILL_WINDOW_SECONDS - 1) // BURST)
assert BURST * BURST_GAP < killwatch.KILL_WINDOW_SECONDS, (BURST, BURST_GAP)
# PLACED AND PAYING. A large slime is placed and grants nothing - it bursts
# into smalls - so a kill claim for one is the reward-less IMPOSSIBLE below, not
# an honest farm.
PLACED = [eid for eid, e in gamedata.ENEMIES.items()
          if int(e.get("placed_count", 0)) > 0 and e.get("grants_rewards", True)]

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

# 0. THE SAME NUMBERS AS THE LIVE GATE. killwatch.py keeps its own copy of the
#    window and the respawn floor so it runs without the server; this is what
#    holds the copy to the original (kill/services.py since 18 Oct). A drift
#    is the tool crying wolf on honest kills, or missing real breaches.
from kill import services as kill_rules  # noqa: E402

check("killwatch's window and respawn floor are the live gate's",
      (killwatch.KILL_WINDOW_SECONDS, killwatch.KILL_RESPAWN_FLOOR_SECONDS)
      == (kill_rules.KILL_WINDOW_SECONDS, kill_rules.KILL_RESPAWN_FLOOR_SECONDS))
check("  and so is its ceiling, for any count of placed monsters",
      all(killwatch._ceiling_for(n, killwatch.KILL_WINDOW_SECONDS, killwatch.KILL_RESPAWN_FLOOR_SECONDS)
          == kill_rules.spawn_ceiling(n) for n in range(0, 40)))

# 1. HONEST PLAYER -> nothing. Bushmage every ~40s, 30 kills over 20 minutes.
con = fresh(); add_user(con, 1, "honest")
for i in range(30):
    kill(con, 1, "bushmage", 100000 + i * 45)
d = killwatch.analyze(con)
check("honest player raises no finding", 1 not in d, "found %s" % signals(d, 1))

# 2. SPAWN-CEILING BREACH (H1). Four more bush mages than the world can produce
#    in one window, all inside it.
con = fresh(); add_user(con, 2, "burst")
for i in range(BURST):
    kill(con, 2, BURST_ENEMY, 200000 + i * BURST_GAP)
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
# 9. THE BOOKS (0.10.0). Paid kills matched to presence.py's own judgement of
#    every death (combat_kills) and its tripped checks (combat_flags): WATCHED,
#    with the numbers, never an alarm.
def books_tables(con):
    con.execute(
        "CREATE TABLE combat_kills (id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER, area TEXT,"
        " enemy_id TEXT, origin TEXT DEFAULT '', leader_id INTEGER DEFAULT 0, user_id INTEGER,"
        " slot INTEGER DEFAULT -1, verdict TEXT, damage INTEGER DEFAULT 0, refused INTEGER DEFAULT 0,"
        " hp_left INTEGER DEFAULT 0, max_hp INTEGER DEFAULT 0, seconds REAL DEFAULT 0)")
    con.execute(
        "CREATE TABLE combat_flags (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,"
        " area TEXT DEFAULT '', kind TEXT, count INTEGER DEFAULT 1, detail TEXT DEFAULT '',"
        " first_at INTEGER, last_at INTEGER)")


def judged(con, uid, enemy, at, verdict="agreed", damage=100):
    con.execute("INSERT INTO combat_kills (at, area, enemy_id, user_id, verdict, damage)"
                " VALUES (?, 'field', ?, ?, ?, ?)", (at, enemy, uid, verdict, damage))


print("-" * 68)
con = fresh(); books_tables(con)
for uid, name in ((20, "booked"), (21, "unseen"), (22, "shortly"), (23, "before"), (24, "flagged"),
                  (25, "twice")):
    add_user(con, uid, name)
judged(con, 99, "bushmage", 1000000)          # the books' first row: they began here
kill(con, 23, "bushmage", 999000)             # a kill from before the books
for i in range(10):
    at = 1000100 + i * 60
    kill(con, 20, "bushmage", at)
    judged(con, 20, "bushmage", at - 2)
for i in range(3):
    kill(con, 21, "bushmage", 1000100 + i * 60)
kill(con, 22, "bushmage", 1000500)
judged(con, 22, "bushmage", 1000499, verdict="short", damage=0)
kill(con, 25, "firesprite", 1000600)
kill(con, 25, "firesprite", 1000601)
judged(con, 25, "firesprite", 1000600)
con.execute("INSERT INTO combat_flags (user_id, kind, count, detail, first_at, last_at)"
            " VALUES (24, 'too_fast', 5, 'old', 1000000, 1000010)")
con.execute("INSERT INTO combat_flags (user_id, kind, count, detail, first_at, last_at)"
            " VALUES (24, 'too_fast', 7, 'newest', 1000020, 1000030)")
d = killwatch.analyze(con)
check("books: kills the books agreed with are nothing to see", 20 not in d, "got %s" % signals(d, 20))
check("books: paid kills the books never saw are WATCHED, with the numbers",
      tiers(d, 21) == {"WATCHED"} and "3 the books never saw" in d[21]["findings"][0][2], d.get(21))
check("books: a kill the books judged short is WATCHED",
      tiers(d, 22) == {"WATCHED"} and "1 short" in d[22]["findings"][0][2], d.get(22))
check("books: a kill from before the books began is not judged", 23 not in d, d.get(23))
check("books: one row backs one kill, not two",
      "1 agreed" in str(d.get(25)) and "1 the books never saw" in str(d.get(25)), d.get(25))
check("books: a tripped check is WATCHED, counted, with the newest detail",
      tiers(d, 24) == {"WATCHED"} and "too_fast x12 (latest: newest)" in d[24]["findings"][0][2], d.get(24))
check("books: WATCHED ranks below every review",
      [u for u, _ in killwatch._rank({**d, 99: {"username": "x", "findings": [("SUSPICIOUS", "s", "x")]}})][0]
      == 99)
check("books: the summary line counts every verdict",
      killwatch.books_summary(con).startswith("books: 13 kill rows since")
      and "12 agreed, 1 short, 0 not due" in killwatch.books_summary(con), killwatch.books_summary(con))
plain = fresh()
check("books: a database from before them is no error", killwatch.books_matches(plain) == {}
      and killwatch.books_flags(plain) == {} and killwatch.books_summary(plain) is None)

# THE RECEIPTS LINE (0.57.0): what switching the receipts on would have done to
# those same paid kills - the rule kill/data.py's receipt_for() pays by.
backing = killwatch.receipts_backing(con, days=1, now=1000700)
check("receipts: each paid kill put to the books as the switch would",
      backing is not None and (backing["paid"], backing["backed"], backing["short"], backing["not_due"],
                               backing["never"]) == (16, 11, 1, 0, 4)
      and backing["never_by_enemy"] == {"bushmage": 3, "firesprite": 1}, backing)
check("receipts: the line says how many would have been paid, and which monsters the books never saw",
      killwatch.receipts_line(backing, days=1)
      == "receipts: of 16 kills paid in the last 1 days, 11 (68.8%) had the books' receipt; "
         "1 short, 0 not due, 4 never seen (bushmage 3, firesprite 1)", killwatch.receipts_line(backing, days=1))
edge = fresh(); books_tables(edge)
add_user(edge, 30, "edges")
judged(edge, 99, "bushmage", 2000000)
kill(edge, 30, "bushmage", 2000100); judged(edge, 30, "bushmage", 2000100 + killwatch.RECEIPT_WAIT_SECONDS + 1)
kill(edge, 30, "bushmage", 2000300); judged(edge, 30, "bushmage", 2000300 + killwatch.RECEIPT_WAIT_SECONDS - 3)
kill(edge, 30, "bushmage", 2000600); judged(edge, 30, "bushmage", 2000600 - killwatch.RECEIPT_SECONDS - 1)
kill(edge, 30, "bushmage", 2000900, slot=0)
edge.execute("INSERT INTO combat_kills (at, area, enemy_id, user_id, slot, verdict, damage)"
             " VALUES (2000899, 'field', 'bushmage', 30, 1, 'agreed', 100)")
edged = killwatch.receipts_backing(edge, days=1, now=2001000)
check("receipts: a death written while the game still asks pays; one after it gave up, or a minute before, "
      "or another character's, does not", (edged["paid"], edged["backed"], edged["never"]) == (4, 1, 3), edged)
check("receipts: no books, no line; no kills lately, a line that says so",
      killwatch.receipts_backing(plain) is None and killwatch.receipts_line(None) is None
      and killwatch.receipts_line(killwatch.receipts_backing(con, days=1, now=5000000), days=1)
      == "receipts: no kills paid in the last 1 days to measure them by")

fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
disk = sqlite3.connect(path)
for statement in con.iterdump():
    if statement.startswith(("CREATE TABLE", "INSERT")):
        disk.execute(statement)
disk.commit(); disk.close()
r = subprocess.run([sys.executable, os.path.join(HERE, "killwatch.py"), "--db", path],
                   capture_output=True, text=True)
rq = subprocess.run([sys.executable, os.path.join(HERE, "killwatch.py"), "--db", path, "--quiet"],
                    capture_output=True, text=True)
rd = subprocess.run([sys.executable, os.path.join(HERE, "killwatch.py"), "--db", path, "--days", "30000"],
                    capture_output=True, text=True)
os.remove(path)
check("books: CLI shows WATCHED and the books' line, and exits 0 - never an alarm",
      r.returncode == 0 and "WATCHED" in r.stdout and "books: 13 kill rows" in r.stdout, r.stdout[-400:])
check("books: --quiet stays silent on WATCHED alone", rq.returncode == 0 and rq.stdout.strip() == "",
      rq.stdout[:200])
check("receipts: the CLI prints the line, over the days asked for",
      "receipts: of 16 kills paid in the last 30000 days" in rd.stdout, rd.stdout[:600])

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
breach_rows = [(2, "bad", BURST_ENEMY, 200000 + i * BURST_GAP, 30) for i in range(BURST)]
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

# 9. THE SWELL (game 0.97.1). While a land stirs, and for an hour after, each
#    of its Big Field spots may hold up to four of a monster: a burst that the
#    plain ceiling calls impossible is honest then, and only then.
import world.services as world_rules  # noqa: E402

check("killwatch allows the live swell's most and grace",
      (killwatch.SWELL_MOST, killwatch.SWELL_GRACE_SECONDS)
      == (world_rules.SWELL_MOST, world_rules.SWELL_GRACE_SECONDS))
swell_enemy = next((e for e in sorted(gamedata.ENEMIES) if gamedata.swell_spots_for(e) > 0
                    and gamedata.ENEMIES[e].get("grants_rewards")
                    and int(gamedata.ENEMIES[e].get("element", 0) or 0) in range(1, 8)), None)
if swell_enemy is None:
    print("  skip  the swell: this catalogue places nothing in the Big Field")
else:
    land = int(gamedata.ENEMIES[swell_enemy]["element"])
    placed = gamedata.spawn_count_for(swell_enemy)
    plain = killwatch._ceiling_for(placed, killwatch.KILL_WINDOW_SECONDS, killwatch.KILL_RESPAWN_FLOOR_SECONDS)

    def swell_burst(stirs):
        con = fresh(); add_user(con, 9, "swell")
        con.execute("CREATE TABLE world_ticks (tick INTEGER PRIMARY KEY, element INTEGER,"
                    " started_at INTEGER, ends_at INTEGER, chosen_by TEXT)")
        for n, (element, a, b) in enumerate(stirs, 1):
            con.execute("INSERT INTO world_ticks VALUES (?, ?, ?, ?, '')", (n, element, a, b))
        for i in range(plain + 2):
            kill(con, 9, swell_enemy, 900000 + i)
        return killwatch.analyze(con)

    other = 1 if land != 1 else 2
    check("a burst past the plain ceiling while its land stirs: not flagged",
          "spawn ceiling breached" not in signals(swell_burst([(land, 890000, 905000)]), 9))
    check("  the same burst while another land stirs: IMPOSSIBLE",
          "spawn ceiling breached" in signals(swell_burst([(other, 890000, 910000)]), 9))
    check("  and long after its land's stir and grace hour: IMPOSSIBLE",
          "spawn ceiling breached" in signals(swell_burst([(land, 800000, 810000)]), 9))

# --------------------------------------------------------------------------- #
print("-" * 68)
print("PASSED %d  FAILED %d" % (_passed, _failed))
sys.exit(1 if _failed else 0)
