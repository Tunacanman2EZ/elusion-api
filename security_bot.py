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
Each attack is BLOCKED (the server refused it), BROKE (it got through), or
SKIPPED. Skipped is the important third state and it is not a pass: some
attacks need a precondition set up first - gold to found a guild with, a loot
bag to take twice, a character to be dead - and an attack that never reached
its target has proved nothing. A suite that renders those as "blocked" is
lying, and a red-team tool that lies is worse than no tool. An invariant that
holds after the whole barrage is a pass. The kill event (E-3,
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
import threading
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
        # A 5xx is not itself an invariant break, but it IS a finding: an
        # attacker who can make the shipping server throw an unhandled 500 has
        # found a crash, an info leak (a stack trace), or a DoS lever. Every
        # call records them; the run reports the tally. Locked because the
        # concurrency probes below hammer call() from many threads at once.
        self.crashes = []
        self._crash_lock = threading.Lock()

    def call(self, method, path, body=None, token=None, raw_body=None):
        """Returns (status_code, parsed_json_or_None). A refusal is an answer,
        not an exception - the whole point is to read what the server said.

        raw_body sends bytes verbatim (for the fuzzer's non-JSON and malformed
        payloads); body is the normal JSON path."""
        if raw_body is not None:
            data = raw_body
        else:
            data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            r = self._opener.open(req, timeout=30)
            raw = r.read()
            code, payload = r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                code, payload = e.code, (json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                code, payload = e.code, None      # an HTML error page, e.g. a 500
        except urllib.error.URLError as e:
            code, payload = 0, {"error": str(e)}
        if code >= 500:
            with self._crash_lock:
                self.crashes.append("%s %s -> %d" % (method, path, code))
        return code, payload

    def concurrent(self, n, make_call):
        """Fire make_call() from n threads that all start at the same instant,
        and return the list of (status, payload) results.

        The barrier is the point: a TOCTOU window between a guard's check and
        its write is only a few instructions wide, so the requests have to land
        together to squeeze it. Firing them in a loop would never overlap."""
        results = [None] * n
        barrier = threading.Barrier(n)

        def worker(i):
            try:
                barrier.wait(timeout=30)
                results[i] = make_call()
            except Exception as exc:               # a thread must not vanish silently
                results[i] = (-1, {"error": repr(exc)})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

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

    def all(self, query, args=()):
        """Every row's first column, as a list.

        one() answers "how many" and "what is it"; some findings are "WHICH
        ones", and a count of intruders that does not name them sends the
        reader back to SQLite to find out who."""
        con = self.db()
        try:
            return [row[0] for row in con.execute(query, args).fetchall()]
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

    def call(self, method, path, body=None, raw_body=None):
        return self.srv.call(method, path, body, token=self.token, raw_body=raw_body)

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


def attack_save_smuggle(p):
    # BULK-SAVE SMUGGLING. The granular endpoints are each guarded (E-1, E-2,
    # E-8, E-9); the bulk PUT /api/save writes a whole character at once, which
    # is exactly where authority tends to leak, because a wide write is easy to
    # under-validate. Send a legal save with contraband packed in beside it - a
    # level, a pile of gold and lusions, a fat inventory, worn gear, an over-cap
    # skill - then read the database back. Every one of these must be ignored:
    # level is server-owned, gold lives in the ledger, items are granted server
    # side. A field that lands here is a hole the per-field tests can't see.
    before_gold = p.db_gold()
    p.call("PUT", "/api/save", {
        "slot": p.slot, "class_id": "warrior", "name": "Bot",
        "level": 99, "gold": 1_000_000, "lusions": 1_000_000,
        "inventory": [{"item_id": "embersword", "quantity": 1}],
        "equipment": {"weapon": "embersword"},
        "skills": {"defense": {"level": 2_000_000, "xp": 0}},
    })
    leaked = []
    if p.db_gold() > before_gold:
        leaked.append("gold")
    if p.db_lusions() > 0:
        leaked.append("lusions")
    if p.db_item_qty("embersword") > 0:
        leaked.append("item")
    if p.db_skill_level("defense") > 99:
        leaked.append("skill")
    if "embersword" in json.dumps(p.db_equipment()):
        leaked.append("equipment")
    level = int(p.srv.one(
        "SELECT level FROM saves WHERE user_id=? AND slot=?",
        (p.user_id, p.slot)) or 1)
    if level > 1:
        leaked.append("level")
    return {
        "name": "smuggle contraband through the bulk save",
        "target": "E-1/E-8", "broke": bool(leaked),
        "detail": "fields that leaked through /api/save: %s"
                  % (", ".join(leaked) if leaked else "none"),
    }


def attack_bank_fabricate(p):
    # E-1, the bank half. PUT /api/account/bank once replaced the whole bank
    # with whatever arrived - fabrication straight into storage. It is meant to
    # only REORDER what is already banked, and a bot has banked nothing, so a
    # fabricated stack must not survive to a row in bank_items.
    p.call("PUT", "/api/account/bank",
           {"bank_inventory": [{"item_id": "embersword", "quantity": 99}]})
    rows = int(p.srv.one(
        "SELECT COUNT(*) FROM bank_items WHERE user_id=?", (p.user_id,)) or 0)
    return {
        "name": "fabricate an item straight into the bank",
        "target": "E-1", "broke": rows > 0,
        "detail": "bank_items rows after claiming a bankful of embers: %d" % rows,
    }


def attack_negative_overflow(p):
    # BOUNDARY AND TYPE CONFUSION. The other attacks send oversized-but-typical
    # values; this one sends the values a validator written for the happy path
    # forgets - negatives, floats where an int is meant, and scientific-notation
    # strings that int() would choke on but float() would swallow. If any of
    # them slips a negative balance or an over-cap skill past the guards, the
    # global invariants below will also catch it; this names the door it came
    # through. None of these may stick, and none may 500 (a crash is a finding
    # in its own right, tallied on the Server).
    probes = [
        ("PUT", "/api/player/status", {"slot": p.slot, "hp": -9999}),
        ("PUT", "/api/player/status", {"slot": p.slot, "hp": 1e18}),
        ("PUT", "/api/character/skills",
         {"slot": p.slot, "skills": {"defense": {"level": 1e18, "xp": 0}}}),
        ("PUT", "/api/character/skills",
         {"slot": p.slot, "skills": {"defense": {"level": "1e9", "xp": 0}}}),
        ("PUT", "/api/save",
         {"slot": -1, "class_id": "warrior", "name": "Neg"}),
        ("PUT", "/api/save",
         {"slot": 999999, "class_id": "warrior", "name": "Big"}),
        ("PUT", "/api/character/inventory",
         {"slot": p.slot,
          "inventory": [{"item_id": "embersword", "quantity": -1}]}),
    ]
    for method, path, body in probes:
        p.call(method, path, body)
    _, st = p.call("GET", "/api/player/status?slot=%d" % p.slot)
    hp = int((st or {}).get("hp", 0))
    max_hp = int((st or {}).get("max_hp", 1)) or 1
    defense = p.db_skill_level("defense")
    ember = p.db_item_qty("embersword")
    bad = []
    if hp < 0 or hp > max_hp:
        bad.append("hp %d/%d" % (hp, max_hp))
    if defense > 99:
        bad.append("defense %d" % defense)
    if ember != 0:
        bad.append("ember qty %d" % ember)
    return {
        "name": "negative / overflow / wrong-type values",
        "target": "boundary", "broke": bool(bad),
        "detail": "bad state after hostile numerics: %s"
                  % (", ".join(bad) if bad else "none"),
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
    attack_save_smuggle, attack_bank_fabricate, attack_negative_overflow,
]


# =============================================================================
# CROSS-ACCOUNT AND PRIVILEGE - attacks that need a second identity
# =============================================================================
# Everything above attacks the bot's OWN rows. These probe the other half of
# the model: can one player's token reach across to another account, or up to a
# staff power it was never granted? The server derives the acting user from the
# bearer token (require_auth -> g.user), so the interesting question is whether
# any endpoint trusts a username/subject in the body or URL over that token.

def attack_staff_privilege(srv, attacker, victim):
    # PRIVILEGE ESCALATION. The staff endpoints are gated by require_role /
    # require_owner, which a fresh account does not clear. A plain bot pointing
    # /api/staff/kick at the owner (or another player) must be refused, and -
    # this is the part a status code alone won't tell you - the victim must be
    # untouched: not banned, sessions not ended. Verified from the users table,
    # not from the refusal, because a server that mis-checked the role could
    # also mis-report the refusal.
    victim_banned_before = int(srv.one(
        "SELECT is_banned FROM users WHERE username=?", (victim.name,)) or 0)
    code_kick, _ = attacker.call("POST", "/api/staff/kick",
                                 {"username": victim.name, "reason": "hostile"})
    # also try to ban the owner account itself - the highest-value target.
    attacker.call("POST", "/api/staff/kick",
                  {"username": OWNER_NAME, "reason": "hostile"})
    code_look, _ = attacker.call("GET", "/api/staff/user/%s" % victim.name)
    victim_banned_after = int(srv.one(
        "SELECT is_banned FROM users WHERE username=?", (victim.name,)) or 0)
    attacker_role = srv.one(
        "SELECT role FROM users WHERE username=?", (attacker.name,))
    broke = (code_kick == 200 or code_look == 200
             or victim_banned_after > victim_banned_before
             or attacker_role not in (None, "player"))
    return {
        "name": "wield a staff power without the role",
        "target": "authz", "broke": broke,
        "detail": "kick said %s, lookup said %s; victim is_banned %d->%d; "
                  "attacker role=%s"
                  % (code_kick, code_look, victim_banned_before,
                     victim_banned_after, attacker_role),
    }


def attack_owner_economy(attacker):
    # OWNER-ONLY READ. /api/economy/supply is the dev view of the whole gold
    # supply, gated to the owner. A plain player must not be able to read it -
    # both because it is not theirs and because the numbers are a map of where
    # the money is. A 200 here is a horizontal-to-vertical read leak.
    code, _ = attacker.call("GET", "/api/economy/supply")
    return {
        "name": "read the owner-only economy supply",
        "target": "authz", "broke": code == 200,
        "detail": "a non-owner GET /api/economy/supply returned %s "
                  "(anything but 200/401/404-shaped refusal is the concern)" % code,
    }


# =============================================================================
# THE SYSTEMS THIS BOT HAD NEVER SEEN
# =============================================================================
# Everything above predates the guild system, the coin denominations, the
# owner's gold grants, the revive floor and staff message deletion. None of
# those had a single attack pointed at them, which is the worst kind of gap in
# a red-team tool: the report came back PASS and the pass meant "nothing I
# know how to try got through".
#
# WHAT THESE HAVE IN COMMON: every one is an authorisation question rather than
# a validation question. The attacks above mostly ask "will the server take a
# number I made up"; these ask "will the server let me act on something that is
# not mine" - somebody else's guild, somebody else's message, a loot bag I have
# already emptied, an owner power I was never granted. That is the half of the
# model that grows every time a social feature lands.


def _owner_player(srv):
    """A Player handle on the owner account, with a character in slot 0.

    THE OWNER IS THE ONLY ACCOUNT THAT CAN LEGITIMATELY GET GOLD HERE, and it
    can only give it to ITSELF: /api/staff/gold reads the acting user from the
    token and takes a slot, with no field for a target. The first version of
    this helper passed a username and got a 404 for its trouble - which the
    skip machinery reported honestly rather than papering over, and which is
    the reason the skip machinery exists.

    So the owner is the one who founds the guild the attacks try to seize.
    That is the better test anyway: the highest-value guild on the server is
    the one belonging to the account that cannot be banned.
    """
    token = _owner_token(srv)
    if not token:
        return None
    user_id = srv.one("SELECT id FROM users WHERE username = ?", (OWNER_NAME,))
    if user_id is None:
        return None
    owner = Player(srv, OWNER_NAME, token, int(user_id))
    owner.make_character()
    return owner


def _fund(owner, amount):
    """Mint gold into the owner's own purse, through the ledger.

    Over HTTP and through gold_delta() rather than an UPDATE, so the coins are
    recorded. A bare write here would show up at the end of the run as a
    conservation break, and the attacks would get the blame for the harness's
    own setup."""
    if owner is None:
        return False
    code, _ = owner.call("POST", "/api/staff/gold",
                         {"slot": owner.slot, "amount": int(amount)})
    return code == 200 and owner.db_gold() >= amount


def attack_staff_gold_mint(attacker):
    # THE OWNER'S MINT, AIMED AT BY SOMEBODY WHO IS NOT THE OWNER.
    #
    # /api/staff/gold is the one endpoint in the game that creates gold from
    # nothing on purpose - it exists so the owner can test economy changes
    # without farming for an hour. require_owner is the only thing between it
    # and an infinite money printer, and require_owner is one decorator: delete
    # the line and every route still imports, every test that does not
    # specifically check authorisation still passes, and the server quietly
    # becomes a faucet.
    #
    # CHECKED IN THE LEDGER, NOT THE PURSE. A grant that landed and was then
    # spent would leave the balance looking innocent; the staff_gold row would
    # still be there. The ledger is what cannot be talked out of it.
    before = attacker.db_gold()
    code_self, _ = attacker.call("POST", "/api/staff/gold",
                                 {"username": attacker.name,
                                  "slot": attacker.slot, "amount": 1_000_000})
    # And at the bank, which is the pile death cannot touch.
    attacker.call("POST", "/api/staff/gold",
                  {"slot": attacker.slot, "amount": 1_000_000, "bank": True})
    minted = int(attacker.srv.one(
        "SELECT COALESCE(SUM(delta),0) FROM gold_ledger"
        " JOIN users ON users.id = gold_ledger.user_id"
        " WHERE users.username = ? AND gold_ledger.reason = 'staff_gold'",
        (attacker.name,)) or 0)
    after = attacker.db_gold()
    return {
        "name": "print gold with the owner's staff grant",
        "target": "authz", "broke": code_self == 200 or minted > 0 or after > before,
        "detail": "grant said %s; staff_gold minted into this account: %d; "
                  "purse %d -> %d" % (code_self, minted, before, after),
    }


def attack_guild_found_free(p):
    # GUILD FOUNDING IS A GOLD SINK, and a sink that can be skipped is not one.
    #
    # Founding costs GUILD_FOUND_COST, taken carry-first then from the bank. A
    # bot has nothing, so every one of these must be refused - including the
    # two shapes that look like an accounting bug rather than an attack: a
    # negative amount is not offered by the endpoint at all, but a guild that
    # somehow got created would leave a row, and the row is what this reads.
    before_rows = int(p.srv.one("SELECT COUNT(*) FROM guilds") or 0)
    code, data = p.call("POST", "/api/guild/create",
                        {"name": "Freeloaders", "slot": p.slot})
    # Same again immediately - a create that half-succeeded (row written, gold
    # not taken) would show up as a second refusal for the wrong reason.
    p.call("POST", "/api/guild/create", {"name": "Freeloaders Two", "slot": p.slot})
    after_rows = int(p.srv.one("SELECT COUNT(*) FROM guilds") or 0)
    member = int(p.srv.one(
        "SELECT COUNT(*) FROM guild_members WHERE user_id = ?", (p.user_id,)) or 0)
    return {
        "name": "found a guild without paying for it",
        "target": "economy", "broke": after_rows > before_rows or member > 0,
        "detail": "create said %s (%s); guild rows %d -> %d; bot is in %d guild(s)"
                  % (code, (data or {}).get("error", ""), before_rows,
                     after_rows, member),
    }


def attack_guild_takeover(srv, attacker, victim):
    # SOMEBODY ELSE'S GUILD, WITH NO MEMBERSHIP IN IT.
    #
    # Five separate reaches, because they fail in different places and a server
    # can get four of them right:
    #   - disband it by name   (owner-only override)
    #   - disband it plainly   (leader-only)
    #   - promote yourself to leader
    #   - kick the actual leader out of it
    #   - accept an invitation nobody ever sent
    #
    # THE LAST ONE IS THE INTERESTING ONE. Invite-then-accept means there is a
    # row somewhere saying "this player may join"; a respond handler that trusts
    # the guild name in the body over that row lets anyone walk into any guild.
    # It is also the one a status code will not tell you about, because a server
    # that added the membership and THEN errored would still answer 400.
    if not _fund(victim, 6000):
        return {
            "name": "seize a guild the attacker is not in",
            "target": "authz", "broke": False, "skipped": True,
            "detail": "could not fund the guild's founder, so nothing was proved",
        }

    code_make, _ = victim.call("POST", "/api/guild/create",
                               {"name": "Redteam Keep", "slot": victim.slot})
    if code_make != 200:
        return {
            "name": "seize a guild the attacker is not in",
            "target": "authz", "broke": False, "skipped": True,
            "detail": "victim could not found a guild (%s), so nothing was proved"
                      % code_make,
        }

    guild_id = srv.one("SELECT guild_id FROM guild_members WHERE user_id = ?",
                       (victim.user_id,))
    reaches = {
        "disband by name": attacker.call(
            "POST", "/api/guild/disband", {"name": "Redteam Keep"})[0],
        "disband plainly": attacker.call("POST", "/api/guild/disband", {})[0],
        "promote self": attacker.call(
            "POST", "/api/guild/rank",
            {"username": attacker.name, "rank": "leader"})[0],
        "kick the leader": attacker.call(
            "POST", "/api/guild/kick", {"username": victim.name})[0],
        "accept an invite nobody sent": attacker.call(
            "POST", "/api/guild/respond",
            {"guild": "Redteam Keep", "accept": True})[0],
    }

    still_there = int(srv.one("SELECT COUNT(*) FROM guilds WHERE id = ?",
                              (guild_id,)) or 0)
    attacker_in = int(srv.one(
        "SELECT COUNT(*) FROM guild_members WHERE user_id = ?",
        (attacker.user_id,)) or 0)
    victim_rank = srv.one("SELECT rank FROM guild_members WHERE user_id = ?",
                          (victim.user_id,))
    landed = [what for what, code in reaches.items() if code == 200]
    broke = (still_there == 0 or attacker_in > 0
             or victim_rank != "leader" or bool(landed))
    return {
        "name": "seize a guild the attacker is not in",
        "target": "authz", "broke": broke,
        "detail": "reaches that returned 200: %s; guild still exists: %s; "
                  "attacker is in %d guild(s); the founder is still '%s'"
                  % (", ".join(landed) if landed else "none",
                     bool(still_there), attacker_in, victim_rank),
    }


def attack_guild_chat_leak(srv, attacker, victim):
    # GUILD CHAT IS THE ONE CHANNEL WITH A MEMBERSHIP BOUNDARY on it, and a
    # boundary on a read path is easy to get subtly wrong: filter by the
    # reader's CURRENT guild and a player who leaves keeps reading; forget the
    # filter entirely and everyone reads everything.
    #
    # The victim is in a guild (the takeover attack founded it), the attacker
    # is in none. A bot with no guild asking for the guild channel should get
    # nothing at all - not an error necessarily, but not somebody's words.
    secret = "redteam-canary-%d" % int(time.time())
    code_send, _ = victim.call("POST", "/api/chat/send",
                               {"channel": "guild", "body": secret})
    if code_send != 200:
        return {
            "name": "read a guild's chat from outside it",
            "target": "authz", "broke": False, "skipped": True,
            "detail": "the victim could not post to guild chat (%s), so there "
                      "was nothing to leak" % code_send,
        }
    code_read, data = attacker.call("GET", "/api/chat?channel=guild")
    body = json.dumps(data or {})
    leaked = secret in body
    return {
        "name": "read a guild's chat from outside it",
        "target": "authz", "broke": leaked,
        "detail": "guild-channel read by a guildless account said %s and %s the "
                  "other guild's message"
                  % (code_read, "LEAKED" if leaked else "did not contain"),
    }


def attack_chat_delete_others(srv, attacker, victim):
    # DELETING SOMEBODY ELSE'S WORDS. Staff can remove any message; a player
    # cannot. The check is not the status code - it is whether the row is still
    # there afterwards, because a handler that deletes first and authorises
    # second returns 403 and takes the message with it.
    marker = "redteam-delete-me-%d" % int(time.time())
    code_send, _ = victim.call("POST", "/api/chat/send",
                               {"channel": "world", "body": marker})
    if code_send != 200:
        return {
            "name": "delete another player's chat message",
            "target": "authz", "broke": False, "skipped": True,
            "detail": "the victim could not post (%s), so there was nothing to "
                      "delete" % code_send,
        }
    message_id = srv.one("SELECT id FROM chat_messages WHERE body = ?", (marker,))
    if message_id is None:
        return {
            "name": "delete another player's chat message",
            "target": "authz", "broke": False, "skipped": True,
            "detail": "could not find the victim's message to aim at",
        }
    code_del, _ = attacker.call("POST", "/api/chat/delete", {"id": int(message_id)})
    # Someone else's id, and then a sweep of low ids, in case the handler
    # authorises the one it was given and not the one it acts on.
    for probe_id in range(1, 6):
        attacker.call("POST", "/api/chat/delete", {"id": probe_id})
    survived = int(srv.one("SELECT COUNT(*) FROM chat_messages WHERE id = ?",
                           (message_id,)) or 0)
    return {
        "name": "delete another player's chat message",
        "target": "authz", "broke": code_del == 200 or survived == 0,
        "detail": "delete said %s; the victim's message is %s"
                  % (code_del, "GONE" if survived == 0 else "still there"),
    }


def attack_loot_double_take(p):
    # TAKING THE SAME COIN TWICE.
    #
    # This one got dangerous the day the denomination ladder landed. A loot bag
    # position used to be worth one coin; now a single position can hold a
    # platinum coin worth 100,000 gold, and /api/loot/take credits
    # quantity * value. So a take that does not clear the position - or clears
    # it after crediting rather than before - is not a small dupe any more.
    #
    # E-3 IS OPEN ON PURPOSE, so the bot can fabricate a kill and get a real
    # bag. That is the point: the open finding is the delivery mechanism for
    # testing the closed one.
    for _ in range(6):
        p.call("POST", "/api/combat/kill", {"slot": p.slot, "enemy_id": "bushmage"})
    # THE ID COMES OUT OF THE DATABASE, because /api/loot/bag reads a bag you
    # can already name and the bot has no other way to learn one. Reading
    # ground truth to set an attack up is what every precondition here does;
    # the attack itself is still nothing but HTTP.
    bag_id = p.srv.one(
        "SELECT bag_id FROM loot_bags WHERE user_id = ? AND slot = ?"
        " ORDER BY created_at DESC", (p.user_id, p.slot))
    if not bag_id:
        return {
            "name": "take the same loot twice",
            "target": "E-1/E-8", "broke": False, "skipped": True,
            "detail": "six fabricated kills produced no loot bag to attack",
        }

    before = p.db_gold()
    first, _ = p.call("POST", "/api/loot/take", {"bag_id": bag_id, "position": 0})
    middle = p.db_gold()
    # The same position again, four more times, and once concurrently - a
    # guard that is present but not atomic only shows under the overlap.
    for _ in range(4):
        p.call("POST", "/api/loot/take", {"bag_id": bag_id, "position": 0})
    p.srv.concurrent(8, lambda: p.call(
        "POST", "/api/loot/take", {"bag_id": bag_id, "position": 0}))
    after = p.db_gold()
    gained_once = middle - before
    gained_again = after - middle
    return {
        "name": "take the same loot twice",
        "target": "E-1/E-8", "broke": gained_again > 0,
        "detail": "first take said %s and paid %d; twelve more takes of the same "
                  "position paid %d more (want 0)"
                  % (first, gained_once, gained_again),
    }


def attack_revive_underpay(p):
    # THE REVIVE FLOOR, FROM BELOW.
    #
    # The gold revive charges a share of everything held, with a minimum under
    # it. Below the minimum the route is refused outright - which means there
    # is now a branch that says "no" to a paying customer, and a branch that
    # says no is a branch that can be talked round. The two shapes worth trying
    # are the ones a modified client would actually send: a cost of its own
    # choosing, and a currency name the server does not know.
    #
    # DYING IS DONE HONESTLY. Reporting your own hp DOWN is legitimate - taking
    # damage is a thing clients tell the server about - so no database write is
    # needed to set this up, and the attack stays pure HTTP.
    p.call("PUT", "/api/player/status", {"slot": p.slot, "hp": 0})
    # `or -1` HERE WOULD BE A BUG, and was one: hp 0 is exactly the state this
    # attack needs, and 0 is falsy, so `srv.one(...) or -1` turned a character
    # that had died correctly into "could not get it to 0 hp" and skipped the
    # attack forever. A missing row and a dead character are different answers
    # and only `is None` can tell them apart.
    raw_hp = p.srv.one("SELECT hp FROM saves WHERE user_id=? AND slot=?",
                       (p.user_id, p.slot))
    hp_now = -1 if raw_hp is None else int(raw_hp)
    if hp_now != 0:
        return {
            "name": "revive for less than the floor",
            "target": "economy", "broke": False, "skipped": True,
            "detail": "could not get the character to 0 hp (hp=%d)" % hp_now,
        }

    gold_before = p.db_gold()
    attempts = {
        "plain gold revive while broke": p.call(
            "POST", "/api/character/revive", {"slot": p.slot, "pay": "gold"})[0],
        "naming its own price": p.call(
            "POST", "/api/character/revive",
            {"slot": p.slot, "pay": "gold", "cost": 1})[0],
        "a currency that does not exist": p.call(
            "POST", "/api/character/revive",
            {"slot": p.slot, "pay": "doubloons"})[0],
        "no payment named at all": p.call(
            "POST", "/api/character/revive", {"slot": p.slot})[0],
    }
    raw_after = p.srv.one("SELECT hp FROM saves WHERE user_id=? AND slot=?",
                          (p.user_id, p.slot))
    hp_after = 0 if raw_after is None else int(raw_after)
    landed = [what for what, code in attempts.items() if code == 200]
    return {
        "name": "revive for less than the floor",
        "target": "economy", "broke": hp_after > 0 or bool(landed),
        "detail": "attempts that returned 200: %s; hp after: %d (want 0); gold "
                  "%d -> %d" % (", ".join(landed) if landed else "none",
                                hp_after, gold_before, p.db_gold()),
    }


# =============================================================================
# FUZZ AND RACE - measured sweeps, not single verdicts
# =============================================================================

def fuzz_sweep(attacker):
    # ROBUSTNESS. Throw the shapes a happy-path parser forgets at every write
    # endpoint: a non-JSON body, a JSON array where an object is expected, null
    # fields, a huge string, and SQL/'"><' injection markers in the string
    # fields that reach a query or a template. The bar is simple and absolute:
    # the shipping server must answer every one with a clean 4xx, never a 5xx.
    # A 500 is an unhandled path - a stack trace to leak or a lever to hang the
    # box. The count of them is the finding. (SQL here is placebo-safe anyway:
    # app.py parameterises, and this only confirms it stays that way.)
    endpoints = [
        ("PUT", "/api/player/status"),
        ("PUT", "/api/character/inventory"),
        ("PUT", "/api/character/skills"),
        ("PUT", "/api/account/bank"),
        ("PUT", "/api/save"),
        ("POST", "/api/character/consume"),
        ("POST", "/api/character/revive"),
        ("POST", "/api/combat/kill"),
        ("POST", "/api/staff/kick"),
        # EVERYTHING BELOW POSTDATES THE ORIGINAL LIST. Guilds, the picture
        # upload, staff gold and loot taking all shipped after this sweep was
        # written, so none of them had ever been handed a malformed body. A
        # route that 500s on a JSON array is a stack trace to read and a lever
        # to hang the box, and it does not care how recently it was added.
        ("POST", "/api/staff/gold"),
        ("POST", "/api/guild/create"),
        ("POST", "/api/guild/invite"),
        ("POST", "/api/guild/respond"),
        ("POST", "/api/guild/kick"),
        ("POST", "/api/guild/rank"),
        ("POST", "/api/guild/disband"),
        ("POST", "/api/guild/leave"),
        ("POST", "/api/chat/send"),
        ("POST", "/api/chat/delete"),
        ("POST", "/api/chat/image"),
        ("POST", "/api/chat/upload"),
        ("POST", "/api/loot/take"),
        ("POST", "/api/trade/offer"),
        ("POST", "/api/trade/update"),
        ("POST", "/api/trade/confirm"),
    ]
    payloads = [
        ("non-JSON bytes", dict(raw_body=b"not json at all {[")),
        ("array not object", dict(body=[1, 2, 3])),
        ("nulls", dict(body={"slot": None, "item_id": None, "username": None})),
        ("huge string", dict(body={"slot": 0, "name": "A" * 100000,
                                   "item_id": "A" * 100000})),
        ("injection markers", dict(body={
            "slot": 0, "name": "'; DROP TABLE users;--",
            "item_id": "\" OR 1=1 --", "username": "<script>x</script>"})),
        ("deeply nested", dict(body={"slot": {"slot": {"slot": {"slot": 0}}}})),
    ]
    before = len(attacker.srv.crashes)
    tried = 0
    by_shape = {}       # which malformed shape provoked the crash, and how often
    for method, path in endpoints:
        for label, kw in payloads:
            mark = len(attacker.srv.crashes)
            attacker.call(method, path, **kw)
            if len(attacker.srv.crashes) > mark:
                by_shape[label] = by_shape.get(label, 0) + 1
            tried += 1
    crashed = len(attacker.srv.crashes) - before
    shapes = ", ".join("'%s' x%d" % (k, v)
                       for k, v in sorted(by_shape.items(), key=lambda kv: -kv[1]))
    return {
        "name": "fuzz every write endpoint with malformed input",
        "target": "robustness", "broke": False, "rate": True,
        "crashed": crashed,
        "detail": "%d malformed requests across %d endpoints -> %d server 5xx "
                  "(want 0).%s"
                  % (tried, len(endpoints), crashed,
                     " clean." if crashed == 0
                     else " The shapes that crash it: " + shapes
                          + ". A 500 leaks a stack trace and is a DoS lever; "
                            "these want a clean 400."),
    }


def race_storm(srv, p, width=25):
    # RACE CONDITIONS. Every attack above is one request at a time, so it can
    # only find a guard that is missing - never one that is present but not
    # ATOMIC. This fires the money-minting writes from `width` threads that all
    # release on the same barrier, hunting the TOCTOU window between a guard's
    # check and its write. Nothing here should ever land; the global invariants
    # below are re-derived AFTER this, so a coin or an item that slipped through
    # in the overlap shows up as a conservation or holds-nothing break.
    #
    # Also races the rate limiter directly: a token bucket that reads-then-
    # writes without a lock can pay a burst of `width` kills where it should
    # pay a few. The paid count is measured, not judged - E-3 pays on purpose -
    # but a number far above the single-request burst points at a bucket that
    # isn't atomic.
    #
    # A CAVEAT WORTH KNOWING: the bot's own throwaway server is `flask run`,
    # which is single-threaded, so requests serialize there and a true TOCTOU
    # cannot manifest locally - against the spawned box this mostly proves the
    # serial burst still conserves. Point it at the threaded production/staging
    # server (--server URL) to actually exercise the race.
    gold_before = p.db_gold()
    storms = [
        ("mint gold", lambda: p.call(
            "PUT", "/api/player/status", {"slot": p.slot, "gold": 1_000_000})),
        ("fabricate item", lambda: p.call(
            "PUT", "/api/character/inventory",
            {"slot": p.slot, "inventory": [{"item_id": "embersword", "quantity": 1}]})),
        ("bank fabricate", lambda: p.call(
            "PUT", "/api/account/bank",
            {"bank_inventory": [{"item_id": "embersword", "quantity": 99}]})),
        ("over-cap skill", lambda: p.call(
            "PUT", "/api/character/skills",
            {"slot": p.slot, "skills": {"defense": {"level": 2_000_000, "xp": 0}}})),
    ]
    for _label, fn in storms:
        srv.concurrent(width, fn)
    kill_results = srv.concurrent(
        width, lambda: p.call("POST", "/api/combat/kill",
                              {"slot": p.slot, "enemy_id": "bushmage"}))
    kills_paid = sum(1 for code, _ in kill_results if code == 200)
    gold_after = p.db_gold()
    return {
        "name": "race the write guards and the rate limiter (%d-wide)" % width,
        "target": "TOCTOU", "broke": False, "rate": True,
        "detail": "%d concurrent kills paid %d at once (E-3 pays some; a burst "
                  "near %d would suggest a non-atomic bucket). Storm effects are "
                  "judged by the invariants below; bot gold moved %d -> %d."
                  % (width, kills_paid, width, gold_before, gold_after),
    }


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

    # NO BOT HOLDS A ROLE. role defaults to 'player'; only ELUSION_OWNER is
    # owner, and nothing a request can send grants a role. A bot at 'mod' or
    # 'owner' is a privilege escalation that landed.
    escalated = srv.one(
        "SELECT COUNT(*) FROM users WHERE username IN (%s)"
        " AND role NOT IN ('player','')" % placeholders, names)
    checks.append({
        "name": "no bot account holds a staff role",
        "ok": int(escalated or 0) == 0,
        "detail": "bot accounts with a non-player role: %d" % int(escalated or 0),
    })

    # NO BOT WAS BANNED. Nothing a bot did earns a ban, and no bot has the power
    # to ban, so any is_banned among them is a staff power that misfired across
    # accounts - or fired from an account that should never have held it.
    banned = srv.one(
        "SELECT COUNT(*) FROM users WHERE username IN (%s)"
        " AND is_banned <> 0" % placeholders, names)
    checks.append({
        "name": "no bot account got banned during the barrage",
        "ok": int(banned or 0) == 0,
        "detail": "bot accounts marked banned: %d" % int(banned or 0),
    })

    # NO FABRICATED BANK ITEMS. Same law as the backpack, one table over: a bot
    # banks nothing legitimately, so any row is fabricated storage.
    bank_rows = int(srv.one(
        "SELECT COUNT(*) FROM bank_items JOIN users ON users.id = bank_items.user_id"
        " WHERE users.username IN (%s)" % placeholders, names) or 0)
    checks.append({
        "name": "bots hold nothing in the bank either",
        "ok": bank_rows == 0,
        "detail": "bank_items rows across bot accounts: %d" % bank_rows,
    })

    # NO NEGATIVE BALANCES ANYWHERE. A guard that clamps the top but forgets the
    # bottom lets a negative through, and a negative subtracted is a positive
    # minted. Carried and banked gold must both stay non-negative.
    min_gold = int(srv.one("SELECT COALESCE(MIN(gold), 0) FROM saves") or 0)
    min_bank = int(srv.one("SELECT COALESCE(MIN(bank_gold), 0) FROM accounts") or 0)
    checks.append({
        "name": "no negative gold in any save or bank",
        "ok": min_gold >= 0 and min_bank >= 0,
        "detail": "min carried gold %d, min banked gold %d" % (min_gold, min_bank),
    })

    # NO BOT SITS IN A GUILD. Bot accounts never pay the founding cost and are
    # never invited by anybody; the takeover attack deliberately tries to walk
    # into one. A membership row on a bot is that attack having landed, and it
    # is worth asserting across ALL of them rather than only the attacker -
    # the same reason every other law here is re-derived over the whole set.
    #
    # THE OWNER IS THE EXCEPTION AND IT IS NOT A BOT. attack_guild_takeover has
    # the OWNER account fund itself and found the guild, because /api/staff/gold
    # can only pay the caller and you cannot test seizing a guild without one
    # existing. botowner is not in `names`, so it never reaches this check -
    # which means any row this finds is a bot that got into a guild, and there
    # is no legitimate way for that to have happened.
    founders = srv.all(
        "SELECT users.username FROM guild_members"
        "  JOIN users ON users.id = guild_members.user_id"
        " WHERE users.username IN (%s) AND guild_members.rank = 'leader'"
        % placeholders, names)
    intruders = srv.all(
        "SELECT users.username FROM guild_members"
        "  JOIN users ON users.id = guild_members.user_id"
        " WHERE users.username IN (%s) AND guild_members.rank != 'leader'"
        % placeholders, names)
    checks.append({
        "name": "no bot walked into a guild it was not invited to",
        "ok": len(intruders) == 0,
        "detail": "bot founders (expected: the one the takeover test funded): %s; "
                  "bots holding a non-leader rank: %s"
                  % (", ".join(founders) or "none",
                     ", ".join(intruders) or "none"),
    })

    # The invariants stay PURE: money, life and progress cannot be forged. A
    # server 5xx is a real finding too, but a different KIND - the server
    # crashed, it did not let an attack through - so it is reported on its own
    # tier in report(), not folded in here where it would dilute this signal.
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

    # cross-account / privilege need a second identity: players[1] is the
    # victim to point staff powers at; with only one player the attacker still
    # has the owner account to aim at.
    attacker = players[0]
    victim = players[1] if len(players) > 1 else players[0]
    attack_results.append(attack_staff_privilege(srv, attacker, victim))
    attack_results.append(attack_owner_economy(attacker))

    # THE NEWER SYSTEMS. Ordered deliberately: the takeover founds the guild
    # that the chat-leak attack then reads from, so they run in that order and
    # the leak test has a real guild to be outside of.
    owner = _owner_player(srv)
    attack_results.append(attack_staff_gold_mint(attacker))
    attack_results.append(attack_guild_found_free(attacker))
    attack_results.append(attack_guild_takeover(srv, attacker, owner))
    attack_results.append(attack_guild_chat_leak(srv, attacker, owner))
    attack_results.append(attack_chat_delete_others(srv, attacker, victim))
    attack_results.append(attack_loot_double_take(attacker))
    attack_results.append(attack_revive_underpay(attacker))

    # measured sweeps run BEFORE the invariants, so anything the fuzzer or the
    # race storm knocked loose is caught by the ground-truth re-derivation.
    rate_results = [
        measure_kill_rate(players[0]),
        fuzz_sweep(attacker),
        race_storm(srv, players[0]),
    ]

    invariants = check_invariants(srv, players)
    return {
        "players": player_count,
        "attacks": attack_results,
        "rates": rate_results,
        "invariants": invariants,
        "crashes": list(srv.crashes),
    }


def report(result):
    lines = []
    lines.append("=" * 68)
    lines.append("  ELUSION SECURITY BOT - %d fake players" % result["players"])
    lines.append("=" * 68)

    lines.append("\nATTACKS (each tried over HTTP, as a modified client would)\n")
    any_broke = False
    skipped = 0
    for a in result["attacks"]:
        if a.get("skipped"):
            verdict = "skipped"
            skipped += 1
        else:
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

    crashes = result.get("crashes", [])

    lines.append("\n" + "=" * 68)
    if any_invariant_broke:
        verdict = "FAIL - an invariant broke. The running server let an attack through."
    elif any_broke:
        verdict = ("ATTENTION - an attack got through but no invariant moved. "
                   "Read the detail; the guard may be narrower than the invariant.")
    elif crashes:
        verdict = ("ATTENTION - nothing was forged, but the server 5xx'd %d time(s) on "
                   "malformed input. A crash leaks a stack trace and is a DoS lever; "
                   "those handlers want a clean 400." % len(crashes))
    elif skipped:
        verdict = ("PASS with %d attack(s) SKIPPED - nothing got through, but the "
                   "skipped ones never reached their target and prove nothing. "
                   "Read their detail: a precondition the bot could not set up "
                   "usually means the setup path itself changed." % skipped)
    else:
        verdict = "PASS - every attack blocked, every invariant holds, nothing crashed."
    lines.append("  " + verdict)
    lines.append("=" * 68)
    return "\n".join(lines), (any_invariant_broke or any_broke or bool(crashes))


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
