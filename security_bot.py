#!/usr/bin/env python3
"""
security_bot.py - a red-team harness that attacks a running Elusion server the
way a modified client would, then checks whether the money, life and progress
invariants still hold.

    python3 security_bot.py                 # spins up its own throwaway server
    python3 security_bot.py --players 20    # more fake players
    python3 security_bot.py --json          # machine-readable report as well

WHY THIS EXISTS, NEXT TO test_security.py
-----------------------------------------
test_security.py imports app.py and drives it through Flask's test client. It
proves each finding is closed in the code, and it is the right tool for that.

This is a different question: with the server actually RUNNING and listening on
a socket, can a client still break an invariant? An attacker has HTTP and
nothing else - no test client, no import, no fixtures - so every attack below
is a plain HTTP request. And the check that an attack failed is re-derived from
ground truth (the gold ledger, the accounts, the saves - read straight from
SQLite), never from the server's own "balanced: true", because a server that
could be tricked into minting gold could be tricked into misreporting it too.

So the two suites fail for different reasons. test_security.py goes red when a
fix is deleted. This goes red when the running server, however that happened,
lets an attack through - a bad deploy, a stale binary, a config that skips a
guard. It is the check you run against a box before you let players onto it.

SAFE BY CONSTRUCTION
--------------------
The bot MUTATES whatever it points at - it registers accounts and tries to
write gold. So by default it starts its OWN server on a scratch database and a
free port, attacks that, and deletes it. It physically cannot touch a real
database in that mode.

To attack an already-running server (a staging box), pass --server URL --db
PATH --allow-live. It refuses if the database is named elusion.db, because that
is the production name in this project and running a gold-minting attack
against live accounts is not a thing a "test" should make easy.

WHAT IT DECIDES
---------------
Each attack is BLOCKED (the server refused it) or BROKE (it got through). An
invariant that holds after the whole barrage is a pass. The kill event (E-3,
open) has no invariant to break - the server pays out a bounded number of
fabricated kills on purpose - so it is reported as a measured RATE and a
comparison to the documented ceiling, never as a pass or a fail.

Exit code is 0 only if nothing broke an invariant.
"""

import argparse
import json
import os
import random
import socket
import sqlite3
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OWNER_NAME = "botowner"


# =============================================================================
# HTTP - the only thing the attacker has
# =============================================================================

class Server:
    """An HTTP conversation with one Elusion server, plus a read-only handle on
    its database for ground-truth invariant checks."""

    def __init__(self, base_url, db_path):
        self.base = base_url.rstrip("/")
        self.db_path = db_path
        # NO PROXY. The container routes outbound HTTP through a proxy that does
        # not know about 127.0.0.1; without this every localhost call would be
        # bounced. An attacker on the same host talks to the socket directly.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None, token=None):
        """Returns (status_code, parsed_json_or_None). A refusal is an answer,
        not an exception - the whole point is to read what the server said."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            r = self._opener.open(req, timeout=30)
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, (json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                return e.code, None      # an HTML error page, e.g. a 500
        except urllib.error.URLError as e:
            return 0, {"error": str(e)}

    # --- ground truth, read straight from the database ---------------------
    # read-only (mode=ro), because the checker must never be able to change
    # what it is checking.
    def db(self):
        return sqlite3.connect("file:%s?mode=ro" % self.db_path, uri=True)

    def one(self, query, args=()):
        con = self.db()
        try:
            row = con.execute(query, args).fetchone()
            return row[0] if row else None
        finally:
            con.close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def spawn_server(port, db_path):
    """Start app.py on a scratch DB, wait for it to answer, return the Popen.

    ELUSION_DEBUG is explicitly UNSET: this harness must exercise the shipping
    configuration, and app.py now refuses to start under a debug flag with
    anything that looks like a deployment. FLASK_* likewise."""
    env = dict(os.environ)
    for key in ("ELUSION_DEBUG", "FLASK_DEBUG", "FLASK_ENV", "ELUSION_TRUSTED_PROXIES"):
        env.pop(key, None)
    env["ELUSION_DB"] = db_path
    env["ELUSION_OWNER"] = OWNER_NAME
    env["FLASK_RUN_PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, "-m", "flask", "--app", "app", "run", "--port", str(port)],
        cwd=HERE, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(80):
        if proc.poll() is not None:
            raise RuntimeError("server exited before it answered (code %s)" % proc.returncode)
        try:
            opener.open(base + "/api/status", timeout=1)
            return proc
        except urllib.error.HTTPError:
            return proc      # any HTTP answer means it is up
        except urllib.error.URLError:
            time.sleep(0.25)
    raise RuntimeError("server did not answer in time")


# =============================================================================
# FAKE PLAYERS
# =============================================================================

class Player:
    def __init__(self, srv, name, token, user_id):
        self.srv = srv
        self.name = name
        self.token = token
        self.user_id = user_id
        self.slot = 0

    def call(self, method, path, body=None):
        return self.srv.call(method, path, body, token=self.token)

    def make_character(self, class_id="warrior"):
        return self.call("PUT", "/api/save",
                         {"slot": self.slot, "class_id": class_id, "name": "Bot"})

    # --- ground truth, straight from this player's rows --------------------
    # Attacks verify here, not through a read endpoint. /api/character can 404
    # in a broken server state (a giant gold write does exactly that), and a
    # readback that silently returns 0 would report an attack as blocked while
    # it was landing. The database cannot be talked out of what it holds.
    def db_gold(self):
        return int(self.srv.one(
            "SELECT gold FROM saves WHERE user_id=? AND slot=?",
            (self.user_id, self.slot)) or 0)

    def db_lusions(self):
        return int(self.srv.one(
            "SELECT lusions FROM accounts WHERE user_id=?", (self.user_id,)) or 0)

    def db_item_qty(self, item_id):
        return int(self.srv.one(
            "SELECT COALESCE(SUM(quantity),0) FROM carry_items"
            " WHERE user_id=? AND slot=? AND item_id=?",
            (self.user_id, self.slot, item_id)) or 0)

    def db_skill_level(self, skill_id):
        return int(self.srv.one(
            "SELECT level FROM skills WHERE user_id=? AND slot=? AND skill_id=?",
            (self.user_id, self.slot, skill_id)) or 0)

    def db_equipment(self):
        raw = self.srv.one(
            "SELECT equipment FROM saves WHERE user_id=? AND slot=?",
            (self.user_id, self.slot)) or "{}"
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}


def spawn_players(srv, count):
    players = []
    tag = "".join(random.choice(string.ascii_lowercase) for _ in range(4))
    for i in range(count):
        name = "bot_%s_%d" % (tag, i)
        code, data = srv.call("POST", "/api/auth/register",
                              {"username": name, "password": "password123"})
        if code not in (200, 201) or "token" not in (data or {}):
            raise RuntimeError("could not register %s: %s %s" % (name, code, data))
        p = Player(srv, name, data["token"], int(data["user_id"]))
        p.make_character()
        players.append(p)
    return players


# =============================================================================
# THE ATTACKS
# =============================================================================
# Each returns a dict: {name, target, broke, detail}. `broke` True means the
# attack got through - the invariant it targets is violated. `target` names the
# finding, so a break points straight at the section of SECURITY_NOTES.md that
# is supposed to prevent it.

def attack_set_gold(p):
    # E-8. The one that was live in production once: gold was a writable status
    # field. Ask for a million, then read the saved balance from the database.
    before = p.db_gold()
    p.call("PUT", "/api/player/status", {"slot": p.slot, "gold": 1_000_000})
    after = p.db_gold()
    return {
        "name": "mint gold via /player/status",
        "target": "E-8", "broke": after > before,
        "detail": "saved gold %d -> %d (asked for 1,000,000)" % (before, after),
    }


def attack_set_lusions(p):
    # E-10. The same mistake one endpoint over. Bot accounts never earn lusions
    # legitimately, so any rise is a break.
    p.call("PUT", "/api/account/lusions", {"lusions": 500_000})
    have = p.db_lusions()
    return {
        "name": "mint lusions via /account/lusions",
        "target": "E-10", "broke": have > 0,
        "detail": "saved lusions after asking for 500,000: %d" % have,
    }


def attack_overheal(p):
    # E-9. Write an hp far above the class maximum; the clamp must trim it.
    # The clamp echoes the resulting hp in its OWN response, which is the
    # honest signal for a value the server derives rather than stores plainly.
    _, st = p.call("GET", "/api/player/status?slot=%d" % p.slot)
    max_hp = int((st or {}).get("max_hp", 0)) or 1
    _, resp = p.call("PUT", "/api/player/status", {"slot": p.slot, "hp": max_hp * 100})
    hp = int((resp or {}).get("hp", 0))
    return {
        "name": "set hp above maximum",
        "target": "E-9", "broke": hp > max_hp,
        "detail": "server returned hp %d against max %d" % (hp, max_hp),
    }


def attack_fabricate_item(p):
    # E-1. Write an item the server never granted into the backpack.
    # QUANTITY 1, not 99. A sword stacks to 1, so claiming 99 is refused on the
    # stack ceiling before the reconcile that actually owns this even runs -
    # testing the wrong guard. One fabricated sword is the real E-1 shape: an
    # item the server never granted, in a legal quantity.
    p.call("PUT", "/api/character/inventory",
           {"slot": p.slot,
            "inventory": [{"item_id": "embersword", "quantity": 1}]})
    got = p.db_item_qty("embersword")
    return {
        "name": "fabricate an item in the backpack",
        "target": "E-1", "broke": got > 0,
        "detail": "embersword saved in bag after claiming one: %d" % got,
    }


def attack_overclaim_skill(p):
    # E-2. Claim a skill level past the cap. attack is server-owned and dropped;
    # defense is still claimable UP TO the cap, so an OVER-cap claim is the
    # break, and a within-cap claim is expected to stick (documented gap).
    # THE SHAPE MATTERS. skills want {"defense": {"level": N, "xp": N}}; a bare
    # number is refused on shape and would never reach the cap - a test that
    # cannot fail. This sends a level past the cap in the real shape.
    p.call("PUT", "/api/character/skills",
           {"slot": p.slot,
            "skills": {"defense": {"level": 2_000_000, "xp": 0}}})
    level = p.db_skill_level("defense")
    return {
        "name": "claim a skill level past the cap",
        "target": "E-2", "broke": level > 99,
        "detail": "saved defense level after claiming 2,000,000: %d (cap 99)" % level,
    }


def attack_equip_unowned(p):
    # E-1 (equipment half). The client cannot choose the slot - the server
    # derives it from the item - so the only equipment exploit the API can even
    # express is equipping something you do not hold. The bot never earns
    # embersword (fabrication is blocked above), so this must be refused, and
    # nothing must end up worn.
    # ironsword is a level-1 warrior weapon: the bot's class and level clear the
    # equip_check, so the only thing that can refuse it is NOT HOLDING it - which
    # is the ownership guard this attack is here to probe. (embersword needs
    # level 22 and would be refused on level before ownership is even reached.)
    code, _ = p.call("POST", "/api/character/equip",
                     {"slot": p.slot, "item_id": "ironsword"})
    worn = p.db_equipment()
    broke = "ironsword" in json.dumps(worn)
    return {
        "name": "equip an item the character does not own",
        "target": "E-1", "broke": broke,
        "detail": "server said %d; equipment holds: %s"
                  % (code, json.dumps(worn) if worn else "{}"),
    }


def measure_kill_rate(p, drain=4.0, measure=6.0):
    # E-3, OPEN. There is no invariant here to break: the server pays out
    # fabricated kills on purpose, bounded by a token bucket and the spawn
    # ceiling. So this is a MEASUREMENT, not a verdict.
    #
    # THE SUSTAINED RATE, NOT THE OPENING BURST. A full token bucket empties in
    # the first second, so a short window catches a burst that then stops and
    # extrapolates it to a wildly overstated per-hour figure. What actually
    # bounds an hour of farming is the REFILL rate, so this drains the bucket
    # first (those kills discarded) and then measures how fast the server pays
    # out once it is being fed steadily. Both numbers are reported: the burst
    # is what you get in one moment, the sustained is what you get for an hour.
    def farm(seconds):
        paid = refused = 0
        start = time.time()
        while time.time() - start < seconds:
            code, _ = p.call("POST", "/api/combat/kill",
                             {"slot": p.slot, "enemy_id": "bushmage"})
            if code == 200:
                paid += 1
            else:
                refused += 1
        return paid, refused, time.time() - start

    burst, _, _ = farm(drain)                       # bucket + first refills, discarded
    paid, refused, elapsed = farm(measure)          # steady state
    sustained = int(paid / elapsed * 3600) if elapsed else 0
    under = sustained <= 5208
    return {
        "name": "farm fabricated kills (E-3 is open by design)",
        "target": "E-3", "broke": False, "rate": True,
        "detail": "one enemy id: opening burst ~%d, then %d paid / %d refused "
                  "over %.1fs -> ~%d/hour sustained, %s the ~5,208/hour ceiling. "
                  "A farmer rotating ids multiplies this by the id count - still "
                  "bounded by content, per E-3."
                  % (burst, paid, refused, elapsed, sustained,
                     "under" if under else "OVER"),
    }


ATTACKS = [
    attack_set_gold, attack_set_lusions, attack_overheal,
    attack_fabricate_item, attack_overclaim_skill, attack_equip_unowned,
]


# =============================================================================
# INVARIANTS - re-derived from the database, not from the server's report
# =============================================================================

def check_invariants(srv, players):
    checks = []

    # GOLD CONSERVATION. The equation the whole gold_ledger exists to hold:
    # every coin players hold was minted through gold_delta() and recorded.
    recorded = int(srv.one("SELECT COALESCE(SUM(delta),0) FROM gold_ledger") or 0)
    carried = int(srv.one("SELECT COALESCE(SUM(gold),0) FROM saves") or 0)
    banked = int(srv.one("SELECT COALESCE(SUM(bank_gold),0) FROM accounts") or 0)
    held = carried + banked
    checks.append({
        "name": "gold conservation: held == recorded in the ledger",
        "ok": held == recorded,
        "detail": "held %d (carried %d + banked %d) vs recorded %d, drift %d"
                  % (held, carried, banked, recorded, held - recorded),
    })

    # AND THE SERVER MUST AGREE IT IS BALANCED. If the ground-truth check above
    # passes but the dev endpoint says balanced=false, the endpoint is wrong;
    # if the reverse, the endpoint is lying. Either disagreement is a finding.
    owner_token = _owner_token(srv)
    endpoint_balanced = None
    if owner_token:
        code, supply = srv.call("GET", "/api/economy/supply", token=owner_token)
        if code == 200 and isinstance(supply, dict):
            endpoint_balanced = bool(supply.get("balanced"))
    checks.append({
        "name": "the server's own supply report agrees with the ledger",
        "ok": endpoint_balanced is None or endpoint_balanced == (held == recorded),
        "detail": "endpoint balanced=%s, ground truth balanced=%s"
                  % (endpoint_balanced, held == recorded),
    })

    # NO UNEARNED LUSIONS. Bot accounts never convert a duplicate pet and never
    # earn lusions any other way, so every bot lusion is unaccounted for.
    names = tuple(p.name for p in players)
    placeholders = ",".join("?" * len(names))
    bot_lusions = int(srv.one(
        "SELECT COALESCE(SUM(lusions),0) FROM accounts JOIN users"
        " ON users.id = accounts.user_id WHERE users.username IN (%s)" % placeholders,
        names) or 0)
    checks.append({
        "name": "no lusions on accounts that never earned any",
        "ok": bot_lusions == 0,
        "detail": "total lusions across %d bot accounts: %d" % (len(names), bot_lusions),
    })

    # NO SKILL ABOVE THE CAP on any bot character. The per-attack line reads
    # one skill on one character; this is the law across all of them.
    over_cap = srv.one(
        "SELECT COUNT(*) FROM skills JOIN users ON users.id = skills.user_id"
        " WHERE users.username IN (%s) AND skills.level > 99" % placeholders, names)
    checks.append({
        "name": "no skill level above the cap (99) anywhere",
        "ok": int(over_cap or 0) == 0,
        "detail": "skill rows over the cap across bot accounts: %d" % int(over_cap or 0),
    })

    # BOTS HOLD NOTHING. A fresh character starts with an empty backpack and
    # empty equipment (verified), and bot characters never loot, buy, trade or
    # earn - so ANY item in a bot's bag, or anything worn, is fabricated. This
    # is stronger than looking for one known item id: it catches whatever an
    # attacker fabricated, named or not. Covers E-1's backpack and the equip
    # ownership guard in one sweep.
    bag_rows = int(srv.one(
        "SELECT COUNT(*) FROM carry_items JOIN users ON users.id = carry_items.user_id"
        " WHERE users.username IN (%s)" % placeholders, names) or 0)
    worn = [p_.name for p_ in players if p_.db_equipment()]
    checks.append({
        "name": "bots hold nothing: empty backpacks and no equipment",
        "ok": bag_rows == 0 and not worn,
        "detail": "%d item rows across bot bags; characters wearing gear: %s"
                  % (bag_rows, ", ".join(worn) if worn else "none"),
    })

    # HP WITHIN MAX for every bot character. The clamp (E-9) must have held for
    # all of them, not just the one the attack read back.
    over = []
    for p in players:
        _, st = p.call("GET", "/api/player/status?slot=%d" % p.slot)
        if isinstance(st, dict) and int(st.get("hp", 0)) > int(st.get("max_hp", 1)):
            over.append("%s %d/%d" % (p.name, st["hp"], st["max_hp"]))
    checks.append({
        "name": "every character's hp is within its maximum",
        "ok": not over,
        "detail": "over-max characters: %s" % (", ".join(over) if over else "none"),
    })

    return checks


def _owner_token(srv):
    code, data = srv.call("POST", "/api/auth/register",
                          {"username": OWNER_NAME, "password": "password123"})
    if code in (200, 201) and data and "token" in data:
        return data["token"]
    code, data = srv.call("POST", "/api/auth/login",
                          {"username": OWNER_NAME, "password": "password123"})
    return (data or {}).get("token") if code == 200 else None


# =============================================================================
# RUN AND REPORT
# =============================================================================

def run(srv, player_count):
    players = spawn_players(srv, player_count)

    attack_results = []
    for attack in ATTACKS:
        attack_results.append(attack(players[0]))
    # a second player runs the whole barrage too, so an invariant that only
    # breaks in aggregate (two players, one shared bug) has a chance to show.
    if len(players) > 1:
        for attack in ATTACKS:
            attack(players[1])
    rate_results = [measure_kill_rate(players[0])]

    invariants = check_invariants(srv, players)
    return {
        "players": player_count,
        "attacks": attack_results,
        "rates": rate_results,
        "invariants": invariants,
    }


def report(result):
    lines = []
    lines.append("=" * 68)
    lines.append("  ELUSION SECURITY BOT - %d fake players" % result["players"])
    lines.append("=" * 68)

    lines.append("\nATTACKS (each tried over HTTP, as a modified client would)\n")
    any_broke = False
    for a in result["attacks"]:
        verdict = "BROKE " if a["broke"] else "blocked"
        if a["broke"]:
            any_broke = True
        lines.append("  [%s] %-42s %s" % (verdict, a["name"], "(%s)" % a["target"]))
        lines.append("           %s" % a["detail"])

    lines.append("\nMEASURED (E-3 is open by design - a rate, not a verdict)\n")
    for r in result["rates"]:
        lines.append("  [ rate  ] %s" % r["name"])
        lines.append("           %s" % r["detail"])

    lines.append("\nINVARIANTS (re-derived from the database, not the server's report)\n")
    any_invariant_broke = False
    for c in result["invariants"]:
        verdict = "hold" if c["ok"] else "BROKEN"
        if not c["ok"]:
            any_invariant_broke = True
        lines.append("  [ %-6s] %s" % (verdict, c["name"]))
        lines.append("           %s" % c["detail"])

    lines.append("\n" + "=" * 68)
    if any_invariant_broke:
        verdict = "FAIL - an invariant broke. The running server let an attack through."
    elif any_broke:
        verdict = ("ATTENTION - an attack got through but no invariant moved. "
                   "Read the detail; the guard may be narrower than the invariant.")
    else:
        verdict = "PASS - every attack was blocked and every invariant holds."
    lines.append("  " + verdict)
    lines.append("=" * 68)
    return "\n".join(lines), (any_invariant_broke or any_broke)


def main():
    ap = argparse.ArgumentParser(description="Red-team the Elusion server.")
    ap.add_argument("--players", type=int, default=6)
    ap.add_argument("--server", help="attack an already-running server instead of spawning one")
    ap.add_argument("--db", help="path to that server's database (for ground-truth checks)")
    ap.add_argument("--allow-live", action="store_true",
                    help="required with --server; still refused against elusion.db")
    ap.add_argument("--json", action="store_true", help="also print the raw result as JSON")
    ap.add_argument("--report", help="write the text report to this file too")
    args = ap.parse_args()

    proc = None
    workdir = None
    try:
        if args.server:
            if not args.db:
                ap.error("--server needs --db for the ground-truth invariant checks")
            if not args.allow_live:
                ap.error("--server needs --allow-live (and it is still refused against production)")
            if os.path.basename(args.db) == "elusion.db":
                ap.error("refusing to attack a database named elusion.db - that is the "
                         "production name. Point this at a staging copy.")
            srv = Server(args.server, args.db)
        else:
            workdir = tempfile.mkdtemp(prefix="elusion_bot_")
            db_path = os.path.join(workdir, "bot.db")
            port = free_port()
            proc = spawn_server(port, db_path)
            srv = Server("http://127.0.0.1:%d" % port, db_path)

        result = run(srv, args.players)
        text, bad = report(result)
        print(text)
        if args.report:
            with open(args.report, "w") as fh:
                fh.write(text + "\n")
        if args.json:
            print("\n" + json.dumps(result, indent=2))
        sys.exit(1 if bad else 0)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if workdir:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
