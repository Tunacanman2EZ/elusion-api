from flask import Flask, request, g
from flasgger import Swagger
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

import gamedata
import json
import logging
import sqlite3
import secrets
import time
import os
import re
import sys
import threading
from functools import wraps

app = Flask(__name__)

# HARD BODY-SIZE CAP. A save can carry ~64 KB per explored area (see
# MAX_EXPLORED_BYTES), so 4 MiB sits far above any legitimate request while
# still refusing the multi-megabyte bodies whose only purpose is to exhaust
# memory before the JSON is even parsed. Werkzeug returns a clean 413 on its
# own once this is set - no handler ever sees an oversized body. Raise it if a
# real save ever legitimately approaches the cap.
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024

app.config["SWAGGER"] = {
    "title": "Elusion RPG API",
    "uiversion": 3,
    "description": "Backend for the Elusion RPG Godot client.",
    "version": "0.1.0",
}
swagger = Swagger(app)


# =============================================================================
# CONFIG
# =============================================================================

DB_PATH = os.environ.get("ELUSION_DB", os.path.join(os.path.dirname(__file__), "elusion.db"))

# how long a login token stays valid. games shouldn't log people out
# constantly, so this is generous - 30 days in seconds.
TOKEN_TTL = 60 * 60 * 24 * 30

# LOGIN THROTTLE. A real account that fails this many times in a row is frozen
# for the cooldown below, so a stolen-password guessing run cannot grind at HTTP
# speed. Only a real row can be locked, so this does reveal that a locked
# username exists - the standard, accepted trade for per-account lockout. See
# SECURITY_NOTES.md (E-5). Counting resets on the first correct password.
LOGIN_MAX_ATTEMPTS = 8
LOGIN_LOCKOUT_SECONDS = 15 * 60

# THE SAME ANSWER IS NOT ENOUGH IF IT ARRIVES AT A DIFFERENT TIME.
#
# /login returns an identical 401 whether the username does not exist or the
# password is wrong, which is the entire point of that branch. But the two paths
# did not COST the same:
#
#     no such user     -> returns immediately              ~1 ms
#     wrong password   -> check_password_hash runs scrypt  ~100 ms
#
# `if row is None or not check_password_hash(...)` short-circuits, so a missing
# user never reached the hash. Two identical responses a hundredfold apart on
# the clock enumerate usernames exactly as well as two different messages do —
# the attacker reads the stopwatch instead of the body.
#
# A missing user is now verified against this hash instead and the result thrown
# away. It is a real scrypt verify against a real hash, so it costs what the
# genuine path costs.
#
# COMPUTED ONCE AT IMPORT, never per request. generate_password_hash is the same
# 32MB of work as verifying one, so minting a fresh dummy per miss would let an
# attacker make the server do double work per guess — turning a fix for one
# side channel into a denial-of-service lever, which is the trap the per-IP
# throttle below already exists to avoid.
#
# The password is random and is never stored, so nothing can authenticate as
# this: there is no row for it to match against.
_TIMING_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32))

# PER-IP THROTTLE, and the reason the per-account one above is not enough.
#
# LOGIN_MAX_ATTEMPTS stops VERTICAL brute force - many passwords against one
# account. It does nothing about HORIZONTAL: one host trying "password123"
# against a thousand different usernames never reaches 8 consecutive misses on
# any single one, so it never trips anything. That is the cheaper attack, it is
# the one that actually gets run against small games, and it was invisible here.
#
# So this counts failures by SOURCE rather than by account, over a rolling
# window. Distinct usernames are counted separately and more strictly, because
# twelve failures spread across twelve names is a spray, while twelve against
# one name is a person who forgot their password.
IP_MAX_FAILURES = 25              # total failures from one address per window
IP_MAX_USERNAMES = 6              # distinct usernames failed against per window
IP_WINDOW_SECONDS = 10 * 60
IP_LOCKOUT_SECONDS = 15 * 60

# WHICH ROWS ARE EVIDENCE OF AN ATTACK. An ALLOWLIST, and the shape is the
# whole point.
#
# login_attempts records everything that happens at this door, and most of it
# is not evidence. The gate's OWN refusals go in the same table, with a fresh
# username on every one - and the first version of _ip_throttle_state() counted
# any row that was not 'register-%', which meant those refusals counted toward
# the condition that produced them. The gate fed itself:
#
#   six typos anywhere behind one address trip the spray rule
#   -> every later attempt is refused and LOGGED as a new failed username
#   -> the window never falls below six distinct names
#   -> the lockout renews forever, for as long as anyone keeps trying
#
# Measured, not theorised: sixty players with the CORRECT password, retrying
# continuously behind one address, were refused for four full windows and only
# got in once every client went silent simultaneously. Game clients retry on
# their own, so "everyone stops at the same moment" does not happen.
#
# That is a launch-day outage waiting for a shared address, and shared
# addresses are the normal case - a household, a student hall, a mobile
# carrier's NAT, and EVERY player at once if this sits behind a proxy with
# ELUSION_TRUSTED_PROXIES left at 0.
#
# Only a row where a credential was actually checked and actually failed
# counts. Deliberately excluded:
#   ip-spray / ip-volume    this gate's own refusals - see above
#   register-*              a clumsy signup must not cost anyone their login
#   account-locked          no credential was checked; counting it lets one
#                           account's lockout escalate to the whole address
#   banned                  the password was CORRECT. A banned player retrying
#                           would otherwise lock out everyone in their house.
#
# An ALLOWLIST rather than a longer NOT-LIKE, because the denylist version had
# already needed extending once and its failure mode is silent: a refusal
# reason added later becomes evidence by default, and nothing says so.
THROTTLE_EVIDENCE_REASONS = ("no-such-user", "bad-password", "bad-password-on-change")

# REGISTRATION PROBING, which is the same question asked at a different door.
#
# /login goes to real lengths never to reveal whether a username exists.
# /register answers it outright, with 409 against 201, and has to — a signup
# form that will not say a name is taken is not a signup form. That makes it
# the softer target of the two, and it had no limit at all: forty guesses from
# one address got forty straight answers, where /login stopped after six.
#
# TEN, NOT SIX, and counted separately from the login gate. Someone choosing a
# name genuinely does collide a few times; six is well inside honest behaviour
# and being locked out of LOGGING IN for picking a popular username would be a
# worse bug than the one this closes. Ten is clear of real signup behaviour and
# still turns an unlimited oracle into six answers an hour.
REGISTER_MAX_CONFLICTS = 10       # 409s from one address per window

# How long a login attempt row is kept. Long enough to see a slow spray in the
# log, short enough that the table does not grow forever on a box nobody prunes.
LOGIN_LOG_RETENTION_SECONDS = 60 * 60 * 24 * 14

# WHEN AN ADDRESS STOPS MEANING "A PERSON".
#
# The staff link view answers "who else plays from here", and its whole value
# depends on the answer being short. A household is two or three accounts. A
# university hall, a carrier's NAT pool or a shared VPN exit is dozens, and on
# one of those an address links strangers who have never met.
#
# Above this count the link is reported as WEAK rather than hidden. Hiding it
# would be its own bug - the evader behind a carrier address is still there -
# but presenting it at the same weight as a two-account household is how a mod
# at 3am bans somebody's flatmate. Six is chosen to sit above a family and
# below anything institutional; it is a judgement, not a measurement, which is
# why the raw counts are returned alongside the verdict.
SHARED_ADDRESS_ACCOUNTS = 6

# A cap on the linked list itself. A crowded address can link hundreds, and a
# response that large is not a view - it is a denial of service against the
# person reading it, and against the box that has to build it.
LINKED_ACCOUNT_LIMIT = 40

# WHERE THE REGISTRATION BLOCK STOPS APPLYING, AND WHY THAT IS NOT A LOOPHOLE.
#
# The block refuses new accounts from an address holding a live ban. On a home
# connection that is the intended trade: one household inconvenienced, evasion
# made annoying. On a school, a library or a carrier NAT it is a WEAPON - and
# one that costs an attacker nothing to aim.
#
# Demonstrated against this codebase rather than imagined: forty unrelated
# accounts on one campus address, one of them banned on purpose, and the
# forty-first student could never sign up again. A PERMANENT ban makes that
# permanent. The attacker's own account is already gone, so they lose nothing
# by spending it, which is what makes this cheap enough to expect.
#
# So above this count the block does not apply. That is a real cost - an evader
# who finds a crowded address registers freely - accepted for two reasons: a
# VPN already hands them that outcome for a few dollars, so the block was never
# the thing stopping a determined person; and the link view still surfaces
# every one of those accounts to staff, which is the mechanism that was always
# doing the work against someone who actually tries.
#
# Set well above a household (a family of four with two accounts each is eight)
# and well below anything institutional. Distinct from
# SHARED_ADDRESS_ACCOUNTS: marking a link WEAK in a view a human reads is a
# cheap mistake, while refusing a stranger an account is not, so the bar for
# refusing is higher than the bar for doubting.
EVASION_BLOCK_MAX_ACCOUNTS = 12

# HOW MANY REVERSE PROXIES SIT IN FRONT OF THIS APP. Read the long note on
# client_ip() before changing it - getting this wrong breaks the throttle in one
# of two opposite and equally bad ways.
TRUSTED_PROXY_HOPS = int(os.environ.get("ELUSION_TRUSTED_PROXIES", "0") or 0)

if TRUSTED_PROXY_HOPS > 0:
    # Only when explicitly configured. ProxyFix makes Flask read the client
    # address out of X-Forwarded-For, which is correct behind a proxy you
    # control and a forgery hole anywhere else.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXY_HOPS, x_proto=TRUSTED_PROXY_HOPS)

# THE SKILLS THE SERVER GRANTS, AND THEREFORE OWNS.
#
# PUT /api/character/skills replaces the whole skill set with whatever arrives,
# which is fine for a skill the client is still the only source of, and fatal
# for one the server writes: the client's very next routine sync would overwrite
# a grant made seconds earlier, and the XP would survive exactly until the player
# picked up a potion. So anything listed here is dropped from that request.
#
# ATTACK IS HERE BECAUSE THE SERVER ALREADY DECIDED IT. /api/combat/kill has
# always rolled attack_xp itself - the client cannot name its own reward - but
# it then handed the number back and trusted the client to apply it and sync it
# home. The decision was server-side and the bookkeeping was not, which is the
# gap E-2 describes. Granting it where it is rolled closes that for attack.
#
# DEFENSE, AGILITY AND MAGIC ARE DELIBERATELY ABSENT. They are not merely
# un-migrated; the server cannot see the events that earn them. Defense trains
# on damage taken and agility on distance moved (player.gd:
# agility_xp_per_1000_px), neither of which involves killing anything, so there
# is no server-side moment to hang a grant on. Any bound derived from character
# level would be wrong for exactly the same reason: a level 1 character who
# walks far enough legitimately earns agility, and clamping that would punish
# honest play to catch a cheat. They need their own server-observed events
# first. See SECURITY_NOTES.md (E-2).
SERVER_OWNED_SKILLS = {"fishing", "cooking", "attack"}

# SKILL CEILING. The three skills above are granted server-side; the rest are
# still the client's to compute and PUT back, so the server cannot prove those
# were earned.
# Until it can (see SECURITY_NOTES.md E-2), this cap is the guardrail: it stops
# a modified client asserting an absurd level, which is both a power cheat and a
# source of integer/DB nonsense. 99 matches the skill set's RuneScape lineage.
# A legitimate client never reaches it, so capping costs honest players nothing.
MAX_SKILL_LEVEL = 99
MAX_SKILL_XP = 200_000_000

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,20}$")
MIN_PASSWORD_LENGTH = 8

REQUIRED_FIELDS = ["username", "password"]

# The account that owns this server, named in the environment rather than
# stored in the database.
#
# That is the whole point. If "owner" were the top value of a role column, then
# whatever endpoint sets roles could set it, and the first bug in that endpoint
# would be a total compromise. It would also survive a stolen database backup.
# An environment variable cannot be written by any request, does not appear in
# elusion.db, and makes "everyone except me" a comparison that no code path can
# make false.
#
# Set it before starting the server:
#     PowerShell   $env:ELUSION_OWNER = "yourname"
#     cmd          set ELUSION_OWNER=yourname
#     bash         export ELUSION_OWNER=yourname
#
# ...or put it in a .env file next to this one, which is what _load_dotenv()
# below is for. The variable has to be set in the EXACT shell that launches the
# server, every time, and forgetting is silent: the server starts fine, nobody
# is the owner, and the only symptom is that your debug keys stop working. That
# happened, which is why the boot line below now says who the owner is.
#
# Unset means no owner, and every owner check fails closed - a server with no
# configured owner has no owner, rather than everyone being one.


def _load_dotenv():
    """
    Read KEY=value lines from a .env beside this file into the environment.

    A REAL ENVIRONMENT VARIABLE ALWAYS WINS. This only fills in what the shell
    did not set, so an explicit `$env:ELUSION_OWNER = "..."` still overrides the
    file and there is no way for a stale .env to quietly take precedence over
    what someone just typed.

    No dependency: python-dotenv would do this better, but it is one more thing
    to install for eight lines, and requirements.txt being two entries long is
    worth more than the polish.

    .env is gitignored, which is the point - ELUSION_OWNER must not live in the
    repository any more than it lives in the database.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError as exc:
        print(f"[BOOT] could not read .env ({exc}); using the shell environment only")


_load_dotenv()

OWNER_USERNAME = os.environ.get("ELUSION_OWNER", "").strip()

# SAY SO AT BOOT, both ways.
#
# There was no signal here at all. A server with no owner started exactly like
# one with an owner, and the first sign was a staff-only feature silently doing
# nothing - which reads as a broken feature rather than a missing variable.
if OWNER_USERNAME:
    print(f"[BOOT] owner: {OWNER_USERNAME}")
else:
    print("[BOOT] NO OWNER SET. Every owner and staff check will deny.")
    print("[BOOT]   PowerShell: $env:ELUSION_OWNER = \"yourname\"")
    print("[BOOT]   or put ELUSION_OWNER=yourname in a .env beside app.py")


# =============================================================================
# E-4 - THE WERKZEUG DEBUGGER, AND EVERY DOOR IT CAN COME IN BY
# =============================================================================
# The interactive debugger turns an unhandled exception into a Python console
# in the browser: arbitrary code execution on the box that holds elusion.db and
# its password hashes. The run block at the bottom of this file has kept it off
# unless ELUSION_DEBUG=1 since the audit - but that block is ONE way to start
# this server, and it was the only one guarded. Probed on Flask 3.1.3 by
# fetching the debugger's own stylesheet, which only exists while it is
# attached:
#
#     python app.py                                   404   off
#     python app.py, ELUSION_DEBUG=1                  200   on, as asked
#     flask --app app run --debug                     200   on, never asked
#     FLASK_DEBUG=1 flask --app app run               200   on, never asked
#     flask --app app run --debugger                  200   on, never asked
#     ELUSION_DEBUG=1 with ELUSION_TRUSTED_PROXIES=1  200   on, behind a proxy
#
# `flask run` never executes the run block, so the flag it guards was never
# consulted. FLASK_DEBUG=1 is also a very ordinary thing to have in a shell
# from some other Flask project.
#
# ONE SWITCH NOW, TWO LOCKS ON IT:
#
#   at import   refuse to start if FLASK_DEBUG or `flask run --debugger` asks
#               for the debugger without ELUSION_DEBUG, or if any of them is
#               set while a proxy is configured - a proxy in front means other
#               people reach this server, and that is exactly where the
#               debugger must not be.
#   per request refuse to serve at all if the interactive debugger is wrapped
#               around this app anyway, by any door not listed above. It looks
#               for the debugger itself rather than for the ways of asking for
#               it, so a future door is covered too.
#
# --debugger IS IN THE FIRST LIST, NOT ONLY THE SECOND, because the second is
# not enough for it: the per-request guard stops every route, but the
# debugger's own /console page is served outside this app. It is PIN-locked -
# checked, it says "console is locked" - and a lock is a thing you rely on only
# when there is no way to not have the door at all.

_TRUTHY = ("1", "true", "yes", "on")


def _flag(value):
    return str(value or "").strip().lower() in _TRUTHY


def debugger_refusal(env, argv=()):
    """
    Why the Werkzeug debugger must not run with this environment, or None.

    A pure function of the environment and command line so test_security.py can
    hand it a table of them rather than restarting a server for each one.
    """
    wants_ours = _flag(env.get("ELUSION_DEBUG"))
    wants_flask = _flag(env.get("FLASK_DEBUG")) or "--debugger" in argv
    try:
        proxies = int(str(env.get("ELUSION_TRUSTED_PROXIES", "0") or 0).strip() or 0)
    except ValueError:
        proxies = 0   # reported by wsgi.py's preflight; not this function's job
    if wants_flask and not wants_ours:
        return ("FLASK_DEBUG or --debugger is set, which makes `flask run` attach "
                "the Werkzeug debugger - arbitrary code execution for anyone who can "
                "reach this server. This project's switch is ELUSION_DEBUG=1, for a "
                "local dev loop only. Drop the flag, or set ELUSION_DEBUG=1 as well "
                "if you really mean it.")
    if (wants_ours or wants_flask) and proxies > 0:
        return ("a debug flag is set while ELUSION_TRUSTED_PROXIES is %d. A proxy in "
                "front means other people reach this server, and the Werkzeug "
                "debugger must never be reachable by anyone else." % proxies)
    return None


def debugger_permitted(env, argv=()):
    """The interactive debugger may be attached: asked for, and nothing refuses it."""
    return _flag(env.get("ELUSION_DEBUG")) and debugger_refusal(env, argv) is None


# AFTER _load_dotenv(), so a debug flag in .env is judged exactly like one in
# the shell - a .env copied from a dev machine is the likeliest way to ship one.
_DEBUGGER_REFUSAL = debugger_refusal(os.environ, sys.argv)
if _DEBUGGER_REFUSAL is not None:
    raise SystemExit("[BOOT] refusing to start: " + _DEBUGGER_REFUSAL)
DEBUGGER_PERMITTED = debugger_permitted(os.environ, sys.argv)


@app.before_request
def _refuse_under_unasked_debugger():
    # werkzeug.debug.preserve_context is set on every request that passes
    # through the INTERACTIVE debugger (DebuggedApplication with evalex on) -
    # the dangerous mode, whichever way it was attached. The debugger can only
    # open a console on an exception raised inside this app, so refusing every
    # request before any route runs leaves it nothing to open one on - and a
    # server answering 503 to everything is a misconfiguration nobody misses.
    if "werkzeug.debug.preserve_context" in request.environ and not DEBUGGER_PERMITTED:
        return {
            "error": "Service Unavailable",
            "message": "The Werkzeug debugger is attached to this server without "
                       "ELUSION_DEBUG. Restart it without --debug / --debugger.",
        }, 503
    return None


@app.before_request
def _require_json_object():
    # Every write endpoint in this API reads its fields off a JSON OBJECT. A
    # body that parses as valid JSON but is a list, a number, a string or a
    # bool is malformed for all of them - and left to the handler it becomes
    # `.get()` on a list, which raises and returns an unhandled 500: a
    # stack-trace leak and a cheap way to make the box throw. `or {}` at the
    # call site does not catch it, because a non-empty list is truthy.
    #
    # Catching it here, before any route runs, collapses that whole class of
    # crash into one clean 400 for the entire API at once - the red-team bot's
    # fuzzer found eight endpoints that 500'd on a bare `[1,2,3]`, and this is
    # the single guard that closes all of them and any future sibling.
    #
    # It fires ONLY when a JSON body is actually present. A missing body, or a
    # body sent without a JSON content-type, still reaches the handler's own
    # validation exactly as before - get_json(silent=True) returns None for
    # both, and every handler already treats that as "no fields". No endpoint
    # in this API takes a top-level array, so nothing legitimate is refused.
    if request.method in ("POST", "PUT", "PATCH") and request.is_json:
        body = request.get_json(silent=True)
        if body is not None and not isinstance(body, dict):
            return bad_request("request body must be a JSON object")
    return None


@app.after_request
def _security_headers(resp):
    # Cheap, universal hardening headers. nosniff stops content-type games;
    # no-referrer keeps request paths out of any outbound Referer; DENY forbids
    # this API and its Swagger UI from being framed for clickjacking. No CSP on
    # purpose - it would fight the Swagger UI's inline scripts for no gain on a
    # JSON API. setdefault so a route that sets its own value still wins.
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


@app.errorhandler(Exception)
def _handle_unexpected(exc):
    # HTTP errors (400/401/404/405/413/...) are deliberate answers - let them
    # render as themselves, unchanged, so every existing refusal keeps its
    # shape. Anything else is an UNEXPECTED error: log the full traceback
    # server-side where you can see it, and hand the client a flat JSON 500
    # with nothing internal in it. Debug is already forced off (E-4), so Flask
    # would not leak a trace anyway - this is the belt to that suspenders, and
    # the one place every surprise gets recorded instead of vanishing.
    if isinstance(exc, HTTPException):
        return exc
    app.logger.exception("unhandled error on %s %s", request.method, request.path)
    return {
        "error": "Internal Server Error",
        "message": "The server hit an unexpected error.",
    }, 500


# =============================================================================
# RATE LIMITING - a coarse per-IP ceiling on abuse
# =============================================================================
# OPT-IN, because the right number depends on how chatty the real client is,
# which is a thing to MEASURE, not guess - set too low it throttles a player
# mid-fight, set by guesswork it is theatre. Turn it on by naming a limit in the
# server's environment:
#
#     ELUSION_RATE_LIMIT="600 per minute"      # also "600/60", "10 per second"
#
# Unset (the default) is OFF, so the test suites, the security bot, and any box
# that has not chosen a number are untouched. Login and registration already
# carry their own tighter SQLite throttles; this is the blanket ceiling over
# everything else - the save / inventory / skill / bank spam a modified client
# could otherwise fire without limit.
#
# IN-MEMORY, so the count is PER WORKER: under `gunicorn -w 4` the real ceiling
# is four times the number set. That is fine for a DoS ceiling - it still bounds
# one IP to a fixed rate - but it is not a precise fair-use limit. For that the
# counter has to be SHARED across workers (Redis, or the login_attempts SQLite
# pattern already in this file). Start generous, watch the logs, tighten later.

def _parse_rate_limit(raw):
    """"600 per minute" / "600/60" / "10 per second" / "600" -> (count, window_s)."""
    raw = (raw or "").strip().lower()
    if not raw or raw in ("0", "off", "none", "false"):
        return None
    units = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600}
    m = re.match(r"^(\d+)\s*(?:per|/)\s*(\d+)?\s*(second|sec|minute|min|hour)?s?$", raw)
    if m:
        count = int(m.group(1))
        window = (int(m.group(2)) if m.group(2) else 1) * units.get(m.group(3) or "second", 1)
    elif re.match(r"^\d+$", raw):
        count, window = int(raw), 60
    else:
        return None
    return (count, window) if count > 0 and window > 0 else None


_RATE = _parse_rate_limit(os.environ.get("ELUSION_RATE_LIMIT"))
_RATE_HITS = {}                      # ip -> list of recent request timestamps
_RATE_LOCK = threading.Lock()        # threaded workers share one worker's dict
_RATE_EXEMPT = ("/api/status", "/apidocs", "/flasgger_static", "/apispec")

if _RATE:
    print("[BOOT] rate limit: %d requests / %ds per IP (per worker)" % _RATE)


@app.before_request
def _rate_limit():
    # Off unless a limit was named; health check and the API docs are always
    # exempt so a load balancer's polling and the Swagger UI never trip it.
    if _RATE is None:
        return None
    if any(request.path.startswith(p) for p in _RATE_EXEMPT):
        return None
    count, window = _RATE
    ip = client_ip() or "?"
    now = time.time()
    cutoff = now - window
    with _RATE_LOCK:
        hits = [t for t in _RATE_HITS.get(ip, ()) if t >= cutoff]
        if len(hits) >= count:
            _RATE_HITS[ip] = hits
            retry = max(1, int(hits[0] + window - now) + 1)
            return ({"error": "Too Many Requests",
                     "message": "Rate limit exceeded - slow down."},
                    429, {"Retry-After": str(retry)})
        hits.append(now)
        _RATE_HITS[ip] = hits
        # keep the table from growing without bound as IPs come and go
        if len(_RATE_HITS) > 4096:
            for k in [k for k, v in list(_RATE_HITS.items()) if not v or v[-1] < cutoff]:
                _RATE_HITS.pop(k, None)
    return None


# =============================================================================
# DATABASE
# =============================================================================

def get_db():
    """One connection per request, closed automatically in teardown."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        # enforce foreign keys - off by default in sqlite
        g.db.execute("PRAGMA foreign_keys = ON")

        # WAL - READERS AND WRITERS AT THE SAME TIME. In the default rollback
        # journal a writer blocks every reader and every other writer for the
        # length of its transaction, so under `gunicorn -w 4` the whole server
        # serialises behind one write - measured flat at ~250 committed
        # writes/s regardless of how many workers or clients pile on. WAL lets
        # readers keep going while a write is in flight and turns each commit
        # into a sequential append to the -wal file instead of a full-DB
        # rewrite. Re-measured with loadtest.py: committed write throughput rose
        # ~60% under concurrency, with lower tail latency. journal_mode is a
        # PERSISTENT property of the database file, so this is idempotent - the
        # first connection sets it, the rest read "wal" back and move on.
        #
        # THE READER-PATTERN REVIEW THIS WAS WAITING ON IS DONE. The worry was
        # separate reader connections (canary.py, backup_db.py, the security
        # bot, the economy suite's farm loop) seeing WAL data wrong. Verified:
        # every request here takes a FRESH connection and closes it in teardown
        # - there is no long-lived reader in the server itself - and mode=ro
        # readers read a live WAL database correctly, uncheckpointed rows and
        # all, given ordinary directory write access (which the same-box tools
        # have). The only place that reused one connection across many writes
        # was a TEST harness, fixed there rather than by holding WAL back.
        g.db.execute("PRAGMA journal_mode = WAL")

        # synchronous=NORMAL is the SAFE partner to WAL, not a corner cut. Under
        # WAL it still fsyncs at each checkpoint and stays durable across an
        # application crash; the only thing at risk is the very last transaction
        # on a full OS or power loss - the standard production pairing, and an
        # easy trade for a game. FULL (the default) fsyncs on every single
        # commit, which is most of what the write path was paying for. NOT
        # persistent - set on every connection.
        g.db.execute("PRAGMA synchronous = NORMAL")

        # CONCURRENCY FLOOR. Without this, the moment two requests contend for a
        # write the loser fails outright with "database is locked". busy_timeout
        # makes the contender WAIT for the lock (up to 5s) and then proceed, so
        # overlap becomes a brief queue instead of an error. Per-connection.
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist. Safe to call on every boot."""
    db = sqlite3.connect(DB_PATH)

    # Before the schema block, not after - see the docstring.
    _migrate_rename_admin_actions(db)

    db.executescript(
        """
        -- `role` is the rank system. It replaced a boolean called is_admin,
        -- which _migrate_role_column() carries across and
        -- _migrate_drop_is_admin() then removes. There is no admin rank and
        -- there never was one; that column was a yes/no flag from before
        -- ranks existed.
        --
        -- 'owner' is NOT a legal value here. The CHECK says so, but the CHECK
        -- only reaches databases created from this statement - a database
        -- migrated by _migrate_role_column() gets the column via ALTER
        -- and therefore no constraint at all.
        --
        -- So the CHECK is defence in depth, not the mechanism. role_for() is
        -- the mechanism: it reads anything outside ROLES, and anything equal
        -- to 'owner', as DEFAULT_ROLE. A column holding 'owner' grants
        -- nothing on either shape of database.
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT    NOT NULL,
            role          TEXT    NOT NULL DEFAULT 'player'
                          CHECK (role IN ('player', 'mod', 'dev')),
            is_banned      INTEGER NOT NULL DEFAULT 0,
            -- NULL with is_banned = 1 means permanent. See the migration.
            ban_expires_at INTEGER,
            ban_reason     TEXT    NOT NULL DEFAULT '',
            banned_by      TEXT    NOT NULL DEFAULT '',
            banned_at      INTEGER NOT NULL DEFAULT 0,
            created_at    INTEGER NOT NULL
        );

        -- Every moderation action, forever. Names are stored ALONGSIDE ids
        -- because the log has to still make sense after an account is gone -
        -- an audit trail of orphaned integers answers nothing.
        --
        -- This exists from the first day rather than after the first argument.
        -- Once there are mods it is how THEIR decisions get reviewed, which is
        -- what makes delegating safe rather than merely convenient.
        -- "staff" is not a rank. It is the set of ranks above player -
        -- mod, dev and owner - because all three can act here and the table
        -- needs one word for them.
        CREATE TABLE IF NOT EXISTS staff_actions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id    INTEGER,
            actor_name  TEXT    NOT NULL,
            action      TEXT    NOT NULL,
            target_id   INTEGER,
            target_name TEXT    NOT NULL,
            detail      TEXT    NOT NULL DEFAULT '',
            created_at  INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_staff_actions_target
            ON staff_actions(target_name);

        CREATE TABLE IF NOT EXISTS sessions (
            token        TEXT    PRIMARY KEY,
            user_id      INTEGER NOT NULL,
            expires_at   INTEGER NOT NULL,
            -- The last time this client proved it was running. See
            -- ONLINE_WINDOW_SECONDS: a session lives thirty days, so "has a
            -- session" says nothing about whether anyone is playing.
            last_seen_at INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

        -- WHICH ADDRESSES AN ACCOUNT HAS ACTUALLY LOGGED IN FROM.
        --
        -- ONE ROW PER (account, address) PAIR, not per request, so this stays
        -- small: a player on a laptop, a phone and a friend's wifi has three
        -- rows for life. login_attempts already records addresses, but it is
        -- pruned at LOGIN_LOG_RETENTION_SECONDS (14 days) because it holds a
        -- row per ATTEMPT and would grow without bound. A ban outlives that,
        -- and so does the question this table answers.
        --
        -- WHY IT IS SEPARATE FROM THE BAN. The evasion pattern is: banned,
        -- new account, same connection, same minute. Answering "has anyone
        -- banned used this address" needs the association to survive longer
        -- than a fortnight and longer than any single account.
        --
        -- THIS IS LOCATION DATA ABOUT PLAYERS, and it is treated as such: it
        -- is read only by staff who could already act on the account (see
        -- can_act_on), and it rides the same ON DELETE CASCADE as everything
        -- else, so deleting an account takes its address history with it.
        CREATE TABLE IF NOT EXISTS account_ips (
            user_id    INTEGER NOT NULL,
            ip         TEXT    NOT NULL,
            first_seen INTEGER NOT NULL,
            last_seen  INTEGER NOT NULL,
            PRIMARY KEY (user_id, ip),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- The lookup that matters is BY ADDRESS - "who else has been here" -
        -- which the primary key's leading user_id cannot serve.
        CREATE INDEX IF NOT EXISTS idx_account_ips_ip ON account_ips(ip);

        -- EVERY LOGIN ATTEMPT, GOOD AND BAD.
        --
        -- Two jobs, and it is one table because they need the same rows.
        -- First, it is what the per-IP throttle counts: a rolling window of
        -- failures from one address, which no counter on the users table can
        -- express because the attacker never hits the same account twice.
        -- Second, it is the forensic record - "tons of failures for one
        -- username" and "tons of failures from one IP across many usernames"
        -- are both single queries against it, and neither was answerable
        -- before this existed.
        --
        -- username IS NOT A FOREIGN KEY, deliberately. The interesting rows are
        -- attempts against names that do NOT exist, which is exactly what a
        -- spray looks like, and a foreign key would make those unstoreable.
        -- It is the string that was typed, not a user reference.
        --
        -- NO PASSWORD FIELD, not even hashed, not even truncated. There is no
        -- version of storing what someone typed into a password box that is
        -- worth the day it leaks - and a mistyped password is usually a real
        -- password with one character wrong.
        CREATE TABLE IF NOT EXISTS login_attempts (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT    NOT NULL DEFAULT '',
            ip       TEXT    NOT NULL DEFAULT '',
            ok       INTEGER NOT NULL DEFAULT 0,
            reason   TEXT    NOT NULL DEFAULT '',
            at       INTEGER NOT NULL
        );

        -- The throttle's own query: failures from one ip since a cutoff.
        CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, at);
        -- Pruning, and "what happened to this account" for support.
        CREATE INDEX IF NOT EXISTS idx_login_attempts_at ON login_attempts(at);
        CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username, at);

        -- EVERY KILL A CLIENT CLAIMS.
        --
        -- E-3 says the kill EVENT is asserted rather than verified: the server
        -- rolls the rewards itself, refuses reward-less enemies and rate-limits
        -- with a token bucket, but never confirms a fight happened. The note
        -- that matters is "the rate limit caps the SPEED of the fraud, not its
        -- existence" - and until this table, it did not even cap the
        -- INVISIBILITY of it. Nothing recorded a kill. A client reporting one
        -- boss an hour, forever, looked exactly like a player who enjoys the
        -- boss.
        --
        -- THIS DOES NOT CLOSE E-3 AND IS NOT PRETENDING TO. Verification needs
        -- the server to know what spawned, which needs the client to register
        -- spawns - a change on both sides of the wire. What this does is make
        -- the thing measurable, which is the step before any check worth
        -- enforcing: you cannot detect an anomaly you never wrote down, and a
        -- threshold picked without data is how honest players get clamped.
        --
        -- REWARDS ARE STORED AS PAID, not recomputed later. The enemy's profile
        -- can be retuned tomorrow; what this account was actually given is a
        -- fact about that moment and has to survive the retune, exactly like
        -- staff_actions storing names alongside ids.
        CREATE TABLE IF NOT EXISTS kill_reports (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            slot       INTEGER NOT NULL,
            enemy_id   TEXT    NOT NULL,
            xp         INTEGER NOT NULL DEFAULT 0,
            attack_xp  INTEGER NOT NULL DEFAULT 0,
            level_at   INTEGER NOT NULL DEFAULT 0,
            at         INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- "what has this account been killing, and how fast" - one query.
        CREATE INDEX IF NOT EXISTS idx_kill_reports_user ON kill_reports(user_id, at);
        -- "who is reporting this enemy most" - the other one.
        CREATE INDEX IF NOT EXISTS idx_kill_reports_enemy ON kill_reports(enemy_id, at);

        -- EVERY CONSUMABLE THE SERVER AUTHORISED, one row each.
        --
        -- This is not an audit log for its own sake. PUT /api/player/status
        -- accepts any hp the client sends up to the server's derived maximum,
        -- which means healing is currently unexplained by construction: there
        -- has never been a server-known source of it to compare a rise
        -- against. /api/character/consume is that source, and these rows are
        -- what it leaves behind.
        --
        -- THE SAME SHAPE AS kill_reports AND login_attempts, for the same
        -- reason: make the data exist before picking a threshold. A rule
        -- written without it is how honest players get clamped - the interim
        -- skill-level bound under E-2 was exactly that mistake and did not
        -- survive contact with the client.
        --
        -- NO AMOUNT COLUMN YET, and its absence is the honest state of things.
        -- The server does not know what a potion restores; `restore_target`
        -- and `restore_amount` are client-side fields that exportgamedata.gd
        -- does not write to gamedata.json. So today a row answers "was a
        -- potion drunk at all", which already separates a heal from nothing.
        -- When those fields are exported this table gains target and amount,
        -- and the question sharpens from "was there one" to "was it this big".
        -- EVERY LUSION THAT ENTERS OR LEAVES THE WORLD, one row each.
        --
        -- gold_ledger's twin, and it exists for two reasons that arrived
        -- together. E-10 pointed out that lusions had no audit trail at all -
        -- they were created by a duplicate-pet conversion and destroyed by a
        -- revive, and nothing anywhere recorded either. And the kingdom board
        -- now counts a lusion revive as a contribution, which it cannot do
        -- from a balance; a balance says what you have, not what you gave.
        --
        -- WHY NOT JUST PUT THEM IN gold_ledger. Because that table carries an
        -- invariant - SUM(delta) == SUM(saves.gold) + SUM(accounts.bank_gold)
        -- - and a lusion row would break it on the first insert. Two
        -- currencies, two ledgers, two invariants that each mean something.
        CREATE TABLE IF NOT EXISTS lusion_ledger (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            at      INTEGER NOT NULL,
            user_id INTEGER,
            slot    INTEGER,
            delta   INTEGER NOT NULL,
            reason  TEXT    NOT NULL,
            detail  TEXT    NOT NULL DEFAULT '',
            -- SET NULL rather than CASCADE, same as gold_ledger: deleting a
            -- user must not rewrite the history of what existed.
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_lusion_ledger_user
            ON lusion_ledger(user_id, at);
        CREATE INDEX IF NOT EXISTS idx_lusion_ledger_reason
            ON lusion_ledger(reason, at);

        CREATE TABLE IF NOT EXISTS consume_grants (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            slot    INTEGER NOT NULL,
            item_id TEXT    NOT NULL,
            -- WHICH POOL AND HOW MUCH, which the first version of this table
            -- could not record because the server did not know. `target` is
            -- ItemData.RestoreTarget by NAME ('HP', 'MANA', 'STAMINA', or ''
            -- for a revive, which fills all three).
            target  TEXT    NOT NULL DEFAULT '',
            amount  INTEGER NOT NULL DEFAULT 0,
            at      INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- "did this character drink anything in the last N seconds" - the
        -- query the status reconciler runs on every write.
        CREATE INDEX IF NOT EXISTS idx_consume_grants_char
            ON consume_grants(user_id, slot, at);

        -- one row per character slot. the primary key is (user_id, slot)
        -- rather than an autoincrement id, so writing a slot twice is an
        -- upsert instead of a duplicate row. slots are 0..3.
        CREATE TABLE IF NOT EXISTS saves (
            user_id     INTEGER NOT NULL,
            slot        INTEGER NOT NULL,
            class_id    TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            level       INTEGER NOT NULL DEFAULT 1,
            area        TEXT    NOT NULL DEFAULT 'elusion',
            hp          INTEGER NOT NULL DEFAULT 10,
            max_hp      INTEGER NOT NULL DEFAULT 10,
            mana        INTEGER NOT NULL DEFAULT 10,
            max_mana    INTEGER NOT NULL DEFAULT 10,
            stamina     INTEGER NOT NULL DEFAULT 10,
            max_stamina INTEGER NOT NULL DEFAULT 10,
            gold        INTEGER NOT NULL DEFAULT 0,
            xp          INTEGER NOT NULL DEFAULT 0,
            xp_to_next  INTEGER NOT NULL DEFAULT 100,
            bank_gold   INTEGER NOT NULL DEFAULT 0,
            -- which pet this character currently has out, as the client's
            -- item_id (e.g. 'petpoisonslimesmall'). Empty string means none.
            -- Deliberately NOT validated against a list of known pets: the
            -- server has no item registry and inventing one here would mean
            -- every new pet the game adds needs a matching server deploy
            -- before it could be equipped. Same reasoning as bank item_ids.
            active_pet_id TEXT NOT NULL DEFAULT '',
            -- WHAT THIS CHARACTER IS WEARING, and what is on its hotbar.
            --
            -- Both are JSON objects of item ids, both default to '{}' / '[]',
            -- and both are here for the same reason active_pet_id is: a string
            -- the client cannot keep for itself. hotbar_assignments used to
            -- live only in the local slot dictionary, which meant it survived
            -- a scene change and not a re-login - the pet came back and the
            -- hotbar did not, and nothing said why.
            --
            -- JSON IN A TEXT COLUMN, not eight columns and nine more. The
            -- server does not query inside these; it stores them, hands them
            -- back, and refuses ids the catalogue has never heard of. A column
            -- per equip slot would be a schema change every time a slot is
            -- added, to buy a query nobody writes.
            equipment     TEXT NOT NULL DEFAULT '{}',
            hotbar        TEXT NOT NULL DEFAULT '[]',

            -- WHERE THIS CHARACTER HAS BEEN, as WorldMap's compressed
            -- exploration bitmask per area. Opaque to this server by design:
            -- knowing which tiles somebody has walked past confers nothing on
            -- anyone else, so there is nothing here to cheat and nothing worth
            -- the cost of understanding the format.
            --
            -- It is SIZE-CAPPED all the same. An opaque blob in a column with
            -- no ceiling is somewhere to put ten megabytes, and that is a
            -- different problem from cheating.
            explored      TEXT NOT NULL DEFAULT '{}',
            updated_at  INTEGER NOT NULL,
            PRIMARY KEY (user_id, slot),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- the anti-duplication design is the composite primary key. one row
        -- per item per slot means a deposit can only ever UPDATE a quantity,
        -- never insert a second row for the same item. the database refuses
        -- to represent the duplicated state, so no application bug can
        -- create it.
        -- Account-shared game state. Separate from `users`, which is auth:
        -- a password hash and a rank have nothing to do with a lusion
        -- balance, and keeping them apart means the auth table stays the thing
        -- you can reason about when something goes wrong with logging in.
        CREATE TABLE IF NOT EXISTS accounts (
            user_id   INTEGER PRIMARY KEY,
            lusions   INTEGER NOT NULL DEFAULT 0 CHECK (lusions >= 0),
            bank_gold INTEGER NOT NULL DEFAULT 0 CHECK (bank_gold >= 0),
            -- What dying has cost this account, cumulatively. Never decreases;
            -- see the migration for why it is account-shared rather than
            -- per-character.
            score     INTEGER NOT NULL DEFAULT 0 CHECK (score >= 0),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- THE BANK IS ACCOUNT-SHARED, NOT PER-CHARACTER. characterdata.gd puts
        -- bank_gold and bank_inventory in account_data, commented "account-
        -- shared, safe from death" - that is the whole point of the feature.
        -- This table used to carry a `slot` column, which would have made
        -- anything your warrior banked invisible to your mage.
        --
        -- Positional for the same reason carry_items is: the client holds it as
        -- an array of cells with nulls for the empty ones, and the player
        -- expects things to stay where they put them.
        CREATE TABLE IF NOT EXISTS bank_items (
            user_id  INTEGER NOT NULL,
            position INTEGER NOT NULL,
            item_id  TEXT    NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (user_id, position),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- The backpack. Keyed on POSITION, not item_id, unlike bank_items
        -- above: the bank is a set of stacks where order does not matter, but
        -- the backpack is a grid where the player expects a potion to stay in
        -- the cell they dragged it to, and two stacks of the same item in two
        -- different cells is an ordinary legal state. Keying on item_id would
        -- silently merge them and reshuffle the bag on every login.
        CREATE TABLE IF NOT EXISTS carry_items (
            user_id  INTEGER NOT NULL,
            slot     INTEGER NOT NULL,
            position INTEGER NOT NULL,
            item_id  TEXT    NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (user_id, slot, position),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- A loot bag the server rolled and still owns. The client renders a
        -- copy; taking anything out of it is a request, checked against these
        -- rows. That is what takes `gold` off the list of things a client can
        -- simply declare.
        CREATE TABLE IF NOT EXISTS loot_bags (
            bag_id     TEXT    PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            slot       INTEGER NOT NULL,
            enemy_id   TEXT    NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_loot_bags_created ON loot_bags(created_at);

        -- Position-keyed like every other container here. "Take slot 2" is a
        -- request the server can answer exactly once; "take a healthpotion" is
        -- ambiguous when the bag holds two stacks, and a client sending it twice
        -- would be asking for a duplication bug.
        CREATE TABLE IF NOT EXISTS loot_bag_items (
            bag_id   TEXT    NOT NULL,
            position INTEGER NOT NULL,
            item_id  TEXT    NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (bag_id, position),
            FOREIGN KEY (bag_id) REFERENCES loot_bags(bag_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS skills (
            user_id  INTEGER NOT NULL,
            slot     INTEGER NOT NULL,
            skill_id TEXT    NOT NULL,
            level    INTEGER NOT NULL DEFAULT 1 CHECK (level > 0),
            xp       INTEGER NOT NULL DEFAULT 0 CHECK (xp >= 0),
            PRIMARY KEY (user_id, slot, skill_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- EVERY GOLD THAT ENTERS OR LEAVES THE ECONOMY, one row each.
        --
        -- WHAT THIS BUYS. Two numbers that must agree:
        --
        --     SUM(gold_ledger.delta)  ==  SUM(saves.gold) + SUM(accounts.bank_gold)
        --
        -- The left side is what the server believes it created and destroyed.
        -- The right side is what players actually hold. Any gap is gold that
        -- entered or left without passing through gold_delta() - a bug, a
        -- dupe, or a path someone added and forgot to record. Checking it is
        -- one query, so it can run on a timer forever.
        --
        -- This is the thing most games bolt on AFTER the dupe, and then find
        -- they cannot compute because the client has been asserting balances
        -- for two years. Here gold has exactly one mint site and the column is
        -- never written from a request body, so the equation holds from day one.
        --
        -- delta is SIGNED: positive mints, negative burns. One column rather
        -- than separate minted/burned totals means the invariant is a SUM()
        -- instead of a subtraction someone can get backwards.
        --
        -- TRANSFERS ARE NOT RECORDED. Moving gold between a character's purse
        -- and the shared bank changes neither side of the equation, because
        -- both are counted in supply. Only creation and destruction belong
        -- here; a transfer that wrote a row would have to write two.
        --
        -- ON DELETE SET NULL, not CASCADE. If a user is ever removed their
        -- ledger rows must SURVIVE - deleting the history of gold that existed
        -- would rewrite the past and break the invariant for everyone. Note
        -- that deleting a user today would cascade their saves away and
        -- destroy the gold without a burn row; whoever writes that endpoint
        -- must burn the balance first.
        CREATE TABLE IF NOT EXISTS gold_ledger (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            at      INTEGER NOT NULL,
            user_id INTEGER,
            slot    INTEGER,
            delta   INTEGER NOT NULL,
            reason  TEXT    NOT NULL,
            detail  TEXT    NOT NULL DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_gold_ledger_user
            ON gold_ledger(user_id, at);
        CREATE INDEX IF NOT EXISTS idx_gold_ledger_reason
            ON gold_ledger(reason, at);

        -- A TRADE IN PROGRESS. Two characters, what each is putting up, and
        -- whether each has said yes to what the other is showing.
        --
        -- NOTHING IS HELD IN ESCROW, and that is deliberate. The obvious design
        -- takes both sides' items out of their bags when they are offered and
        -- holds them here - but then a disconnect, a crash or an abandoned
        -- window leaves real items owned by a row instead of a player, and
        -- every one of those needs a sweeper to give them back. This table
        -- records INTENT; the items never leave the bag until the single
        -- transaction that crosses them, and that transaction re-checks that
        -- both sides still hold what they promised. The worst case is a trade
        -- that fails at the last moment, which is a refusal, not a lost sword.
        --
        -- state is the concurrency guard. Execution flips 'open' to 'done' with
        -- a conditional UPDATE and checks it changed a row, so two confirms
        -- arriving together can only fire once - see _execute_trade().
        CREATE TABLE IF NOT EXISTS trades (
            trade_id    TEXT PRIMARY KEY,
            a_user      INTEGER NOT NULL,
            a_slot      INTEGER NOT NULL,
            a_gold      INTEGER NOT NULL DEFAULT 0 CHECK (a_gold >= 0),
            a_confirmed INTEGER NOT NULL DEFAULT 0,
            b_user      INTEGER NOT NULL,
            b_slot      INTEGER NOT NULL,
            b_gold      INTEGER NOT NULL DEFAULT 0 CHECK (b_gold >= 0),
            b_confirmed INTEGER NOT NULL DEFAULT 0,
            state       TEXT    NOT NULL DEFAULT 'open',
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL,
            FOREIGN KEY (a_user) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (b_user) REFERENCES users(id) ON DELETE CASCADE
        );

        -- Finding a player's open trade is the single most common read here
        -- (the panel polls it), and it is asked from both directions.
        CREATE INDEX IF NOT EXISTS idx_trades_a ON trades(a_user, state);
        CREATE INDEX IF NOT EXISTS idx_trades_b ON trades(b_user, state);

        -- ONE ROW PER ITEM TYPE PER SIDE, not per backpack cell. The primary
        -- key does the merging: offering five arrows twice is one row of ten,
        -- not two rows that have to be summed before anything can be checked.
        -- Positions are the client's business; what crosses is a quantity.
        CREATE TABLE IF NOT EXISTS trade_items (
            trade_id TEXT    NOT NULL,
            side     TEXT    NOT NULL CHECK (side IN ('a', 'b')),
            item_id  TEXT    NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (trade_id, side, item_id),
            FOREIGN KEY (trade_id) REFERENCES trades(trade_id) ON DELETE CASCADE
        );

        -- DB-LEVEL FLOOR ON CARRIED GOLD. accounts.bank_gold already carries
        -- CHECK (bank_gold >= 0); saves.gold did not - an asymmetry that let a
        -- bug, or any path that bypasses gold_delta, write a negative purse, and
        -- a negative subtracted is a positive minted. gold_delta now refuses
        -- that at the application layer; this is the backstop one level down, so
        -- SQLite itself aborts any write that would take a character's purse
        -- below zero, through gold_delta or not. A TRIGGER, not a CHECK, because
        -- a CHECK needs the table rebuilt, while this installs on an existing
        -- database with no migration and no risk to the rows already there.
        CREATE TRIGGER IF NOT EXISTS trg_saves_gold_nonneg
        BEFORE UPDATE OF gold ON saves
        WHEN NEW.gold < 0
        BEGIN
            SELECT RAISE(ABORT, 'gold cannot go below zero');
        END;
        """
    )

    _migrate_bank_to_account(db)

    _migrate_add_column(db, "saves", "active_pet_id", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(db, "saves", "equipment", "TEXT NOT NULL DEFAULT '{}'")
    _migrate_add_column(db, "saves", "hotbar", "TEXT NOT NULL DEFAULT '[]'")
    _migrate_add_column(db, "saves", "explored", "TEXT NOT NULL DEFAULT '{}'")

    # Millisecond timestamp of the last kill credited to this character, for the
    # rate limit in /api/combat/kill. 0 means "never", which is correctly in the
    # past for every comparison.
    _migrate_add_column(db, "saves", "last_kill_at", "INTEGER NOT NULL DEFAULT 0")

    # Kill-rate tokens. Defaults to the full bucket so an existing character is
    # not penalised for having played before this column existed.
    _migrate_add_column(db, "saves", "kill_tokens", "REAL NOT NULL DEFAULT 20.0")

    # WHAT DEATH HAS COST THIS ACCOUNT, cumulatively, and the only number in
    # the game that is supposed to go up when you lose.
    #
    # ACCOUNT-SHARED, like lusions and the bank, because one of the two things
    # it counts already is: lusions are account-wide, so a per-character score
    # would credit whichever character happened to be holding the body. A
    # lifetime figure for the player is also the one a leaderboard wants.
    #
    # ADDED BY MIGRATION rather than only in CREATE TABLE, because elusion.db
    # already exists and holds real accounts. A column that only appears for
    # new databases is how the two halves of this project drift apart.
    _migrate_add_column(db, "accounts", "score", "INTEGER NOT NULL DEFAULT 0")

    # Millisecond timestamp of the last cast credited to this character, for the
    # rate limit in /api/fishing/catch. Same shape and same reasoning as the
    # kill columns above; a separate bucket because fishing and killing are
    # different activities with different natural rates, and sharing one would
    # mean a fishing trip throttling a boss fight.
    # consume_grants shipped a few hours before the server knew what a potion
    # restored, so the first shape recorded only THAT one was drunk. Any live
    # database created in that window gets the two columns here.
    _migrate_add_column(db, "consume_grants", "target", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(db, "consume_grants", "amount", "INTEGER NOT NULL DEFAULT 0")

    _migrate_add_column(db, "saves", "last_cast_at", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(db, "saves", "cast_tokens", "REAL NOT NULL DEFAULT 6.0")

    _migrate_role_column(db)

    # AFTER the line above, never before. That migration reads is_admin to
    # decide who becomes a dev; dropping the column first would silently demote
    # every one of them on a database that had not migrated yet.
    _migrate_drop_is_admin(db)

    # BAN STATE. Three columns rather than one, because a single nullable
    # timestamp cannot say both "not banned" and "banned forever" - NULL would
    # have to mean both.
    #
    #   is_banned = 0                       not banned
    #   is_banned = 1, ban_expires_at NULL  permanent
    #   is_banned = 1, ban_expires_at set   until that moment
    #
    # Permanent is a real state, queryable and displayable, rather than a very
    # large number. A ban of 9999 days shows the player a date decades away,
    # cannot be told apart from a long timeout, and stops being distinguishable
    # at all the moment someone types a bigger one.
    _migrate_add_column(db, "users", "is_banned", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(db, "users", "ban_expires_at", "INTEGER")
    _migrate_add_column(db, "users", "ban_reason", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(db, "users", "banned_by", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(db, "users", "banned_at", "INTEGER NOT NULL DEFAULT 0")

    # PRESENCE. See ONLINE_WINDOW_SECONDS. Existing sessions start at 0, which
    # reads as offline until their client's next heartbeat - the truthful
    # default for a row nobody has vouched for yet.
    _migrate_add_column(db, "sessions", "last_seen_at", "INTEGER NOT NULL DEFAULT 0")

    # LOGIN THROTTLE state. failed_logins counts consecutive misses; lockout_until
    # is a unix time before which login is refused. Both default 0 so every
    # existing account starts clean. See login() and SECURITY_NOTES.md (E-5).
    _migrate_add_column(db, "users", "failed_logins", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(db, "users", "lockout_until", "INTEGER NOT NULL DEFAULT 0")

    _migrate_seed_gold_ledger(db)
    _migrate_seed_lusion_ledger(db)

    db.commit()
    db.close()


def _migrate_bank_to_account(db):
    """
    Move a per-character bank onto the account, and re-key it by position.

    WHY THIS IS NOT _migrate_add_column's JOB: that helper adds a COLUMN.
    bank_items needed its PRIMARY KEY changed - (user_id, slot, item_id) became
    (user_id, position) - and SQLite cannot alter a primary key. The table has
    to be rebuilt.

    WHY IT IS NEEDED AT ALL: CREATE TABLE IF NOT EXISTS does nothing to a table
    that already exists, so the corrected schema above reaches a fresh database
    and no other. elusion.db still had the old shape, and every bank request
    against it failed with "no such column: position" - while the test suite
    passed, because it builds its database from scratch every run.

    IDEMPOTENT. Both halves check for the end state first, so this is safe on
    every boot forever.
    """
    columns = {row[1] for row in db.execute("PRAGMA table_info(bank_items)")}

    if columns and "position" not in columns:
        # Old shape. Read it out before touching anything.
        #
        # SUM across slots: the same item banked by two characters was two rows
        # under the old key and is one stack under the new one. Summing is the
        # only reading that does not throw away items.
        legacy = db.execute(
            """
            SELECT user_id, item_id, SUM(quantity)
              FROM bank_items
             GROUP BY user_id, item_id
             ORDER BY user_id, item_id
            """
        ).fetchall()

        db.execute("ALTER TABLE bank_items RENAME TO bank_items_legacy")
        db.execute(
            """
            CREATE TABLE bank_items (
                user_id  INTEGER NOT NULL,
                position INTEGER NOT NULL,
                item_id  TEXT    NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                PRIMARY KEY (user_id, position),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        # Positions are assigned in item_id order. The old schema had no notion
        # of where anything sat, so there is no original order to preserve -
        # any stable arrangement is as faithful as any other.
        next_position = {}
        for user_id, item_id, quantity in legacy:
            position = next_position.get(user_id, 0)
            db.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (user_id,))
            db.execute(
                "INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, ?, ?, ?)",
                (user_id, position, item_id, quantity),
            )
            next_position[user_id] = position + 1

        db.execute("DROP TABLE bank_items_legacy")

    # Banked GOLD was a column on `saves`, so it was per-character too. Pool it
    # into the account and zero the old column - which is also what makes this
    # half idempotent: a second run finds nothing left to move.
    save_columns = {row[1] for row in db.execute("PRAGMA table_info(saves)")}
    if "bank_gold" in save_columns:
        pooled = db.execute(
            "SELECT user_id, SUM(bank_gold) FROM saves GROUP BY user_id HAVING SUM(bank_gold) > 0"
        ).fetchall()
        for user_id, total in pooled:
            db.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (user_id,))
            db.execute(
                "UPDATE accounts SET bank_gold = bank_gold + ? WHERE user_id = ?",
                (total, user_id),
            )
            db.execute("UPDATE saves SET bank_gold = 0 WHERE user_id = ?", (user_id,))


def _migrate_role_column(db):
    """
    Carry the old is_admin boolean across to the `role` column, once.

    The ranks are player < mod < dev < owner. There is no 'admin' rank; the
    column this replaced was called is_admin, which is the only reason that
    word appears here at all.

    A boolean cannot express three ranks. The moment a mod exists, "can this
    person do X" stops being one bit and becomes an expression repeated in
    every endpoint - so the rank became an ordered column before there was any
    endpoint reading it, which is the cheap moment to change it.

    RUNS EXACTLY ONCE, and the guard is that the column did not exist a moment
    ago - not that the values in it look untouched.

    The first version guarded on `role = 'player'`, which reads as "only fill
    in rows nobody has set". It is not: an admin who was DEMOTED is a row with
    role 'player' and is_admin still 1, so every restart promoted them again.
    init_db() runs on every boot, so a migration guarded by anything other than
    "this has not happened yet" is a migration that happens forever.
    """
    existing = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "role" in existing:
        return

    db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'player'")

    # An old is_admin=1 account becomes a dev - the highest rank that is
    # storable, since owner comes from the environment.
    if "is_admin" in existing:
        db.execute("UPDATE users SET role = 'dev' WHERE is_admin = 1")


def _migrate_drop_is_admin(db):
    """
    Remove users.is_admin once `role` has taken over from it.

    MUST RUN AFTER _migrate_role_column(). See the call site.

    Dropping a column from the one database holding real accounts is not
    something to do casually, and the earlier version of this deliberately did
    not: it left the column in place on the grounds that nothing read it. That
    was the right call while the client still received an is_admin key. The
    client does not any more, and a column nobody reads but everybody sees is
    how "there is no admin rank" keeps needing to be explained.

    ALTER TABLE DROP COLUMN needs SQLite 3.35 (March 2021). An older build
    keeps the column, which costs nothing - no query names it.
    """
    existing = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "is_admin" not in existing:
        return
    if "role" not in existing:
        # Never drop the source before the destination exists.
        return

    try:
        db.execute("ALTER TABLE users DROP COLUMN is_admin")
    except sqlite3.OperationalError as exc:
        print(f"[migrate] users.is_admin left in place ({exc}); nothing reads it")


def _migrate_rename_admin_actions(db):
    """
    Rename the audit table off the word 'admin'.

    RUNS BEFORE THE SCHEMA BLOCK, which is the whole point. init_db() creates
    staff_actions with CREATE TABLE IF NOT EXISTS; if that ran first it would
    make an empty staff_actions, this rename would find the name taken and skip,
    and every moderation action ever recorded would be stranded in a table
    nothing queries. An audit trail that silently stops being the audit trail is
    worse than no audit trail.
    """
    names = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}
    if "admin_actions" in names and "staff_actions" not in names:
        db.execute("ALTER TABLE admin_actions RENAME TO staff_actions")

    # AN INDEX FOLLOWS ITS TABLE THROUGH A RENAME AND KEEPS ITS OWN OLD NAME.
    # So the line above leaves idx_admin_actions_target sitting on
    # staff_actions, and the schema block then creates idx_staff_actions_target
    # beside it: two indexes on one column, one of them still called admin, and
    # every insert paying to maintain both.
    #
    # Unconditional, deliberately - not inside the branch above. A database that
    # renamed before this line existed has already taken the branch and will
    # never take it again, and the stale index is still sitting there.
    db.execute("DROP INDEX IF EXISTS idx_admin_actions_target")


def _migrate_seed_lusion_ledger(db):
    """
    Opening balance for the lusion ledger, for exactly the reasons
    _migrate_seed_gold_ledger() gives: lusions existed before anything counted
    them, so without one entry the two sides differ forever by the supply
    already in the world.

    ONLY WHEN THE LEDGER IS EMPTY, or running twice mints the whole supply
    again.
    """
    row = db.execute("SELECT COUNT(*) FROM lusion_ledger").fetchone()
    if row is not None and int(row[0]) > 0:
        return

    opening = int(db.execute("SELECT COALESCE(SUM(lusions), 0) FROM accounts").fetchone()[0])
    db.execute(
        "INSERT INTO lusion_ledger (at, user_id, slot, delta, reason, detail)"
        " VALUES (?, NULL, NULL, ?, 'opening_balance', ?)",
        (int(time.time()), opening, "lusions held when the ledger began"),
    )
    app.logger.info("[LEDGER] opening lusion balance recorded: %d", opening)


def _migrate_seed_gold_ledger(db):
    """
    OPENING BALANCE. The invariant compares what the server recorded creating
    against what players hold - but gold existed before the ledger did, so on
    the first boot after this ships the two sides differ by exactly the supply
    already in the world.

    A real ledger handles this the same way: one opening entry equal to the
    balance on the day you started counting. After it, every change goes
    through gold_delta() and the equation holds forever.

    ONLY WHEN THE LEDGER IS EMPTY. Running twice would mint the whole supply a
    second time, which is the opposite of what an audit table is for.
    """
    row = db.execute("SELECT COUNT(*) FROM gold_ledger").fetchone()
    if row is not None and int(row[0]) > 0:
        return

    carried = db.execute("SELECT COALESCE(SUM(gold), 0) FROM saves").fetchone()[0]
    banked = db.execute("SELECT COALESCE(SUM(bank_gold), 0) FROM accounts").fetchone()[0]
    opening = int(carried) + int(banked)

    # Written even when it is zero. A fresh database gets a row saying the world
    # started empty, which is a fact worth being able to point at - and it means
    # "no rows" always and only means "the migration has not run".
    db.execute(
        "INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
        " VALUES (?, NULL, NULL, ?, 'opening_balance', ?)",
        (int(time.time()), opening, "supply present when the ledger was added"),
    )
    print("[LEDGER] opening balance recorded: %d gold" % opening)


class InsufficientGold(Exception):
    """Raised by gold_delta / bank_gold_delta when a debit would take a balance
    below zero.

    WHY RAISE INSTEAD OF RETURN A FLAG. It aborts the caller's WHOLE
    transaction, and that is the safety property. A purchase reads the balance,
    grants the item, then burns the gold - three steps in one transaction. Two
    of those requests racing each other both pass the Python "can you afford it"
    read, and without an atomic stop both would grant and both would burn,
    leaving a negative balance and two items paid for once. Here the second
    burn's guarded UPDATE matches no row, this raises, the caller never reaches
    its commit, and the item grant rolls back WITH the failed burn. A caller
    that does nothing at all is therefore still safe - it just surfaces as a 500
    (rolled back) rather than a clean 409. Callers with a nice error to give may
    catch it; safety does not depend on their doing so.
    """
    pass


def gold_delta(db, user_id, slot, delta, reason, detail=""):
    """
    THE ONLY WAY GOLD MAY CHANGE. Moves a character's purse and records why, in
    one place, so the ledger can never disagree with the balances.

    Callers must already be inside their transaction and must not commit - the
    balance and its ledger row have to land together or not at all. Same
    discipline the bank transfer uses for its two tables: there is no moment
    where the gold exists in neither place, and here no moment where it moved
    without being recorded.

    delta is signed. Positive mints (a gold pile taken from a bag the server
    rolled), negative burns (a vendor purchase, the kingdom tax). A transfer is
    NOT a delta - see gold_ledger in init_db().

    Returns the new balance, or None when the slot does not exist. Raises
    InsufficientGold when a burn would drop the balance below zero - the atomic
    stop that closes the check-then-write race (see the class above).
    """
    if delta == 0:
        return None

    # GUARDED, CONDITIONAL WRITE. The `gold + ? >= 0` in the WHERE is what makes
    # this atomic: SQLite re-checks the balance AT WRITE TIME against whatever it
    # is right now, not against the value the caller read a few instructions ago.
    # For a mint (delta > 0) the guard is always satisfied, so nothing changes
    # there. When it matches no row the follow-up read tells the two reasons
    # apart: a missing slot (the old None contract) or a burn that would go
    # negative (the new atomic refusal).
    cur = db.execute(
        "UPDATE saves SET gold = gold + ?, updated_at = ?"
        " WHERE user_id = ? AND slot = ? AND gold + ? >= 0",
        (int(delta), int(time.time()), user_id, slot, int(delta)),
    )
    if cur.rowcount == 0:
        exists = db.execute(
            "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
        ).fetchone()
        if exists is None:
            return None
        raise InsufficientGold(
            "debit of %d would take slot %s below zero" % (delta, slot))

    db.execute(
        "INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (int(time.time()), user_id, slot, int(delta), str(reason), str(detail)[:120]),
    )
    row = db.execute(
        "SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    return int(row["gold"]) if row is not None else None


def gold_supply(db):
    """
    The invariant, as a dictionary. recorded should equal held; drift is gold
    that entered or left without going through gold_delta().

    REPORTS RATHER THAN RAISES. A mismatch is something to alert on and
    investigate, not a reason to take the server down - the players' balances
    are still their balances, and refusing to serve them would turn an
    accounting discrepancy into an outage.
    """
    one = lambda q: int(db.execute(q).fetchone()[0])
    recorded = one("SELECT COALESCE(SUM(delta), 0) FROM gold_ledger")
    carried = one("SELECT COALESCE(SUM(gold), 0) FROM saves")
    banked = one("SELECT COALESCE(SUM(bank_gold), 0) FROM accounts")
    minted = one("SELECT COALESCE(SUM(delta), 0) FROM gold_ledger WHERE delta > 0")
    burned = -one("SELECT COALESCE(SUM(delta), 0) FROM gold_ledger WHERE delta < 0")
    held = carried + banked
    return {
        "minted": minted,
        "burned": burned,
        "recorded": recorded,
        "held": held,
        "carried": carried,
        "banked": banked,
        "drift": held - recorded,
        "balanced": held == recorded,
    }


def _migrate_add_column(db, table, column, definition):
    """
    Add a column to an existing table, once.

    CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
    a new column in the schema above reaches a FRESH database and no other.
    elusion.db already holds real accounts and characters, and dropping it to
    pick up a column would delete them - so existing databases get the column
    added here instead.

    PRAGMA table_info is the check rather than catching the "duplicate column"
    error, because a bare try/except around ALTER would also swallow a genuine
    failure (locked database, bad definition) and leave the column silently
    missing until the first query against it.
    """
    existing = {row[1] for row in db.execute("PRAGMA table_info(%s)" % table)}
    if column in existing:
        return
    db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))


init_db()


# =============================================================================
# VALIDATION
# =============================================================================

def validate_credentials(payload):
    """
    Check an incoming register/login body.
    Returns (is_valid, errors_list) - same shape as spells_api.
    """
    errors = []

    if not isinstance(payload, dict):
        errors.append("Payload must be a JSON object.")
        return False, errors

    for field in REQUIRED_FIELDS:
        if field not in payload:
            errors.append(f"Missing required field: {field}")

    if errors:
        return False, errors

    username = payload.get("username")
    password = payload.get("password")

    if not isinstance(username, str) or not USERNAME_PATTERN.match(username):
        errors.append(
            "Field 'username' must be 3-20 characters, letters, numbers and underscore only."
        )

    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        errors.append(
            f"Field 'password' must be a string of at least {MIN_PASSWORD_LENGTH} characters."
        )

    return len(errors) == 0, errors


# =============================================================================
# AUTH HELPERS
# =============================================================================

def issue_token(user_id):
    """Mint a session token and store it. Returns (token, expires_at)."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    expires_at = now + TOKEN_TTL

    db = get_db()
    # Seen NOW: whoever just logged in is, by definition, at the keyboard.
    db.execute(
        "INSERT INTO sessions (token, user_id, expires_at, last_seen_at)"
        " VALUES (?, ?, ?, ?)",
        (token, user_id, expires_at, now),
    )
    db.commit()

    return token, expires_at


def user_for_token(token):
    """Resolve a bearer token to a user row, or None if missing/expired."""
    if not token:
        return None

    db = get_db()
    row = db.execute(
        """
        SELECT u.id, u.username, u.role,
               u.is_banned, u.ban_expires_at, u.ban_reason, u.banned_by, u.banned_at,
               s.expires_at
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token = ?
        """,
        (token,),
    ).fetchone()

    if row is None:
        return None

    if row["expires_at"] < int(time.time()):
        # expired - clean it up rather than leaving dead rows around
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        db.commit()
        return None

    # THE BAN IS CHECKED HERE, not only at login, because login is not the only
    # door. Banning deletes the user's sessions in the same transaction, so a
    # live token should not exist - but "should not exist" is not a security
    # control, and require_auth wraps every authenticated route at once.
    #
    # The session goes with them rather than being left to expire.
    if ban_state(row) is not None:
        db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        db.commit()
        return None

    return row


def bearer_token():
    """Pull the token out of an 'Authorization: Bearer <token>' header."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header[7:].strip()


def _row_int(row, key):
    """Read an integer column defensively.

    A row read before the throttle migration ran would not carry the new
    columns; keys() lets this survive that instead of raising KeyError. Every
    live row has them after init, so this is belt-and-braces, not the mechanism.
    """
    if key in row.keys() and row[key] is not None:
        return int(row[key])
    return 0


def client_ip():
    """
    The address this request came from, as far as it can be trusted.

    THIS IS THE PART OF PER-IP THROTTLING THAT GOES WRONG, and it fails in two
    opposite directions depending on which mistake you make.

    TRUST X-Forwarded-For WHEN YOU ARE NOT BEHIND A PROXY and the header is
    attacker-controlled: a spray sets a different fake address on every request,
    every bucket holds one failure, the throttle never fires, and the log fills
    with invented addresses that frame innocent people.

    IGNORE IT WHEN YOU ARE BEHIND ONE and request.remote_addr is the proxy -
    the same value for every player alive. One brute-force run then trips the
    IP lockout for THE ENTIRE PLAYER BASE. The throttle becomes the outage.

    Neither is a setting you can guess from inside the app, so it is not
    guessed: ELUSION_TRUSTED_PROXIES is 0 by default, meaning "nothing in
    front of me, believe the socket". Set it to the number of proxies you
    actually run when you deploy, and ProxyFix reads that many hops back.
    """
    return request.remote_addr or ""


def _record_login_attempt(db, username, ip, ok, reason=""):
    """
    Write one row for this attempt, and shout about the failures.

    BOTH OUTCOMES ARE STORED, not just the failures. A window holding nothing
    but failures cannot tell "someone is being attacked" from "the server is
    down for everyone" - the successes are the baseline that makes the failures
    mean something.
    """
    db.execute(
        "INSERT INTO login_attempts (username, ip, ok, reason, at) VALUES (?, ?, ?, ?, ?)",
        (str(username or "")[:64], ip, 1 if ok else 0, reason, int(time.time())),
    )
    db.commit()

    if not ok:
        # Also to the app log, so a failure is visible to whoever is tailing
        # the process without them knowing the schema. warning, not info:
        # these are the lines someone should actually see.
        #
        # THE WORD IS TAKEN FROM THE REASON, because this table records
        # /register's refusals too and calling those "login failed" sends
        # whoever is reading at 3am to the wrong endpoint. The reason prefix
        # already carries the distinction - _ip_throttle_state() excludes
        # 'register-%' on exactly this basis - so the log line reads it rather
        # than hard-coding a word that is wrong for a third of the rows.
        app.logger.warning(
            "%s failed  user=%r  ip=%s  reason=%s",
            "registration" if str(reason).startswith("register-") else "login",
            str(username or "")[:64], ip or "?", reason or "bad-credentials",
        )


def _prune_login_attempts(db, now):
    """Drop rows past the retention window. Called on the login path, which is
    the only thing that writes here, so the table cannot grow without something
    also cleaning it."""
    db.execute(
        "DELETE FROM login_attempts WHERE at < ?",
        (now - LOGIN_LOG_RETENTION_SECONDS,),
    )
    db.commit()


def _record_account_ip(db, user_id, ip, now):
    """Remember that this account has been seen at this address.

    UPSERT rather than insert, so a player who logs in every day for a year
    leaves one row with a moving last_seen, not three hundred and sixty five.
    first_seen is preserved on conflict - it is the half that answers "has
    this account genuinely played from here, or did it appear today".

    ONLY CALLED ON SUCCESS. A failed login proves nothing about who was
    holding the keyboard, and recording those would fill this table with
    addresses belonging to whoever is currently guessing at the account.
    """
    if not ip or not user_id:
        return
    db.execute(
        """
        INSERT INTO account_ips (user_id, ip, first_seen, last_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, ip) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (user_id, ip, now, now),
    )
    db.commit()


def _ban_evasion_state(db, ip, now):
    """Has a currently-banned account logged in from this address?

    Returns the banned username when one has, or None.

    WHAT THIS IS FOR, AND WHAT IT IS NOT. The pattern it catches is the common
    one: banned, makes a new account, same connection, same minute. It is used
    to refuse REGISTRATION only. Existing accounts on the address keep logging
    in, because the alternative is that banning one teenager takes out their
    household, their school, or everyone behind a mobile carrier's shared
    address - which is the failure mode _ip_throttle_state's own comment warns
    about, and it is worse than the evasion it would prevent.

    IT EXPIRES WITH THE BAN. ban_state() reads expiry against now, so a served
    sentence stops blocking registrations the moment it is up. Nothing
    accumulates: an address is not poisoned for a stranger in two years by
    something somebody else did on it.

    IT DOES NOT APPLY ON A CROWDED ADDRESS. Above EVASION_BLOCK_MAX_ACCOUNTS
    the address is a building rather than a household, and refusing there lets
    anyone lock a whole campus out of the game by getting themselves banned on
    purpose. Read that constant's note - the attack was run against this code,
    not guessed at.

    HONEST ABOUT THE LIMIT: most home addresses are dynamic and a VPN defeats
    this outright. It is here to make casual evasion fail, not to make evasion
    impossible - the part that handles a determined evader is the staff link
    view, which makes the NEXT account visible rather than trying to stop it.
    """
    if not ip:
        return None

    # IS THIS AN ADDRESS, OR IS IT A BUILDING? Asked first, because on a campus
    # or a carrier pool the honest answer to everything below is "this tells
    # you nothing about who is registering". See EVASION_BLOCK_MAX_ACCOUNTS for
    # what an attacker does with the version that skips this.
    crowd = db.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM account_ips WHERE ip = ?",
        (ip,),
    ).fetchone()
    if int(crowd["n"] or 0) > EVASION_BLOCK_MAX_ACCOUNTS:
        return None

    # u.* RATHER THAN A COLUMN LIST, AND THAT IS NOT LAZINESS.
    #
    # ban_state() takes "a user row" and reads five columns off it - is_banned,
    # ban_expires_at, ban_reason, banned_by, banned_at - and a sqlite3.Row
    # raises IndexError for a column the SELECT did not fetch. This function
    # first shipped selecting only the three that its own logic cared about,
    # which turned every blocked registration into a 500: the refusal was
    # CORRECT and the response was a crash, so the block looked broken while
    # working perfectly.
    #
    # Naming the five columns here would fix today and break again the next
    # time ban_state() grows a field. Every other caller reaches ban_state()
    # with a full row out of SELECT * (see _user_by_name), so the invariant the
    # function is written against is "a whole user row" - this restores it
    # instead of maintaining a second, silently-drifting copy of ban_state()'s
    # column list at a distance from the function that owns it.
    #
    # Ordered by recency so the name carried into the staff log is the account
    # most recently seen here, not whichever the planner happened to hand back.
    candidates = db.execute(
        """
        SELECT u.*, a.last_seen AS linked_last_seen
        FROM account_ips a
        JOIN users u ON u.id = a.user_id
        WHERE a.ip = ? AND u.is_banned = 1
        ORDER BY a.last_seen DESC
        """,
        (ip,),
    ).fetchall()

    for candidate in candidates:
        # ban_state() rather than is_banned alone, so an expired sentence does
        # not keep blocking. Same rule the login path uses, asked the same way.
        if ban_state(candidate) is not None:
            return candidate["username"]
    return None


def _linked_accounts(db, user_id):
    """Other accounts seen at any address this one has used.

    THIS IS THE HALF THAT HANDLES THE DETERMINED EVADER, and it works by
    looking rather than by blocking. _ban_evasion_state() stops the lazy case -
    banned, new account, same connection, same minute - and a dynamic address
    or any VPN walks straight past it. Nothing at this layer can fix that.
    What CAN be fixed is that the next account used to arrive invisible: staff
    had to already suspect a name before they could check it. Now the account
    in front of them carries its own siblings.

    IT NEVER BANS ANYTHING. Every account here is returned for a human to read.
    An automatic ban on this signal would be a machine acting on a coincidence
    it cannot evaluate, and the cost of being wrong is somebody who did nothing
    losing an account they paid attention to.

    STRENGTH, NOT JUST PRESENCE. A link through an address carrying three
    accounts and a link through one carrying two hundred are different facts,
    and flattening them into "linked" is what turns this view into a way to
    punish people for their internet provider. `quietest` is the deciding
    number: the smallest crowd on any address the two accounts share. If they
    have even one quiet address in common that is a real link; if every shared
    address is crowded, they may simply both be behind the same carrier.

    COLUMNS ARE NAMED RATHER THAN SELECT *, unlike _ban_evasion_state above,
    and the difference is deliberate: that row is read and discarded inside a
    refusal, while this one feeds a staff response. A SELECT * here would carry
    password_hash into a payload the first time somebody stopped reading
    carefully. ban_state() still needs its five columns, so all five are listed.
    """
    rows = db.execute(
        """
        SELECT u.id, u.username, u.role,
               u.is_banned, u.ban_expires_at, u.ban_reason,
               u.banned_by, u.banned_at,
               COUNT(DISTINCT mine.ip) AS shared,
               MIN(crowd.n)            AS quietest,
               MAX(crowd.n)            AS busiest,
               MIN(theirs.first_seen)  AS first_linked,
               MAX(theirs.last_seen)   AS last_linked
        FROM account_ips mine
        JOIN account_ips theirs
             ON theirs.ip = mine.ip AND theirs.user_id != mine.user_id
        JOIN users u ON u.id = theirs.user_id
        JOIN (
            -- HOW BUSY EACH OF THIS ACCOUNT'S ADDRESSES IS.
            --
            -- Scoped by the inner WHERE rather than grouping the whole table:
            -- unscoped this is a full GROUP BY over every address the server
            -- has ever seen, on a route a mod hits repeatedly while working
            -- through a report. Restricting it to the two or three addresses
            -- actually in question turns that into index lookups on
            -- idx_account_ips_ip. Measured on 20k accounts / 35k links:
            -- 24ms unscoped, ~1ms scoped, same answer.
            SELECT ip, COUNT(DISTINCT user_id) AS n
            FROM account_ips
            WHERE ip IN (SELECT ip FROM account_ips WHERE user_id = ?)
            GROUP BY ip
        ) crowd ON crowd.ip = mine.ip
        WHERE mine.user_id = ?
        GROUP BY u.id
        ORDER BY u.is_banned DESC, quietest ASC, last_linked DESC
        LIMIT ?
        """,
        (user_id, user_id, LINKED_ACCOUNT_LIMIT + 1),
    ).fetchall()

    # One over the limit was fetched so "there are more" can be stated as a
    # fact rather than guessed from a full page.
    truncated = len(rows) > LINKED_ACCOUNT_LIMIT
    rows = rows[:LINKED_ACCOUNT_LIMIT]

    linked = []
    for row in rows:
        quietest = int(row["quietest"] or 0)
        linked.append({
            "username": row["username"],
            "role": role_for(row),
            "ban": ban_state(row),
            "shared_addresses": int(row["shared"] or 0),
            # The two crowd figures are returned raw as well as judged, so a
            # mod can disagree with SHARED_ADDRESS_ACCOUNTS without reading
            # this file.
            "quietest_address_accounts": quietest,
            "busiest_address_accounts": int(row["busiest"] or 0),
            "strength": "weak" if quietest >= SHARED_ADDRESS_ACCOUNTS else "strong",
            "first_linked": int(row["first_linked"] or 0),
            "last_linked": int(row["last_linked"] or 0),
        })

    return linked, truncated


def _prune_sessions(db, now):
    """Drop session rows whose expiry has passed.

    user_for_token() already deletes an expired row when someone presents one,
    which covers every token that comes back. It cannot cover the ones that do
    not: a player who uninstalls, or reinstalls and logs in fresh, leaves a row
    that nothing will ever look at again and nothing will ever remove.

    That is a slow leak rather than a vulnerability - an expired row grants
    nothing, because the expiry is checked on read - but it grows in one
    direction forever, on a table joined by every authenticated request, and
    the only symptom is that request getting gradually slower.

    Called from the login path, like _prune_login_attempts() above and for the
    same reason: the cleanup lives where the growth does, so the table cannot
    grow without something also tidying it.
    """
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    db.commit()


def _ip_throttle_state(db, ip, now):
    """
    Is this address currently locked out, and why?

    Returns None when it is fine, or a (reason, retry_seconds) pair.

    COUNTS FAILURES, NOT REQUESTS. A busy household behind one address logging
    in successfully all day never approaches either ceiling; only misses count,
    which is what keeps this from punishing shared connections.
    """
    if not ip:
        # No address to attribute anything to. Fall through to the per-account
        # throttle rather than inventing a bucket every unknown request shares.
        return None

    cutoff = now - IP_WINDOW_SECONDS
    # THE REASON FILTER IS AN ALLOWLIST. Read THROTTLE_EVIDENCE_REASONS before
    # changing it - the denylist this replaced made the lockout self-renewing
    # and the failure only shows up under sustained retries, which is to say on
    # launch day and not in testing.
    placeholders = ", ".join("?" * len(THROTTLE_EVIDENCE_REASONS))
    row = db.execute(
        """
        SELECT COUNT(*) AS failures, COUNT(DISTINCT username) AS names
        FROM login_attempts
        WHERE ip = ? AND ok = 0 AND at >= ?
          AND reason IN (%s)
        """ % placeholders,
        (ip, cutoff) + THROTTLE_EVIDENCE_REASONS,
    ).fetchone()

    failures = int(row["failures"] or 0)
    names = int(row["names"] or 0)

    # THE SPRAY TEST FIRST, because it is the cheaper attack and the tighter
    # bound. Six different usernames failing from one address inside ten
    # minutes is not somebody misremembering their own password.
    if names >= IP_MAX_USERNAMES:
        return ("ip-spray", IP_LOCKOUT_SECONDS)

    if failures >= IP_MAX_FAILURES:
        return ("ip-volume", IP_LOCKOUT_SECONDS)

    return None


def _register_throttle_state(db, ip, now):
    """
    Is this address probing /register for usernames that exist?

    Returns None when it is fine, or a (reason, retry_seconds) pair.

    A SEPARATE COUNT FROM THE LOGIN ONE, DELIBERATELY, and this is the whole
    design decision in this function.

    Register conflicts could simply have been recorded as ordinary failures and
    left to _ip_throttle_state(). They are not, because that gate trips at
    IP_MAX_USERNAMES — six distinct names in ten minutes — and someone signing
    up who tries "bob", "bob2", "bobby", "bobbyx", "bobby99" has done nothing
    wrong and would be locked out of LOGGING IN for a quarter of an hour for
    the crime of picking a popular name. The punishment has to fit what was
    actually observed.

    So the ceiling here is looser, and it only locks registration. The gate in
    the other direction is deliberately kept: /register checks the login state
    too, because an address already spraying passwords does not get to change
    endpoints and continue.

    WHAT THIS BUYS. Unthrottled, the 409/201 split answers "does this username
    exist" as many times as anyone cares to ask. It cannot be made to stop
    answering — a signup form has to say when a name is taken — so the only
    available lever is how OFTEN, and this turns an unlimited oracle into
    REGISTER_MAX_CONFLICTS per window, logged, and visible in the audit trail
    as a run of register-conflict rows from one address.
    """
    if not ip:
        return None

    cutoff = now - IP_WINDOW_SECONDS
    row = db.execute(
        """
        SELECT COUNT(*) AS conflicts
        FROM login_attempts
        WHERE ip = ? AND at >= ? AND reason = 'register-conflict'
        """,
        (ip, cutoff),
    ).fetchone()

    if int(row["conflicts"] or 0) >= REGISTER_MAX_CONFLICTS:
        return ("register-probe", IP_LOCKOUT_SECONDS)

    return None


def _register_failed_login(db, row, now):
    """One more consecutive miss for this account; lock it if that crosses the
    threshold. Called only for a real row with a wrong password - a login for a
    username that does not exist has nothing to count against."""
    count = _row_int(row, "failed_logins") + 1
    if count >= LOGIN_MAX_ATTEMPTS:
        db.execute(
            "UPDATE users SET failed_logins = 0, lockout_until = ? WHERE id = ?",
            (now + LOGIN_LOCKOUT_SECONDS, row["id"]),
        )
    else:
        db.execute(
            "UPDATE users SET failed_logins = ? WHERE id = ?",
            (count, row["id"]),
        )
    db.commit()


def is_owner(username):
    """
    True only for the account named by ELUSION_OWNER.

    Case-insensitive, because `users.username` is COLLATE NOCASE - Tunacan and
    tunacan are the same account, so an owner check that disagreed with the
    database about that would lock the owner out of their own server.

    Fails closed: no configured owner means nobody is the owner.
    """
    if not OWNER_USERNAME or not username:
        return False
    return str(username).casefold() == OWNER_USERNAME.casefold()


# The ranks, in order. Index is the comparison - "mod or above" is one
# integer test rather than an expression repeated in every endpoint, which is
# what a pile of booleans turns into the moment there is more than one of them.
#
# 'owner' is last and is NOT storable. users.role has a CHECK that refuses it,
# and role_for() supplies it from the environment instead. That is what makes
# the top rank ungrantable: there is no write that produces it.
ROLES = ("player", "mod", "dev", "owner")

# What a fresh account is, and what an unrecognised value is read as. A row
# holding something this server has never heard of - written by an older build,
# or by hand - must read as the LEAST privilege, never the most.
DEFAULT_ROLE = "player"

# The ranks a moderation endpoint may assign. 'owner' is absent and always will
# be - it comes from ELUSION_OWNER, so no request can grant it. Mirrors
# SETTABLE_ROLES in set_role.py.
SETTABLE_ROLES = tuple(r for r in ROLES if r != "owner")


def role_for(user):
    """
    The effective rank of a user row.

    The owner is decided before the column is consulted, so revoking their row
    cannot lock them out of their own server, and a column set to 'owner' by
    hand cannot grant it.
    """
    if is_owner(user["username"]):
        return "owner"

    stored = user["role"] if "role" in user.keys() else DEFAULT_ROLE
    return stored if stored in ROLES and stored != "owner" else DEFAULT_ROLE


def role_at_least(user, minimum):
    """
    True when this user's rank is `minimum` or higher.

    An unrecognised `minimum` denies rather than raising. It should never
    happen - the argument comes from this codebase, not from a request - but
    ROLES.index() on a name that is not there throws ValueError, and a route
    asking "may I" deserves a "no" rather than a 500 with a traceback in it.

    That is not theoretical: removing the 'admin' rank turned every surviving
    role_at_least(..., "admin") call into a crash.
    """
    if minimum not in ROLES:
        return False
    return ROLES.index(role_for(user)) >= ROLES.index(minimum)


def can_act_on(actor, target):
    """
    True when `actor` may moderate `target`.

    STRICTLY ABOVE, not at-or-above. One comparison produces every rule that
    was wanted:

        a mod cannot ban another mod      equal ranks, refused
        a dev can ban a mod               dev is above mod
        a dev cannot ban another dev      equal ranks, refused
        the owner can ban anyone          nothing is at or above the owner
        nobody can ban the owner          the owner is the top of ROLES
        a player cannot ban anyone        there is nothing below player

    It also refuses acting on yourself, since your own rank is never strictly
    below your own - which saves a separate guard against banning yourself.

    role_for() is used on both sides rather than the raw column, so the owner
    outranks everyone whatever their row says, and an unrecognised rank is
    treated as `player` on both sides of the comparison.
    """
    return ROLES.index(role_for(actor)) > ROLES.index(role_for(target))


def ban_state(user):
    """
    The live ban on a user row, or None when there is not one.

    NEEDS A ROW CARRYING ALL FIVE BAN COLUMNS: is_banned, ban_expires_at,
    ban_reason, banned_by, banned_at. A sqlite3.Row raises IndexError for a
    column its SELECT did not fetch, so a caller that narrows to the two this
    function branches on gets a 500 at the exact moment it decides to refuse
    someone - the decision correct, the response a crash. That is not
    hypothetical: _ban_evasion_state() shipped that way. Pass SELECT *, or
    name all five (see user_for_token, which does).

    Reads EXPIRY AGAINST NOW rather than trusting is_banned alone, so a served
    sentence stops mattering the moment it is up - nothing has to run on a
    schedule to release people, and a server that was switched off for a week
    does not keep anyone an extra week.
    """
    if not user["is_banned"]:
        return None

    expires = user["ban_expires_at"]
    if expires is not None and int(expires) <= int(time.time()):
        return None

    return {
        "permanent": expires is None,
        "expires_at": None if expires is None else int(expires),
        "reason": user["ban_reason"] or "",
        "banned_by": user["banned_by"] or "",
        "banned_at": int(user["banned_at"] or 0),
    }


def log_staff_action(actor, action, target_name, target_id=None, detail=""):
    """
    Record one moderation action. Called inside the same transaction as the
    thing it describes, so an action cannot happen without a line about it.
    """
    get_db().execute(
        """
        INSERT INTO staff_actions
               (actor_id, actor_name, action, target_id, target_name, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (actor["id"], actor["username"], action, target_id, target_name,
         detail, int(time.time())),
    )


def _user_by_name(username):
    return get_db().execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def _moderation_target(payload):
    """
    Resolve and authorise the target of a moderation action.

    Returns (row, None) or (None, response). Every refusal is the SAME 404,
    whether the account does not exist or is simply out of your reach - a
    distinguishable answer would let a mod map out who outranks them.
    """
    username = str(payload.get("username", "")).strip()
    if not username:
        return None, bad_request("username is required")

    target = _user_by_name(username)
    not_found = ({"error": "Not Found", "message": "No such account."}, 404)

    if target is None:
        return None, not_found
    if not can_act_on(g.user, target):
        return None, not_found

    return target, None


def require_role(minimum):
    """
    Decorator for a route that needs a rank. Sits inside @require_auth, which
    is what puts the user row on g.

        @app.post("/api/staff/whatever")
        @require_auth
        @require_role("mod")
        def whatever(): ...
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not role_at_least(g.user, minimum):
                # 404 rather than 403, same as require_owner. A 403 confirms
                # the route exists and that you are not allowed to use it,
                # which tells someone exactly where to push.
                return {"error": "Not Found", "message": "Not found."}, 404
            return view(*args, **kwargs)
        return wrapped
    return decorator


def require_owner(view):
    """
    Decorator for the handful of things only the server's owner may do.

    Deliberately NOT a check against a database column. See OWNER_USERNAME.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_owner(g.user["username"]):
            # 404, not 403. A 403 confirms the route exists and that you are
            # not allowed to use it, which tells an attacker where to aim.
            return {"error": "Not Found", "message": "Not found."}, 404
        return view(*args, **kwargs)

    return wrapped


def require_auth(view):
    """
    Decorator for any route that needs a logged-in user.
    Puts the user row on g.user so the view can read it.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = user_for_token(bearer_token())
        if user is None:
            return {
                "error": "Unauthorized",
                "message": "Missing, invalid or expired token.",
            }, 401
        g.user = user
        return view(*args, **kwargs)

    return wrapped


# =============================================================================
# AUTH
# =============================================================================

@app.post("/api/auth/register")
def register():
    """
    Create a new account
    ---
    tags:
      - Auth
    consumes:
      - application/json
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            username:
              type: string
            password:
              type: string
    responses:
      201:
        description: Account created, returns a session token
        schema:
          type: object
          properties:
            user_id:
              type: integer
            username:
              type: string
            token:
              type: string
            expires_at:
              type: integer
      400:
        description: Validation error
        schema:
          type: object
          properties:
            error:
              type: string
            message:
              type: array
              items:
                type: string
      409:
        description: Username already taken
        schema:
          type: object
          properties:
            error:
              type: string
            message:
              type: string
    """
    data = request.get_json(silent=True)

    is_valid, errors = validate_credentials(data)
    if not is_valid:
        return {"error": "Bad Request", "message": errors}, 400

    username = data["username"]

    db = get_db()
    ip = client_ip()
    now = int(time.time())

    # THE GATES COME BEFORE THE HASH, and that ordering is the point of both of
    # them. See /login for the same arrangement and the same reasoning.
    #
    # WHY /register NEEDED THIS AT ALL. /login is careful never to say whether
    # a username exists: one message for both failures, and the same cost on
    # both paths. /register answers the identical question outright and has to
    # — you cannot ask someone to choose a name and refuse to say it is taken.
    # What it must not do is answer that question an unlimited number of times.
    # Unthrottled, the 409/201 split is a clean oracle for the whole user table,
    # and it made the care taken on /login pointless: an attacker enumerates
    # here instead, then brings the list back to the login throttle.
    #
    # AND THE HASH WAS THE SECOND PROBLEM. generate_password_hash ran BEFORE
    # the INSERT, so every probe — including every one that was about to be
    # refused as a duplicate — cost a full scrypt: 32MB and about 100ms of work
    # done on the attacker's behalf, at their chosen rate. That is exactly the
    # denial-of-service lever the per-IP gate on /login exists to avoid, and it
    # was sitting wide open one endpoint over. Nothing is hashed now until the
    # request has passed both gates.
    _prune_login_attempts(db, now)

    # The login gate first: an address already spraying passwords does not get
    # to switch endpoints and carry on. This direction only — a clumsy signup
    # must not lock anyone out of logging in, which is why the register count
    # below is kept separate.
    ip_state = _ip_throttle_state(db, ip, now)
    if ip_state is not None:
        reason, retry = ip_state
        _record_login_attempt(db, username, ip, False, reason)
        return {
            "error": "Too Many Requests",
            "message": "Too many failed attempts from this address. Try again in %d seconds." % retry,
        }, 429

    probe_state = _register_throttle_state(db, ip, now)
    if probe_state is not None:
        reason, retry = probe_state
        _record_login_attempt(db, username, ip, False, reason)
        return {
            "error": "Too Many Requests",
            "message": "Too many registration attempts from this address. Try again in %d seconds." % retry,
        }, 429

    # BAN EVASION. The pattern this catches is the ordinary one: banned, makes
    # a new account, same connection, same minute.
    #
    # REGISTRATION ONLY - logins from this address are untouched, so the
    # sibling, the roommate and everyone behind the same mobile carrier keep
    # playing. Blocking their logins too would be the version that takes out a
    # household to stop one person, which is the trade _ip_throttle_state's
    # comment already refuses to make.
    #
    # 403, NOT 429. This is not "slow down", it is "not from here", and a retry
    # timer would invite exactly the retrying it is trying to stop. The banned
    # account's name is NOT returned: whoever is reading this either already
    # knows it, or is a stranger who should not be told who was banned on a
    # shared address.
    evader = _ban_evasion_state(db, ip, now)
    if evader is not None:
        # ONE LOG LINE, NOT TWO. _record_login_attempt() already writes the row
        # and warns, and a second dedicated warning here would double the noise
        # for the one event most likely to repeat - an evader retrying is
        # precisely what this refuses. The name of the banned account it
        # matched is not in the line because it does not need to be: the link
        # lives in account_ips permanently, where the staff view reads it, and
        # a log that rotates is the worse place to keep it.
        _record_login_attempt(db, username, ip, False, "register-ban-evasion")
        return {
            "error": "Forbidden",
            "message": "New accounts cannot be created from this connection.",
        }, 403

    password_hash = generate_password_hash(data["password"])

    try:
        cursor = db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, int(time.time())),
        )
        db.commit()
    except sqlite3.IntegrityError:
        # RECORDED, because a conflict is the only observable this endpoint
        # emits and a run of them is the signature of enumeration. The reason
        # is prefixed so _ip_throttle_state() can exclude it — see the note
        # there about why a name-picker must not trip the login spray gate.
        _record_login_attempt(db, username, ip, False, "register-conflict")
        return {
            "error": "Conflict",
            "message": "That username is already taken.",
        }, 409

    user_id = cursor.lastrowid
    token, expires_at = issue_token(user_id)
    # The account's first address. Recorded here as well as on login, so an
    # account that registers and is banned before it ever logs in still carries
    # the link that makes its next sibling visible.
    _record_account_ip(db, user_id, ip, now)

    # THE SAME SHAPE AS LOGIN AND SESSION. This used to omit keys that those
    # two returned, so a client that read the response after registering got a
    # different object than the one it got after logging in - and the
    # difference was a missing key rather than a false value, which is the kind
    # that surfaces as a crash somewhere else entirely.
    #
    # A new account is always a player, but it can be the owner: registering
    # the account named by ELUSION_OWNER is exactly how a fresh server gets one.
    return {
        "user_id": user_id,
        "username": username,
        "role": "owner" if is_owner(username) else DEFAULT_ROLE,
        "is_owner": is_owner(username),
        "token": token,
        "expires_at": expires_at,
    }, 201


@app.post("/api/auth/login")
def login():
    """
    Log in and receive a session token
    ---
    tags:
      - Auth
    consumes:
      - application/json
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            username:
              type: string
            password:
              type: string
    responses:
      200:
        description: Logged in
        schema:
          type: object
          properties:
            user_id:
              type: integer
            username:
              type: string
            role:
              type: string
            token:
              type: string
            expires_at:
              type: integer
      400:
        description: Validation error
      401:
        description: Bad username or password
        schema:
          type: object
          properties:
            error:
              type: string
            message:
              type: string
    """
    data = request.get_json(silent=True)

    is_valid, errors = validate_credentials(data)
    if not is_valid:
        return {"error": "Bad Request", "message": errors}, 400

    db = get_db()
    ip = client_ip()
    now = int(time.time())

    # THE PER-IP GATE COMES FIRST - before the user lookup, before the account
    # lockout, before any password hashing. A spray should cost the server one
    # indexed COUNT and nothing else; running scrypt for an attacker is doing
    # 32MB of work per guess ON THEIR BEHALF, which turns the good hash into a
    # denial-of-service lever.
    _prune_login_attempts(db, now)
    # Expired sessions go here too. This is the one request every returning
    # player makes, which makes it the natural place to tidy a table that only
    # ever grows - and the delete is indexed on a column every row has.
    _prune_sessions(db, now)
    ip_state = _ip_throttle_state(db, ip, now)
    if ip_state is not None:
        reason, retry = ip_state
        _record_login_attempt(db, (data or {}).get("username", ""), ip, False, reason)
        return {
            "error": "Too Many Requests",
            "message": "Too many failed attempts from this address. Try again in %d seconds." % retry,
        }, 429

    row = db.execute(
        # SELECT * rather than a column list: ban_state() and role_for() both read
        # from this row, and a list here is a list that gets forgotten the next
        # time a column is added - which is exactly what happened when the ban
        # columns arrived.
        "SELECT * FROM users WHERE username = ?",
        (data["username"],),
    ).fetchone()

    # LOCKOUT comes before the password check on purpose: while an account is
    # frozen, no amount of guessing gets a verdict, correct or not. Only a real
    # row can be frozen, so a locked username is knowable - the accepted cost of
    # per-account lockout (see SECURITY_NOTES.md E-5).
    if row is not None and _row_int(row, "lockout_until") > now:
        retry = _row_int(row, "lockout_until") - now
        _record_login_attempt(db, data["username"], ip, False, "account-locked")
        return {
            "error": "Too Many Requests",
            "message": "Too many failed attempts. Try again in %d seconds." % retry,
        }, 429

    # same response whether the user is missing or the password is wrong -
    # otherwise this endpoint tells an attacker which usernames exist.
    #
    # NOT `row is None or not check_password_hash(...)`, which is what this was.
    # `or` short-circuits, so a missing user skipped the hash entirely and came
    # back in about a millisecond while a real user with a wrong password spent
    # about a hundred in scrypt. The response was identical and the clock was
    # not, which enumerates usernames just as well. See _TIMING_DUMMY_HASH.
    #
    # The verify now ALWAYS runs — against the real hash when there is a row,
    # against the dummy when there is not — so both paths pay the same cost and
    # there is no early return for a stopwatch to find.
    stored_hash = row["password_hash"] if row is not None else _TIMING_DUMMY_HASH
    password_ok = check_password_hash(stored_hash, data["password"])

    if row is None or not password_ok:
        if row is not None:
            _register_failed_login(db, row, now)
        # The REASON is recorded even though the RESPONSE cannot distinguish
        # them. The 401 has to stay identical or it enumerates usernames; the
        # log is on our side of that line, and "no-such-user" against forty
        # names is the signature of a spray.
        _record_login_attempt(
            db, data["username"], ip, False,
            "no-such-user" if row is None else "bad-password",
        )
        return {
            "error": "Unauthorized",
            "message": "Incorrect username or password.",
        }, 401

    # A correct password clears the streak - the throttle is about CONSECUTIVE
    # misses, so one success resets it. Skip the write when there is nothing to
    # clear, which is the common case.
    if _row_int(row, "failed_logins") or _row_int(row, "lockout_until"):
        db.execute(
            "UPDATE users SET failed_logins = 0, lockout_until = 0 WHERE id = ?",
            (row["id"],),
        )
        db.commit()

    # AFTER the password check, deliberately. Telling someone their account is
    # banned before they have proved it is theirs would make this endpoint a
    # way to find out who is banned.
    #
    # 403 rather than 401, and the reason IS included - unlike the routes that
    # hide behind a 404, a banned player has every right to know they are
    # banned and why. Silence there reads as the game being broken.
    ban = ban_state(row)
    if ban is not None:
        _record_login_attempt(db, row["username"], ip, False, "banned")
        return {
            "error": "Forbidden",
            "message": "This account is banned." if ban["permanent"]
                       else "This account is banned until further notice.",
            "ban": ban,
        }, 403

    token, expires_at = issue_token(row["id"])
    _record_login_attempt(db, row["username"], ip, True, "")
    # ON SUCCESS ONLY, and after the ban check above - a banned account never
    # reaches here, so a ban cannot keep refreshing its own address history and
    # extending the block on everyone who shares the connection.
    _record_account_ip(db, row["id"], ip, now)

    return {
        "user_id": row["id"],
        "username": row["username"],
        # THE OWNER IS ALWAYS THE OWNER, whatever the column says. A stored
        # rank is a row someone could revoke; the owner is configuration.
        # Locking the owner out of their own server with an UPDATE should not
        # be possible, so role_for() answers from ELUSION_OWNER first.
        "role": role_for(row),
        "is_owner": is_owner(row["username"]),
        "token": token,
        "expires_at": expires_at,
    }, 200


@app.get("/api/auth/session")
@require_auth
def session_info():
    """
    Validate a stored token
    ---
    tags:
      - Auth
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200:
        description: Token is valid
        schema:
          type: object
          properties:
            user_id:
              type: integer
            username:
              type: string
            role:
              type: string
            expires_at:
              type: integer
      401:
        description: Missing, invalid or expired token
    """
    # THIS IS ALSO THE HEARTBEAT. The game calls it every HEARTBEAT seconds
    # while a character is in the world, for two reasons:
    #
    #   a 401 here is how a kicked or banned player finds out. Both delete the
    #   session; without a regular call the client never asks, and keeps
    #   playing until it is restarted - which made a kick a suggestion.
    #
    #   the stamp below is how staff see who is actually playing. A session
    #   lasts thirty days, so "holds a session" is not "online".
    #
    # ONE UPDATE BY PRIMARY KEY. The token is the row, so this touches exactly
    # the session asking and nothing else. expires_at is NOT extended: a
    # heartbeat proves the client is running, not that the login is fresher.
    db = get_db()
    db.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE token = ?",
        (int(time.time()), bearer_token()),
    )
    db.commit()

    return {
        "user_id": g.user["id"],
        "username": g.user["username"],
        "role": role_for(g.user),
        "is_owner": is_owner(g.user["username"]),
        "expires_at": g.user["expires_at"],
    }, 200


@app.post("/api/auth/logout")
@require_auth
def logout():
    """
    Invalidate the current token
    ---
    tags:
      - Auth
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      204:
        description: Token invalidated
      401:
        description: Missing, invalid or expired token
    """
    db = get_db()
    db.execute("DELETE FROM sessions WHERE token = ?", (bearer_token(),))
    db.commit()
    return "", 204


@app.post("/api/auth/logout-all")
@require_auth
def logout_all():
    """
    Invalidate every session for the logged-in account
    ---
    tags:
      - Auth
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200:
        description: Sessions invalidated
        schema:
          type: object
          properties:
            revoked:
              type: integer
      401:
        description: Missing, invalid or expired token
    """
    # WHY THIS EXISTS, given TOKEN_TTL is thirty days.
    #
    # A long token is the right call for a game - nobody wants to retype a
    # password to play for twenty minutes - but it is only defensible if there
    # is a way to END one early. Without this, a token that leaks stays valid
    # for up to a month and nothing anyone can do shortens that: changing a
    # password would not help, because /api/auth/logout only kills the token
    # doing the asking, and the attacker is holding a different one.
    #
    # So the answer to "30 days is a long time" is not a smaller number that
    # annoys every honest player. It is this: the window stays generous, and it
    # can be slammed shut on demand.
    #
    # DELETES THE CALLER'S TOKEN TOO. "Log out everywhere" that leaves the
    # device you typed it on still logged in is not what anyone means by it,
    # and if the reason you pressed it is that you think you were compromised,
    # the ambiguity is the last thing you need.
    db = get_db()
    cursor = db.execute("DELETE FROM sessions WHERE user_id = ?", (g.user["id"],))
    db.commit()

    app.logger.warning(
        "all sessions revoked  user=%r  ip=%s  count=%d",
        g.user["username"], client_ip() or "?", cursor.rowcount,
    )

    return {"revoked": cursor.rowcount}, 200


@app.post("/api/auth/password")
@require_auth
def change_password():
    """
    Change the logged-in account's password
    ---
    tags:
      - Auth
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            current_password:
              type: string
            new_password:
              type: string
    responses:
      200:
        description: Password changed; every session was revoked and a new one issued
        schema:
          type: object
          properties:
            token:
              type: string
            expires_at:
              type: integer
            revoked:
              type: integer
      400:
        description: Validation error
      401:
        description: Missing token, or the current password is wrong
      429:
        description: Too many failed attempts
    """
    # THE OTHER HALF OF E-6. logout-all lets you end sessions; without this
    # there was no way to change the credential that leaked, so an attacker who
    # had the password simply logged back in and got a fresh 30-day token.
    # Revoking without rotating is a door you keep closing on someone holding
    # the key.
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return bad_request("Payload must be a JSON object.")

    current = data.get("current_password")
    new = data.get("new_password")

    if not isinstance(current, str) or not isinstance(new, str):
        return bad_request("current_password and new_password are required strings.")
    if len(new) < MIN_PASSWORD_LENGTH:
        return bad_request(
            "Field 'new_password' must be at least %d characters." % MIN_PASSWORD_LENGTH
        )
    if new == current:
        return bad_request("The new password must be different from the current one.")

    db = get_db()
    ip = client_ip()
    now = int(time.time())

    row = db.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if row is None:
        # The token resolved a moment ago, so this is a deleted account racing
        # its own session rather than anything the caller did.
        return {"error": "Unauthorized", "message": "No such account."}, 401

    # THE CURRENT PASSWORD IS REQUIRED, and it is the whole point. A valid token
    # is not proof of identity here - a stolen token is exactly the situation
    # this endpoint exists for, and letting one set a new password would hand
    # the account to the thief rather than take it back.
    #
    # THROTTLED LIKE LOGIN, for the same reason: this is a second place to guess
    # a password, and one that answers from an authenticated session. Leaving it
    # unthrottled would put the lock back on the front door and a window beside
    # it. Both ceilings apply, and a miss counts against both.
    ip_state = _ip_throttle_state(db, ip, now)
    if ip_state is not None:
        reason, retry = ip_state
        _record_login_attempt(db, row["username"], ip, False, reason)
        return {
            "error": "Too Many Requests",
            "message": "Too many failed attempts from this address. Try again in %d seconds." % retry,
        }, 429

    if _row_int(row, "lockout_until") > now:
        retry = _row_int(row, "lockout_until") - now
        _record_login_attempt(db, row["username"], ip, False, "account-locked")
        return {
            "error": "Too Many Requests",
            "message": "Too many failed attempts. Try again in %d seconds." % retry,
        }, 429

    if not check_password_hash(row["password_hash"], current):
        _register_failed_login(db, row, now)
        _record_login_attempt(db, row["username"], ip, False, "bad-password-on-change")
        return {
            "error": "Unauthorized",
            "message": "The current password is incorrect.",
        }, 401

    # ONE TRANSACTION: re-hash, revoke, re-issue. Splitting these leaves a window
    # where the password is new and the attacker's session is still live, which
    # is the exact state the endpoint exists to remove.
    #
    # EVERY session goes, including the caller's own, and then a fresh one is
    # issued to the caller. That ordering matters: "change my password" must not
    # be a way to log everyone out except whoever currently holds a stolen token.
    new_hash = generate_password_hash(new)
    db.execute(
        "UPDATE users SET password_hash = ?, failed_logins = 0, lockout_until = 0 WHERE id = ?",
        (new_hash, row["id"]),
    )
    cursor = db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
    db.commit()

    revoked = cursor.rowcount
    token, expires_at = issue_token(row["id"])
    _record_login_attempt(db, row["username"], ip, True, "password-changed")

    app.logger.warning(
        "password changed  user=%r  ip=%s  sessions_revoked=%d",
        row["username"], ip or "?", revoked,
    )

    return {"token": token, "expires_at": expires_at, "revoked": revoked}, 200


# =============================================================================
# SAVES  -  GET/PUT /api/save
# =============================================================================

MAX_SLOT = 3
VALID_CLASSES = {"warrior", "mage", "tank", "healer"}


def parse_slot(raw):
    """
    Validate a slot number coming from a query string or a JSON body.

    Note the bool check: in Python `True == 1` and `isinstance(True, int)` is
    True, so without it `{"slot": true}` would quietly be accepted as slot 1.
    """
    if isinstance(raw, bool):
        return None
    try:
        slot = int(raw)
    except (TypeError, ValueError):
        return None
    if slot < 0 or slot > MAX_SLOT:
        return None
    return slot


def bad_request(message):
    return {"error": "Bad Request", "message": message}, 400


def parse_pet_id(raw):
    """
    Validate an active_pet_id from a request body.

    Same shape as a bank item_id - any string the client's item registry knows,
    capped at 64 characters. Empty string is valid and means "no pet out".
    Returns None if the value can't be one, so the caller can 400 rather than
    store something meaningless.
    """
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return None
    if not isinstance(raw, (str, int, float)):
        return None
    text = str(raw).strip()
    if len(text) > 64:
        return None
    return text


# THE SLOT NAMES COME OUT OF THE CATALOGUE. See gamedata.EQUIP_SLOTS.
#
# THIS LINE USED TO BE THE LIST ITSELF, typed from memory, and it was wrong in
# both directions at once:
#
#     ("weapon", "helm", "chest", "robe", "legs", "shield", "ring", "amulet")
#
# ItemData.EquipSlot has no "robe" - a robe is a mage's CHEST piece - and it
# does have BOOTS, which is missing above. Nothing errored, and nothing could
# have: a server holding its own copy of a vocabulary has no way to discover it
# disagrees with the game. It would simply have refused every pair of boots
# anyone owned, and accepted a slot no client would ever send. The reasoning
# written here about enums being renumbered was right and was being applied to
# a list that had never been in step in the first place.
#
# The tuple below is now only a FALLBACK, for a gamedata.json exported before
# equip_slot_name existed - the same arrangement the regen constants use.
# _warn_if_protections_unarmed() says so at boot when it is in play.
EQUIP_SLOTS_FALLBACK = (
    "weapon", "helm", "chest", "legs", "boots", "shield", "ring", "amulet",
)
EQUIP_SLOTS = (tuple(sorted(gamedata.EQUIP_SLOTS)) if gamedata.EQUIP_EXPORTED
               else EQUIP_SLOTS_FALLBACK)

# A hotbar is nine keys. Fixed, because the client draws nine.
HOTBAR_SIZE = 9


def parse_equipment(raw, class_id="", character_level=0):
    """
    Validate an equipment map from a request body: {slot_name: item_id}.

    Returns a dict, or None when the value cannot be one - so the caller can
    400 rather than store something the client will later fail to read back.

    WHAT IS CHECKED. The slot names have to be ones the catalogue uses, the ids
    have to be items it holds, and then gamedata.equip_check() settles the
    three questions that decide whether wearing it means anything: is this item
    worn in THAT slot, may this CLASS wear it, and is the character high enough
    LEVEL.

    WHAT THIS USED TO CHECK, AND WHY THAT WAS DEFENSIBLE UNTIL IT WASN'T. The
    first version stopped after "the slot exists and the item exists", and said
    so on purpose: equipping is a reference to a bag item rather than a move of
    one, so the server was recording a preference and had no business policing
    it. That argument holds exactly as long as equipment does nothing. It stops
    the moment an equipped weapon decides what a hit is worth - and in the
    meantime {"helm": "embersword"} was a legal save, so the column combat is
    about to read could already contain a sword worn on the head.

    character_level MUST BE THE SERVER'S. /api/save does not take level from
    the body for exactly this family of reasons; passing the payload's number
    in here would hand the gate back to the client it is meant to bind.

    class_id IS THE ONE IN THE SAME SAVE, which is self-consistent rather than
    circular: a client that claims to be a mage to wear a robe is also claiming
    to be a mage for max_hp, and the save endpoint recomputes the pools from
    that claim a few lines down. Lying costs more than it buys.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None

    out = {}
    for slot_name, item_id in raw.items():
        if slot_name not in EQUIP_SLOTS:
            return None
        if item_id is None or item_id == "":
            continue
        if isinstance(item_id, bool) or not isinstance(item_id, (str, int, float)):
            return None
        text = str(item_id).strip()
        if text == "":
            continue
        if len(text) > 64 or not gamedata.has_item(text):
            return None

        # SKIPPED ENTIRELY on a gamedata.json that predates the equipment
        # export, because every slot would read as "" there and every honest
        # save would be refused. A server in that state is warned about at
        # boot; it is not a server that should stop accepting saves.
        if gamedata.EQUIP_EXPORTED:
            verdict = gamedata.equip_check(text, slot_name, class_id, character_level)
            if not verdict["ok"]:
                return None

        out[slot_name] = text
    return out


def parse_hotbar(raw):
    """
    Validate a hotbar from a request body: a list of item ids, "" for empty.

    PADDED AND TRIMMED rather than refused on length. An older client that
    sends seven entries is not lying about anything - it just predates two of
    the keys - and 400-ing an otherwise honest save over the shape of a
    convenience feature would stop that client saving at all.
    """
    if raw is None:
        return [""] * HOTBAR_SIZE
    if not isinstance(raw, list):
        return None

    out = []
    for item_id in raw[:HOTBAR_SIZE]:
        if item_id is None or item_id == "":
            out.append("")
            continue
        if isinstance(item_id, bool) or not isinstance(item_id, (str, int, float)):
            return None
        text = str(item_id).strip()
        if text == "":
            out.append("")
            continue
        if len(text) > 64 or not gamedata.has_item(text):
            return None
        out.append(text)
    return out + [""] * (HOTBAR_SIZE - len(out))


# The ceiling on one character's explored map. Every area in the game together
# is under 3 KB of bits before compression, and base64 of the deflated form is
# a few hundred bytes an area - so 64 KB is roughly twenty times the largest
# honest payload, and still small enough that a client cannot use the column as
# free storage.
MAX_EXPLORED_BYTES = 65536

# Sanity bounds on one area's dimensions, mirroring WorldMap.MAX_AREA_TILES.
MAX_EXPLORED_TILES = 262144


def parse_explored(raw):
    """
    Validate an exploration map: {area_id: {w, h, ox, oy, bits}}.

    Returns a dict, or None when the value cannot be one.

    WHAT IS CHECKED IS THE SHAPE, NOT THE CONTENT. The bits are a compressed
    bitmask this server has no reason to decompress: there is no claim being
    made that anyone could benefit from. What matters is that the column
    contains a bounded amount of JSON of a known shape, so it cannot be used
    as a place to park arbitrary data and cannot make the save endpoint 500 on
    read-back.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None

    encoded = json.dumps(raw)
    if len(encoded) > MAX_EXPLORED_BYTES:
        return None

    out = {}
    for area, entry in raw.items():
        if not isinstance(area, str) or len(area) > 64:
            return None
        if not isinstance(entry, dict):
            return None

        width = entry.get("w")
        height = entry.get("h")
        if not isinstance(width, int) or not isinstance(height, int):
            return None
        if isinstance(width, bool) or isinstance(height, bool):
            return None
        if width <= 0 or height <= 0 or width * height > MAX_EXPLORED_TILES:
            return None

        for key in ("ox", "oy"):
            value = entry.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int):
                return None

        bits = entry.get("bits", "")
        if not isinstance(bits, str) or len(bits) > MAX_EXPLORED_BYTES:
            return None

        out[area] = {
            "w": width, "h": height,
            "ox": int(entry.get("ox", 0)), "oy": int(entry.get("oy", 0)),
            "bits": bits,
        }
    return out


def _stored_json(raw, fallback):
    """Read one of the JSON columns back, forgiving anything unreadable.

    A row written by a future version, or corrupted by hand, must not make the
    save endpoint 500 - the character is still playable without their hotbar."""
    try:
        value = json.loads(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def save_row_to_dict(row):
    return {
        "slot": row["slot"],
        "class_id": row["class_id"],
        "name": row["name"],
        "level": row["level"],
        "area": row["area"],
        "active_pet_id": row["active_pet_id"],
        "equipment": _stored_json(row["equipment"], {}),
        "hotbar": _stored_json(row["hotbar"], [""] * HOTBAR_SIZE),
        "explored": _stored_json(row["explored"], {}),
        "updated_at": row["updated_at"],
    }


@app.get("/api/save")
@require_auth
def list_saves():
    """
    List the account's character slots
    ---
    tags:
      - Save
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200:
        description: Occupied slots only; an empty account returns an empty list
      401:
        description: Missing, invalid or expired token
    """
    rows = get_db().execute(
        "SELECT * FROM saves WHERE user_id = ? ORDER BY slot",
        (g.user["id"],),
    ).fetchall()

    return {
        "username": g.user["username"],
        "slots": [save_row_to_dict(r) for r in rows],
    }, 200


@app.put("/api/save")
@require_auth
def write_save():
    """
    Create or overwrite one character slot
    ---
    tags:
      - Save
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, class_id, name]
          properties:
            slot:     {type: integer, example: 0}
            class_id: {type: string,  example: warrior}
            name:     {type: string,  example: Tunacan}
            level:    {type: integer, example: 12}
            area:     {type: string,  example: elusion}
            active_pet_id:
              type: string
              example: petpoisonslimesmall
              description: "Pet currently out, as the client's item_id. Empty string means none. Omitted leaves the stored value unchanged."
    responses:
      200:
        description: Slot written
      400:
        description: Invalid slot, class, name or active_pet_id
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    class_id = str(payload.get("class_id", "")).strip().lower()
    if class_id not in VALID_CLASSES:
        return bad_request("class_id must be one of: %s" % ", ".join(sorted(VALID_CLASSES)))

    name = str(payload.get("name", "")).strip()
    if not name or len(name) > 20:
        return bad_request("name must be 1-20 characters")

    # NOT from the payload. level is server-owned (see SERVER_OWNED_STATS): a
    # new character starts at 1, and an existing one keeps whatever the server
    # has. Taking it from the client here would have left an open door beside
    # the one /api/player/status just closed.
    level = 1
    area = str(payload.get("area", "elusion")).strip() or "elusion"

    # OMITTED means "leave it alone", not "clear it".
    #
    # The client writes this slot on character creation and again on every save,
    # and not every one of those callers knows or cares which pet is out. If a
    # missing key cleared the field, one save from a caller that doesn't send it
    # would silently unequip the player's pet - and a pet is a 1-in-216 drop, so
    # that is the last thing in the game that should vanish by omission.
    #
    # Sending "" explicitly is how you clear it. That's a deliberate act.
    pet_key_sent = "active_pet_id" in payload
    active_pet_id = parse_pet_id(payload.get("active_pet_id")) if pet_key_sent else ""
    if pet_key_sent and active_pet_id is None:
        return bad_request("active_pet_id must be a string of at most 64 characters")

    explored_key_sent = "explored" in payload
    explored = parse_explored(payload.get("explored")) if explored_key_sent else {}
    if explored_key_sent and explored is None:
        return bad_request(
            "explored must be an object of {area: {w, h, ox, oy, bits}} and "
            "under %d bytes" % MAX_EXPLORED_BYTES)

    hotbar_key_sent = "hotbar" in payload
    hotbar = parse_hotbar(payload.get("hotbar")) if hotbar_key_sent else []
    if hotbar_key_sent and hotbar is None:
        return bad_request("hotbar must be a list of known item ids, or \"\" for empty")

    now = int(time.time())

    db = get_db()

    # Read BEFORE the upsert, because afterwards there is no way to tell a
    # character that was just created from one that already existed - and they
    # need different treatment below. The level comes back with it: equipment
    # is gated on it, and taking that number from the payload would hand the
    # gate to the client. A character that does not exist yet is level 1,
    # which is what the INSERT below is about to store.
    existing = db.execute(
        "SELECT level FROM saves WHERE user_id = ? AND slot = ?", (g.user["id"], slot)
    ).fetchone()
    is_new = existing is None
    stored_level = 1 if is_new else int(existing["level"])

    # SAME KEY-PRESENT RULE AS THE PET, for the same reason spelled out above.
    # Several callers write this slot and not all of them know what the player
    # is wearing; a missing key must keep what is stored, and an explicit
    # empty one is how you take everything off.
    #
    # PARSED HERE RATHER THAN WITH THE REST OF THE BODY because it is the one
    # field whose validity depends on the character, not just on the JSON - the
    # slot has to suit the item, the class has to be allowed it and the level
    # has to reach it. The SELECT above is the only thing between the two, and
    # a read is not a write: every refusal below still happens before anything
    # is stored.
    # IGNORED, NOT REFUSED - and this is the line that makes equipment
    # server-owned rather than merely server-moved.
    #
    # /api/character/equip and /unequip take the item OUT of the backpack and
    # put it back, in one transaction, and the take IS the ownership check.
    # All of that is worth nothing if a client can then PUT whatever equipment
    # map it likes here: it would simply skip the endpoints and dress itself,
    # in the column combat reads to decide what a hit is worth. That is E-1
    # one column over, and E-8 one field over.
    #
    # IGNORED rather than 400, exactly like gold. An un-updated client still
    # sends `equipment` on every save, and refusing an otherwise honest sync
    # over a field it is no longer allowed to set would break saving for
    # anyone who had not restarted their game. It is named in `ignored` so a
    # client can see it is being dropped rather than silently disagreeing with
    # the server forever.
    #
    # parse_equipment() is still used - by nothing on this path now, but it is
    # what the equip endpoint's checks are made of, and it stays the one place
    # that knows what a wearable map looks like.
    equipment_ignored = "equipment" in payload

    # ALWAYS 0, and that is the whole point rather than a leftover.
    #
    # The upsert below reads this as "did the caller supply equipment" and
    # writes excluded.equipment when it is true. Setting it from the payload
    # while forcing `equipment` to {} - which is what the first draft of this
    # change did - would write an EMPTY map over the character's gear on every
    # save. That is the same stripping bug this whole endpoint pair exists to
    # fix, reintroduced by the fix.
    #
    # Zero means the CASE keeps saves.equipment, which is what the equip
    # endpoints wrote and the only thing that should ever have written it.
    equip_key_sent = 0
    equipment = {}

    # ON CONFLICT rather than DELETE-then-INSERT: an upsert is one statement,
    # so there is no window where the slot exists in neither state.
    #
    # active_pet_id uses excluded.* only when the caller actually sent the key;
    # otherwise it keeps whatever the row already holds. On a fresh INSERT there
    # is nothing to keep, so it lands as the '' default.
    db.execute(
        """
        INSERT INTO saves (user_id, slot, class_id, name, level, area, active_pet_id,
                           equipment, hotbar, explored, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, slot) DO UPDATE SET
            class_id      = excluded.class_id,
            name          = excluded.name,
            -- level is NOT taken from excluded. The row keeps the level the
            -- server granted through /api/combat/kill; the 1 in VALUES only
            -- ever applies to a brand new character.
            level         = saves.level,
            area          = excluded.area,
            active_pet_id = CASE WHEN ? THEN excluded.active_pet_id
                                 ELSE saves.active_pet_id END,
            equipment     = CASE WHEN ? THEN excluded.equipment
                                 ELSE saves.equipment END,
            hotbar        = CASE WHEN ? THEN excluded.hotbar
                                 ELSE saves.hotbar END,
            explored      = CASE WHEN ? THEN excluded.explored
                                 ELSE saves.explored END,
            updated_at    = excluded.updated_at
        """,
        (g.user["id"], slot, class_id, name, level, area, active_pet_id,
         json.dumps(equipment), json.dumps(hotbar or [""] * HOTBAR_SIZE),
         json.dumps(explored), now,
         1 if pet_key_sent else 0,
         equip_key_sent,
         1 if hotbar_key_sent else 0,
         1 if explored_key_sent else 0),
    )
    db.commit()

    # DERIVED STATS BELONG ON A NEW CHARACTER TOO, not only on the next status
    # write. Without this a freshly created warrior sat at the table's DEFAULT 10
    # hp until something happened to push a status - and if the client had been
    # trusted for max_hp, nobody would ever have noticed, because it would have
    # overwritten the 10 on its first save.
    #
    # Recomputed on every save rather than only on creation: max_hp is a pure
    # function of class and level, so there is no state where storing anything
    # else is correct.
    stored_level = db.execute(
        "SELECT level FROM saves WHERE user_id = ? AND slot = ?", (g.user["id"], slot)
    ).fetchone()["level"]
    derived = gamedata.max_stats_for(class_id, stored_level)

    if derived is not None:
        if is_new:
            # A new character starts with full pools. Only on creation - doing it
            # on every save would refill the player's health for free.
            db.execute(
                """
                UPDATE saves
                   SET max_hp = ?, hp = ?, max_mana = ?, mana = ?,
                       max_stamina = ?, stamina = ?
                 WHERE user_id = ? AND slot = ?
                """,
                (derived["max_hp"], derived["max_hp"],
                 derived["max_mana"], derived["max_mana"],
                 derived["max_stamina"], derived["max_stamina"],
                 g.user["id"], slot),
            )
        else:
            db.execute(
                "UPDATE saves SET max_hp = ?, max_mana = ?, max_stamina = ? WHERE user_id = ? AND slot = ?",
                (derived["max_hp"], derived["max_mana"], derived["max_stamina"],
                 g.user["id"], slot),
            )
        db.commit()

    # active_pet_id is echoed back only when the caller actually set it, so a
    # client can tell "you stored this" apart from "we left yours alone".
    result = {"slot": slot, "updated_at": now}
    if pet_key_sent:
        result["active_pet_id"] = active_pet_id

    # SAID OUT LOUD, the way gold is. A client that keeps sending `equipment`
    # is not doing anything wrong - it simply predates the endpoints - but a
    # field that is silently dropped is a client and a server disagreeing
    # forever with nothing to notice it by.
    if equipment_ignored:
        result["ignored"] = ["equipment"]
        result["equipment"] = _stored_json(
            get_db().execute(
                "SELECT equipment FROM saves WHERE user_id = ? AND slot = ?",
                (g.user["id"], slot)).fetchone()["equipment"], {})
    return result, 200


# =============================================================================
# PLAYER STATUS  -  GET /api/player/status
# =============================================================================

@app.get("/api/player/status")
@require_auth
def player_status():
    """
    Live stat values for the HUD
    ---
    tags:
      - Player
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: query
        name: slot
        type: integer
        required: true
        description: Character slot, 0-3
    responses:
      200:
        description: Current stats for that slot
      400:
        description: Missing or out-of-range slot
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    slot = parse_slot(request.args.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    row = get_db().execute(
        "SELECT * FROM saves WHERE user_id = ? AND slot = ?",
        (g.user["id"], slot),
    ).fetchone()

    # 404 is the right answer for an empty slot: the request was valid, the
    # resource does not exist. The client distinguishes this from a transport
    # failure and shows "no character here" instead of "server is down".
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    return {
        "slot": row["slot"],
        "level": row["level"],
        "hp": row["hp"], "max_hp": row["max_hp"],
        "mana": row["mana"], "max_mana": row["max_mana"],
        "stamina": row["stamina"], "max_stamina": row["max_stamina"],
        "gold": row["gold"],
        "xp": row["xp"], "xp_to_next": row["xp_to_next"],
    }, 200


# =============================================================================
# PLAYER STATUS - WRITE
# =============================================================================

# Every stat the client is allowed to push, and the ceiling each one is
# checked against. The client proposes; the server decides. A value that
# cannot legally exist is refused rather than clamped, because clamping
# hides the bug that produced it.
STATUS_FIELDS = {
    "level":       None,        # no paired maximum
    "hp":          "max_hp",
    "mana":        "max_mana",
    "stamina":     "max_stamina",
    "gold":        None,
    "xp":          None,
    "xp_to_next":  None,
    "max_hp":      None,
    "max_mana":    None,
    "max_stamina": None,
}

# nothing in this game legitimately exceeds these. a number past one of them
# means a corrupted save, an overflow, or someone editing packets - all three
# are worth refusing rather than storing.
STAT_CEILING = 1_000_000_000

# FIELDS THE SERVER OWNS. A client may send them; the server ignores the values
# and keeps its own.
#
# These three are granted by /api/combat/kill, which applies the level-up loop
# itself and commits the result. The server therefore already knows what they
# should be, and an assertion from the client is not evidence of anything.
#
# THIS IS THE HALF THAT MAKES THE OTHER HALF MEAN SOMETHING. Moving the loot
# roll to the server closed the "give myself a pet" hole, but while
# /api/player/status accepted a level, a modified client could simply declare
# itself level 60 and skip the game entirely. It could not do that until there
# was a server-side record to contradict it - which is why this change had to
# come second, not first.
#
# NOT IN THIS LIST, and honestly so:
#
#   gold          arrives by picking up a loot bag, and the pickup is still
#                 client-side. The server rolled what was IN the bag but does
#                 not know the player walked over it. Closing that needs the
#                 bag itself to be server-owned - a bag id, and an endpoint
#                 that transfers from it. That is the next piece.
#   hp/mana/      the result of combat and regen the server does not simulate.
#   stamina       Clamped against their maxima below, which is all the server
#                 can honestly say about them.
#   hp/mana/      the result of combat and regen the server does not simulate.
#   stamina       Clamped against their maxima below, which is all the server
#                 can honestly say about them.
# The two item ids that resolve into carried gold rather than a backpack cell.
# Read from gamedata so the authored source stays baseenemy.gd's constants.
CONSTANTS_GOLD_SMALL = gamedata.CONSTANTS.get("gold_small_id", "smallamountofgold")
CONSTANTS_GOLD_LARGE = gamedata.CONSTANTS.get("gold_large_id", "largeamountofgold")

# GOLD IS IN THIS LIST AND ITS ABSENCE WAS A HOLE STRAIGHT THROUGH THE ECONOMY.
#
# PUT /api/save has always refused a client's gold. PUT /api/player/status never
# did - `gold` is one of STATUS_FIELDS and was not one of these, so a single
# request set any balance an authenticated player liked:
#
#     PUT /api/player/status  {"slot": 0, "gold": 1000000}   -> 200
#
# and the invariant the whole gold_ledger exists to hold up
#
#     SUM(gold_ledger.delta) == SUM(saves.gold) + SUM(accounts.bank_gold)
#
# broke on that one call: nothing was minted through gold_delta(), so the left
# side stayed at 0 while the right side went to a million. The vendor sink, the
# kingdom tax and every trade valuation sit on top of that equation.
#
# NOTHING HONEST NEEDS TO PUSH A GOLD FIGURE, which is why this is safe to close
# rather than shadow-log first. /api/loot/take resolves a CURRENCY drop straight
# into a balance through gold_delta() and calls itself the only place gold is
# created; shop purchases, bank moves and trades all write their own rows. The
# one client path that added gold locally was inventoryscreen.gd's gold-pile
# handler, and a gold pile cannot reach a backpack in the first place - CURRENCY
# is in EXCLUDED_FROM_LOOT.
#
# Ignored rather than refused, like every other owned field: an honest client
# sends its whole status block and has no way to know which fields the server
# has taken over. It is named in `ignored` so the client can see it is being
# corrected rather than wonder why its gold did not stick.
SERVER_OWNED_STATS = ("level", "xp", "xp_to_next", "gold")

# DERIVED, not merely owned. These are not stored from a request at all - they
# are recomputed from the character's class and level every time the status is
# written, using the same curve the client runs:
#
#     max_hp = hp_base + (level - 1) * hp_per_lvl
#
# That curve used to be four literals inside warrior.gd's _set_stat_curve(), so
# the server knew your level AND your class and still could not work out your
# maximum health. ClassData moved it into data/classes/*.tres and the exporter
# carries it here - the same route EnemyData took.
DERIVED_STATS = ("max_hp", "max_mana", "max_stamina")


# =============================================================================
# UNEXPLAINED HEALING — SHADOW MODE
# =============================================================================
#
# THE FINDING THIS IS THE FIRST HALF OF. PUT /api/player/status accepts any hp
# the client sends, up to the maximum the server derives. That ceiling is real
# and it is not the interesting part: below it, a patched client heals to full
# whenever it likes, never dies, and never needs a potion. Nothing recorded it,
# so like the kill event before kill_reports, the fraud was not merely
# unpunished - it was invisible.
#
# It could not be checked before, because there was no legitimate source of
# healing for the server to compare against. /api/character/consume is that
# source. This is what it was for.
#
# IT REFUSES NOTHING, AND THAT IS THE POINT OF THIS PASS. E-1 shipped exactly
# this way - _report_unexplained_gains() logged the difference for weeks before
# anything was trimmed - and the reason is written into that function: shipping
# the refusal before the comparison had proven itself would have broken honest
# saves for real players. A threshold picked without data is how honest players
# get clamped, and the interim skill bound under E-2 was that mistake already.
#
# WHAT AN HONEST RISE LOOKS LIKE, and why a naive check would flag every player
# in the game: player.gd regenerates ALL THREE stats continuously, at
# `regen_percent_per_second` of each stat's own maximum with a floor of
# `regen_minimum_per_second`. A character sitting still returns to full in
# roughly a minute. So the allowance below is regen over the elapsed time, and
# a rise inside it is not merely tolerated - it is the normal case.
#
# THE RATES COME FROM THE GAME NOW, not from two literals retyped here.
#
# They are PlayerStats.REGEN_PERCENT_PER_SECOND and REGEN_MINIMUM_PER_SECOND,
# carried across by exportgamedata.gd, read through gamedata.regen_rate_for().
# They used to be copies sitting in this file, which was the first thing that
# had to change before this check could ever refuse anything: a copied constant
# in this project has form - the XP formula lived in two places, they drifted,
# and the sanitiser began rewriting honest saves with garbage.
#
# gamedata falls back to the old literals when gamedata.json predates the
# export, so an un-regenerated file still runs. _warn_if_protections_unarmed()
# says so once at boot rather than letting it pass unnoticed, because measuring
# against numbers nobody is keeping in step is the exact failure this move was
# meant to end.

# Slack over the computed allowance, for clock skew between the client's frame
# timer and the server's clock, and for the rounding player.gd does with its
# fractional accumulators.
HEAL_ALLOWANCE_MARGIN = 1.25

# THE ENFORCED LINE, as a multiple of the same allowance. See _reconcile_heals()
# for why there are two and why this one is deliberately loose.
#
# TIGHTEN THIS, DO NOT WIDEN IT. It starts generous because the evidence for the
# tight line is eighteen simulated scenarios rather than real traffic, and the
# log records every rise that falls between the two so the decision to close the
# gap can be made from data. A band that stays empty across real play is the
# argument for lowering this to 1.25; a band that fills with honest players is
# the proof that shipping the tight clamp would have broken them.
HEAL_CLAMP_MARGIN = 3.00

# ENFORCEMENT IS OFF, AND THIS IS WHY.
#
# It was on for about an hour. The first real save it saw was an honest one and
# it took 370 hp and 114 stamina off a real character:
#
#     unexplained heal: hp +480 vs regen 110 + granted 0  (elapsed 11.0s)
#     heal clamped:     hp 480 -> 110   stamina 205 -> 91
#
# At 4.4x the allowance it was not close to the 3x line - it was nowhere near
# it, which is the part worth keeping. The argument for enforcing at 3x was
# that three times more healing than the game can produce cannot plausibly be
# honest. That argument was wrong, and it was wrong by a factor rather than at
# the margin.
#
# WHAT PRODUCED IT, and it is not a cheat or a bug: player.gd::_ready() reads
#
#     hp = max_hp if (was_full_hp or hp <= 0) else clampi(hp, 0, max_hp)
#
# A character stored at 0 stands up at full, deliberately and with a comment
# explaining that the alternative is spawning a corpse that dies on its first
# frame. The death penalty is carried by lost gold and the lusion cost of a
# revive, not by refusing to let the player stand. Nothing about that reaches
# the server: no /api/character/revive call, no consume_grants row, nothing for
# the reconciler to find.
#
# SO THE FIX IS NOT A WIDER MARGIN. A margin wide enough to admit a
# zero-to-full refill is a margin wide enough to admit any heal at all, which
# is not a control. The fix is that the client's one remaining legitimate
# refill has to leave a record, the way the level-up and the revive already do.
# Until it does, this measures and says so and changes nothing.
#
# THE DISCIPLINE THIS RESTORES is the one SECURITY_NOTES already had written
# down under E-1: log first, refuse second, and let the log decide. The band
# between the two margins is still recording, so when the refill is accounted
# for there will be real evidence rather than another argument from plausibility.
#
# ----------------------------------------------------------------------------
# BACK ON, AND THIS TIME FOR A REASON RATHER THAN AN ARGUMENT.
#
# The refill above is gone. player.gd::_ready() no longer stands a character
# stored at 0 up at full - it routes to the game over screen, which is where
# the death penalty lives, and closes the separate hole where logging out at 0
# hp returned the character alive with every item. So the one confirmed false
# positive no longer exists.
#
# The client's remaining full-heal paths were enumerated rather than assumed,
# which is what was missing the first time:
#
#   the `level` setter        fires on load, then SAVEABLE_STATS writes the
#                             stored hp over it - net zero
#   level_up()                /api/combat/kill writes a LEVELUP grant in the
#                             same transaction, so it is already explained
#   has_active_revive         declared, read once, and set to true by NOTHING
#                             in the project - the branch cannot run
#   hp <= 0 on load           removed
#
# That leaves no refill the server does not already know about. Not a proof -
# an enumeration of a client this size is only as good as the reading - but it
# is evidence of a kind the first attempt did not have.
#
# WHAT WOULD SEND THIS BACK TO False: an `unexplained heal` line for a player
# who was playing honestly. That is the only signal that matters, and the log
# is still the thing that produces it.
HEAL_CLAMP_ENFORCED = True

# Two writes in the same second must not read as "healed with zero time
# available". Treated as at least this many seconds apart.
HEAL_MINIMUM_ELAPSED_SECONDS = 2.0

# How recently a consume has to have happened to count toward a rise.
#
# STILL GENEROUS, but it no longer has to carry the whole check on its own. It
# used to be the ONLY thing standing between a potion and an arbitrary heal -
# any potion inside the window explained any rise, because the server did not
# know what a potion restored. Now the amount is known, so the window only has
# to be wide enough to cover a client that drinks and syncs a moment later.
HEAL_EXPLAIN_WINDOW_SECONDS = 30

# What a revive writes into consume_grants. Not a real item_id, and it must not
# collide with one - `gamedata.has_item()` is the guard if that ever needs
# proving. A revive explains a rise for the same reason a potion does, so it
# lives in the same table rather than in a second one somebody forgets to read.
REVIVE_GRANT_ID = "__revive__"

# And the same for a level-up. player.gd::level_up() calls
# _fill_all_resources(), so every level legitimately fills all three pools
# at once - the one remaining client-side refill that is supposed to happen.
# The server is the thing that decides a level-up (it owns level and grants
# the XP at the kill), so it is also the thing that can say so.
#
# WITHOUT THIS the check would flag every level a player ever gains, which is
# the surest way to make a log nobody reads.
LEVELUP_GRANT_ID = "__levelup__"

# A revive fills every pool, so it explains any rise in any of them. Recorded
# with an empty target and a zero amount, and read here rather than stored as
# three rows of unknown size.
REVIVE_EXPLAINS_EVERYTHING = True


# PROTECTIONS THAT ARE ONLY AS REAL AS THE CATALOGUE THEY READ.
#
# Every one of these FAILS OPEN when its field is missing, and that is the
# right call: a server whose gamedata.json predates a field must keep serving
# rather than refuse every request. See the spawn ceiling's note for the full
# argument.
#
# WHAT WAS WRONG WAS THAT IT WAS QUIET. gamedata.json in this repo once sat 46
# hours behind the Godot export and all four of these were open the whole time
# - equipment validation, the spawn ceiling, the regen rates and the potion
# amounts, none of them doing anything, with 1,515 tests passing because every
# suite builds its own fixture and none of them read the file that ships.
#
# TWO THINGS CHANGED, and the other one matters more. test_catalogue.py asserts
# these flags against the REAL gamedata.json, so a stale copy is now a red test
# rather than a silence. This block is the production half: CI cannot see the
# file on the deployed box.
#
# A TABLE, NOT FOUR FUNCTIONS. There were two of these written out longhand and
# the two that arrived later never got written at all - which is the whole
# incident in one sentence. Adding a protection means adding a row here, and a
# row is small enough that it actually happens.
EXPORT_DEPENDENT_PROTECTIONS = (
    (
        "equip_slot_name",
        lambda: gamedata.EQUIP_EXPORTED,
        "equipment saves fall back to EQUIP_SLOTS_FALLBACK and nothing "
        "verifies an item belongs in the slot it was sent for - the state "
        "that let {\"helm\": \"embersword\"} through",
    ),
    (
        "placed_count",
        lambda: gamedata.SPAWNS_EXPORTED,
        "the spawn ceiling is disabled entirely - kill claims are bounded "
        "only by the token bucket",
    ),
    (
        "regen_percent_per_second",
        lambda: gamedata.REGEN_EXPORTED,
        "the healing check measures against app.py's fallback literals "
        "rather than against PlayerStats",
    ),
    (
        "restore_amount",
        lambda: any("restore_amount" in item for item in gamedata.ITEMS.values()),
        "a consume grant carries no amount, and an unknown amount makes "
        "_reconcile_heals() return early - one cheap potion "
        "explains a heal of any size",
    ),
)


def _warn_if_protections_unarmed():
    """Say once, at boot, which export-gated protections are not running.

    ONE BLOCK, NOT ONE LINE EACH. Four separate warnings scroll; a block with a
    count in its first line reads as an event. Same reasoning as collapsing the
    export tool's per-boss warnings into a single line.

    ERROR, NOT WARNING. A warning is what this was, and a warning is what got
    scrolled past for 46 hours. Nothing here is a style preference or a
    deprecation - these lines mean a security control that the code believes is
    running is not running.
    """
    unarmed = [(field, why) for field, armed, why in EXPORT_DEPENDENT_PROTECTIONS
               if not armed()]
    if not unarmed:
        return

    app.logger.error(
        "%d of %d export-gated protections are NOT ARMED - gamedata.json is "
        "stale or incomplete:",
        len(unarmed), len(EXPORT_DEPENDENT_PROTECTIONS),
    )
    for field, why in unarmed:
        app.logger.error("    missing %-26s %s", field, why)
    app.logger.error(
        "    FIX: re-run src/tools/exportgamedata.gd in Godot, then copy "
        "Elusion_RPG/data/gamedata.json over this folder's gamedata.json"
    )

    # REFUSING TO BOOT IS THE OTHER OPTION, and it is deliberately not the
    # default. Uncomment to take it:
    #
    #     raise gamedata.GameDataError(
    #         "refusing to serve with %d protections unarmed" % len(unarmed))
    #
    # The argument for it is that a game with real accounts should not quietly
    # serve with equipment validation off. The argument against is that it
    # turns a missed copy into an outage at the worst possible moment, and the
    # honest state today is that nobody is watching the log at 3am either way.
    # Left as a one-line flip rather than decided here, because it is a
    # deployment posture question and not a code question.


# AT IMPORT, which is boot for this app - init_db() runs the same way a few
# hundred lines up. Said once rather than per request, because a warning that
# repeats on every status write is one nobody reads.
_warn_if_protections_unarmed()


def _reconcile_heals(db, user_id, slot, before, after):
    """
    Trim any rise in hp/mana/stamina that regeneration and authorised
    consumables cannot account for. Returns {field: corrected} for the caller
    to apply, empty when there is nothing to correct.

    RENAMED FROM _report_unexplained_heals, because it no longer only reports.
    It sits beside _reconcile_bank() and _reconcile_inventory() now, does the
    same job as those two - compare the claim to the record and correct it -
    and is named for it.

    TWO LINES, NOT ONE, AND THAT IS THE WHOLE DESIGN.

        HEAL_ALLOWANCE_MARGIN   1.25x   the TIGHT line: what regen plus
                                        consumes can honestly produce.
                                        Logged, never enforced.
        HEAL_CLAMP_MARGIN       3.00x   the LOOSE line: enforced.

    Enforcing at 1.25x on the evidence available would be the E-2 mistake - a
    threshold picked from eighteen simulated scenarios rather than from real
    traffic. Enforcing at 3x cannot plausibly catch an honest player: it is
    three times more healing than the game can produce, and a rise that large
    did not come from the game.

    Meanwhile every rise landing BETWEEN the two lines is logged as
    "tight-clamp would have caught this". That is the evidence for closing the
    gap later, gathered while a control is already running rather than instead
    of one. If that band stays empty across real play, 1.25x is safe and the
    log is what says so. If it fills up with honest players, the band is the
    proof that shipping the tight clamp would have broken them.

    CLAMPS, DOES NOT REFUSE. A 400 here fails the whole save, and the save
    carries XP, gold, position and inventory - punishing a suspicious hp figure
    by discarding a legitimate half-hour of play is a worse bug than the cheat.
    Trimmed to what the player could have earned, exactly as
    _reconcile_inventory() trims a bag, and the write proceeds.

    WHAT IT STILL DOES NOT DO. It bounds the RATE of unexplained healing, not
    its existence: measured against a 180 hp pool, roughly 7 hp per save slips
    under the tight line and ~14 full heals an hour under it. That is the same
    shape as the kill bucket, and the reason both are controls rather than
    proofs is the same - the server does not observe combat. See E-3.

    THE CHECK IS PER-POOL NOW, not per-character. The first version asked "did
    this character drink anything recently", which meant one cheap stamina
    potion explained an arbitrary jump in health - a hole exactly as wide as the
    smallest consumable in the game. It asked that because the server did not
    know what a potion restored. It does now, so a grant explains a rise in the
    pool it actually fills, up to the amount it actually gives.

    KNOWN SOURCE OF FALSE POSITIVES, named so whoever reads the log is not
    misled: **a level-up** raises the derived maximum in the same write. The
    rise in the CURRENT stat still has to come from somewhere, so this only
    misfires if the client refills on level-up as well.

    (Revive used to be the big one and no longer is - it goes through
    /api/character/revive and leaves a grant row of its own.)

    Anything that survives all of that is a client claiming health it did not
    earn.
    """
    elapsed = max(
        float(int(time.time()) - int(before["updated_at"] or 0)),
        HEAL_MINIMUM_ELAPSED_SECONDS,
    )

    risen = {}
    for field, cap_field in (("hp", "max_hp"), ("mana", "max_mana"), ("stamina", "max_stamina")):
        gained = int(after[field]) - int(before[field] or 0)
        if gained <= 0:
            continue

        ceiling = max(int(after[cap_field]), 1)

        # Capped at the headroom that existed, because regeneration cannot take
        # you past your own maximum however long you waited. Without this cap a
        # client that simply saves rarely is handed an unlimited allowance.
        #
        # ELAPSED IS THE WHOLE WINDOW, even though regen only runs after
        # REGEN_IDLE_THRESHOLD seconds of standing still and any action resets
        # it. The server cannot see movement, so it assumes the most generous
        # case. That makes this an upper bound rather than a tight one, which
        # is the right direction to be wrong in for a check that must never
        # fire on honest play.
        allowance = min(
            elapsed * gamedata.regen_rate_for(ceiling) * HEAL_ALLOWANCE_MARGIN,
            float(ceiling - int(before[field] or 0)),
        )

        if gained > allowance:
            risen[field] = (gained, allowance)

    if not risen:
        return {}

    # WHAT THE SERVER AUTHORISED IN THE WINDOW, by pool.
    since = int(time.time()) - HEAL_EXPLAIN_WINDOW_SECONDS
    granted = {"hp": 0.0, "mana": 0.0, "stamina": 0.0}
    for row in db.execute(
        "SELECT item_id, target, amount FROM consume_grants "
        "WHERE user_id = ? AND slot = ? AND at >= ?",
        (user_id, slot, since),
    ):
        if row["item_id"] in (REVIVE_GRANT_ID, LEVELUP_GRANT_ID):
            # Both fill every pool to its maximum, so either explains any rise
            # in any of them. They are special cases rather than large amounts
            # because "as much as you had room for" is not a number the server
            # can write down in advance.
            return {}
        target = str(row["target"] or "").lower()
        if target in granted:
            granted[target] += float(row["amount"] or 0)
        elif row["amount"] == 0 and target == "":
            # A grant written before the target/amount columns existed, or a
            # potion from a gamedata.json that predates the export. Unknown
            # rather than zero: it explains nothing on its own, but it must not
            # be reported as a cheat either, because the server genuinely does
            # not know what it did.
            return {}

    still_unexplained = {
        field: (gained, allowance)
        for field, (gained, allowance) in risen.items()
        if gained > allowance + granted.get(field, 0.0)
    }
    if not still_unexplained:
        return {}

    # THE LINE SAYS WHAT WAS ALLOWED AND WHAT WAS GRANTED, per pool, because
    # "unexplained heal" on its own is not something anyone can act on. The old
    # message ended "(no consume in 30s)", which stopped being true the moment
    # a grant could be partial - a potion may well have been drunk; it just did
    # not cover the jump.
    app.logger.warning(
        "unexplained heal: user=%s slot=%s elapsed=%.1fs  %s  (window %ds)",
        user_id, slot, elapsed,
        "  ".join(
            "%s +%d vs regen %.0f + granted %.0f" % (field, gained, allowance, granted.get(field, 0.0))
            for field, (gained, allowance) in sorted(still_unexplained.items())
        ),
        HEAL_EXPLAIN_WINDOW_SECONDS,
    )

    # THE ENFORCED LINE. Everything above this point is the 1.25x measurement
    # and is unchanged; this is the only part that acts.
    #
    # The ratio is taken against the TIGHT allowance, so widening the enforced
    # margin never widens what gets logged. The two numbers stay independent on
    # purpose: one is the honest estimate, the other is how much benefit of the
    # doubt is currently being given, and conflating them is how a temporary
    # margin quietly becomes the definition of honest.
    if not HEAL_CLAMP_ENFORCED:
        # Measured and reported above; nothing is corrected. See the constant.
        return {}

    corrections = {}
    for field, (gained, allowance) in still_unexplained.items():
        ceiling = allowance + granted.get(field, 0.0)
        if gained <= ceiling * (HEAL_CLAMP_MARGIN / HEAL_ALLOWANCE_MARGIN):
            # Between the lines. Logged above, deliberately not trimmed - this
            # is the band that decides whether the tight margin is safe.
            continue
        corrections[field] = int(before[field] or 0) + int(ceiling)

    if corrections:
        app.logger.warning(
            "heal clamped: user=%s slot=%s  %s",
            user_id, slot,
            "  ".join("%s %s -> %d" % (field, after[field], value)
                      for field, value in sorted(corrections.items())),
        )
    return corrections


def parse_stat(raw):
    """Return a non-negative int, or None if the value isn't one."""
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > STAT_CEILING:
        return None
    return value


@app.put("/api/player/status")
@require_auth
def write_player_status():
    """
    Push current stat values for one character slot
    ---
    tags:
      - Player
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot]
          properties:
            slot:        {type: integer, example: 0}
            level:       {type: integer, example: 12}
            hp:          {type: integer, example: 88}
            max_hp:      {type: integer, example: 120}
            mana:        {type: integer, example: 30}
            max_mana:    {type: integer, example: 60}
            stamina:     {type: integer, example: 45}
            max_stamina: {type: integer, example: 50}
            gold:        {type: integer, example: 1450}
            xp:          {type: integer, example: 15320}
            xp_to_next:  {type: integer, example: 2100}
    responses:
      200:
        description: The full status after the write
      400:
        description: A field was not a non-negative integer, or hp exceeded max_hp
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    row = get_db().execute(
        "SELECT * FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    # PARTIAL UPDATE: only fields actually present are touched. A client that
    # knows nothing about stamina can still push hp without silently zeroing
    # everything it didn't mention.
    # RECOMPUTED BEFORE ANYTHING IS VALIDATED, so that hp is checked against the
    # maximum the server believes in rather than the one the client sent. A
    # client declaring max_hp = 999999 alongside hp = 999999 would otherwise pass
    # the paired check below on its own say-so.
    derived = gamedata.max_stats_for(row["class_id"], row["level"])

    updates = {}
    ignored = []
    for field in STATUS_FIELDS:
        if field not in payload:
            continue

        # Derived fields are dropped from the request for the same reason as
        # owned ones, but they are then written back from the curve - see below.
        if derived is not None and field in DERIVED_STATS:
            ignored.append(field)
            continue

        # IGNORED, NOT REFUSED. A 400 here would break every honest save: the
        # client sends its whole status block and has no way to know which
        # fields the server has taken ownership of. Dropping them silently and
        # reporting which ones were dropped lets an honest client carry on and
        # gives a dishonest one nothing.
        if field in SERVER_OWNED_STATS:
            ignored.append(field)
            continue

        value = parse_stat(payload[field])
        if value is None:
            return bad_request(
                "%s must be a non-negative integer no greater than %d" % (field, STAT_CEILING)
            )
        updates[field] = value

    if not updates and not ignored:
        return bad_request("no writable fields supplied")

    # Validate against the state AFTER the merge, not against what was sent.
    # Pushing hp=120 alone is illegal if stored max_hp is 100, but legal in the
    # same request that raises max_hp to 120 - and order of keys in JSON must
    # not decide which.
    merged = {field: row[field] for field in STATUS_FIELDS}
    merged.update(updates)

    if derived is not None:
        # The server's own numbers go in AFTER the client's, so they win, and
        # they are added to `updates` so they are actually written - a character
        # that levelled up needs its new maximum stored, not just enforced.
        merged.update(derived)
        updates.update(derived)

    for field, cap_field in STATUS_FIELDS.items():
        if cap_field is None:
            continue
        if merged[field] > merged[cap_field]:
            # CLAMPED, not refused, when the ceiling is one the server derived.
            #
            # A character at full health who levels DOWN - or whose class curve
            # is retuned downward between releases - legitimately arrives with
            # hp above the new max_hp. Refusing would make that character
            # unsaveable. Clamping is the honest reading: you have as much health
            # as the curve allows.
            if cap_field in DERIVED_STATS and derived is not None:
                merged[field] = merged[cap_field]
                updates[field] = merged[cap_field]
                continue
            return bad_request(
                "%s (%d) cannot exceed %s (%d)" % (field, merged[field], cap_field, merged[cap_field])
            )

    db = get_db()
    if updates:
        # BEFORE THE WRITE, because it compares the stored row to the merged
        # one and the stored row is about to stop existing.
        #
        # IT CORRECTS RATHER THAN REFUSING, and the corrections go into BOTH
        # `merged` and `updates`: merged is what the rest of this function
        # reasons about, updates is what actually reaches the UPDATE statement.
        # Writing only one of them is how a clamp becomes decorative - the log
        # would say "clamped" and the client's figure would be stored anyway.
        healed = _reconcile_heals(db, user_id, slot, row, merged)
        for field, value in healed.items():
            merged[field] = value
            updates[field] = value

        assignments = ", ".join("%s = ?" % f for f in updates)
        values = list(updates.values()) + [int(time.time()), user_id, slot]
        db.execute(
            "UPDATE saves SET %s, updated_at = ? WHERE user_id = ? AND slot = ?" % assignments,
            values,
        )
        db.commit()

    result = status_payload(user_id, slot)
    if ignored:
        # The server's own values are already in `result`; this just names what
        # was disregarded, so a client can see it is being corrected rather than
        # wondering why its level did not stick.
        result["ignored"] = ignored
    return result, 200


def status_payload(user_id, slot):
    row = get_db().execute(
        "SELECT * FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()
    if row is None:
        return None
    return {
        "slot": row["slot"],
        "level": row["level"],
        "hp": row["hp"], "max_hp": row["max_hp"],
        "mana": row["mana"], "max_mana": row["max_mana"],
        "stamina": row["stamina"], "max_stamina": row["max_stamina"],
        "gold": row["gold"],
        "xp": row["xp"], "xp_to_next": row["xp_to_next"],
    }


# =============================================================================
# THE BANK AND THE ACCOUNT
# =============================================================================
#
# REWRITTEN. The bank was modelled per-CHARACTER and it is account-shared.
#
# characterdata.gd is unambiguous about this - bank_gold and bank_inventory
# live in account_data, commented "account-shared, safe from death", and that
# is the whole point of the feature: carry gold and carry items are lost when
# you die, so the bank is where you put things you do not want to lose, and it
# is shared so a second character can use what the first one banked.
#
# The server had bank_items keyed on (user_id, slot, item_id) and bank_gold as
# a column on `saves`. Banking something on your warrior would have made it
# invisible to your mage, and dying would have felt like losing the bank too.
# That was not a bug anyone had hit, because nothing in the game talks to these
# endpoints yet - it was a bug waiting for the first client that did.
#
# Two other things move with it:
#
# LUSIONS did not exist on the server at all. They are the account-shared
# premium currency and the revive cost, and they were the one piece of player
# state with no column anywhere.
#
# THE BANK IS POSITIONAL, like the backpack and for the same reason: the client
# holds it as an array of BANK_CAPACITY cells with nulls for the empty ones, and
# the player expects things to stay where they put them. Keyed on item_id it was
# a set, which would silently merge two stacks and reshuffle the grid.
#
# The deposit/withdraw op endpoint was removed when this became positional, on
# the grounds that a grid client always holds the whole picture and a whole-array
# replace has no partial state to reconcile - while /api/bank/gold kept its op
# "because gold is the one thing here the server can actually verify: it holds
# both balances and can conserve the total".
#
# THAT LAST SENTENCE WAS THE ARGUMENT AGAINST ITSELF, and POST /api/bank/items
# now brings the op back. The server holds both sides of an item transfer too -
# carry_items and bank_items are both its rows - so it could always have
# conserved items exactly the way it conserves gold. It simply was not asked to.
# A whole-array replace cannot conserve anything: two arrays that do not add up
# are indistinguishable from two that do.
#
# The layout objection was real and is answered rather than ignored. The server
# decides which cell a deposited stack lands in, the same way _add_to_backpack()
# already does for loot, and the response carries BOTH grids. There is no partial
# state for the client to reconcile because the client is not deciding anything.
#
# PUT /api/account/bank still exists and still takes a whole array on trust. It
# goes when the client no longer needs it - see docs/inventoryauthority.md.

# Must match BANK_MAX_SLOTS in characterdata.gd. Exported into gamedata.json so
# there is one authored source; the fallback here only applies to an older
# gamedata that predates the key.
#
# THE ONLY DEFINITION. There used to be a second one, BANK_CAPACITY = 40, six
# hundred lines above this. Python took the later assignment, so the bank really
# held 50 and everything worked - right up until someone tidied away the
# "unused" one and the bank silently shrank by ten cells, at which point every
# save with an item in cells 40-49 starts failing validation.
BANK_CAPACITY = int(gamedata.CONSTANTS.get("bank_capacity", 50))


def _add_to_bank(user_id, item_id, quantity):
    """
    Put quantity of item_id into the bank and return the cells touched, or None
    when it does not fit.

    The bank's _add_to_backpack(). Same rules for the same reason: tops up an
    existing stack before opening a cell, and writes NOTHING unless the whole
    quantity fits. A half-completed deposit is the shape of bug that ends with
    an item in neither the bag nor the bank.
    """
    definition = gamedata.ITEMS.get(item_id, {})
    stackable = bool(definition.get("stackable", False))
    max_stack = int(definition.get("max_stack", 1)) if stackable else 1
    if max_stack < 1:
        max_stack = 1

    rows = get_db().execute(
        "SELECT position, item_id, quantity FROM bank_items WHERE user_id = ? ORDER BY position",
        (user_id,),
    ).fetchall()
    occupied = {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in rows}

    remaining = int(quantity)
    writes = []

    if stackable:
        for position in sorted(occupied):
            if remaining <= 0:
                break
            held_id, held_qty = occupied[position]
            if held_id != item_id or held_qty >= max_stack:
                continue
            room = max_stack - held_qty
            moved = min(room, remaining)
            writes.append((position, item_id, held_qty + moved))
            remaining -= moved

    for position in range(BANK_CAPACITY):
        if remaining <= 0:
            break
        if position in occupied:
            continue
        moved = min(max_stack, remaining)
        writes.append((position, item_id, moved))
        occupied[position] = (item_id, moved)
        remaining -= moved

    if remaining > 0:
        return None

    db = get_db()
    for position, written_id, written_qty in writes:
        db.execute(
            """
            INSERT INTO bank_items (user_id, position, item_id, quantity)
                 VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, position)
              DO UPDATE SET item_id = excluded.item_id, quantity = excluded.quantity
            """,
            (user_id, position, written_id, written_qty),
        )
    return [position for position, _, _ in writes]


def _take_from_cells(rows, item_id, quantity):
    """
    Work out which cells to empty or reduce to remove quantity of item_id.

    Returns (list of (position, new_quantity_or_zero), None) or (None, how many
    were actually available) when there are not enough. Shared by the backpack
    and the bank because "take 5 potions out of a positional grid" is one rule,
    and the last time this project had that rule twice the two copies stopped
    agreeing about the stack ceiling.

    HIGHEST POSITION FIRST. Emptying the last cell of a split stack leaves the
    player's grid looking like they expect - things disappear from the end, not
    out of the middle.
    """
    held = [(int(r["position"]), int(r["quantity"]))
            for r in rows if r["item_id"] == item_id]
    available = sum(q for _, q in held)
    if available < quantity:
        return None, available

    remaining = int(quantity)
    changes = []
    for position, held_qty in sorted(held, reverse=True):
        if remaining <= 0:
            break
        taken = min(held_qty, remaining)
        changes.append((position, held_qty - taken))
        remaining -= taken
    return changes, None


def _apply_cell_changes(table, key_columns, key_values, changes):
    """Write back what _take_from_cells worked out. Zero means delete the row."""
    db = get_db()
    where = " AND ".join("%s = ?" % column for column in key_columns)
    for position, new_quantity in changes:
        if new_quantity <= 0:
            db.execute(
                "DELETE FROM %s WHERE %s AND position = ?" % (table, where),
                (*key_values, position),
            )
        else:
            db.execute(
                "UPDATE %s SET quantity = ? WHERE %s AND position = ?" % (table, where),
                (new_quantity, *key_values, position),
            )


def lusion_delta(db, user_id, slot, delta, reason, detail=""):
    """
    THE ONLY WAY LUSIONS MAY CHANGE, and the twin of gold_delta().

    Same discipline: the caller is already inside a transaction and must not
    commit, so the balance and its row land together or not at all.

    Lusions are ACCOUNT-scoped, so the balance lives on accounts. `slot` is
    recorded anyway when there is one, because "which character died" is the
    interesting question about a revive and it costs one nullable column to be
    able to answer it later.

    Returns the new balance, or None when the account row is missing.
    """
    if delta == 0:
        return None

    db.execute(
        "UPDATE accounts SET lusions = lusions + ? WHERE user_id = ?",
        (int(delta), user_id),
    )
    db.execute(
        "INSERT INTO lusion_ledger (at, user_id, slot, delta, reason, detail)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (int(time.time()), user_id, slot, int(delta), str(reason), str(detail)[:120]),
    )
    row = db.execute(
        "SELECT lusions FROM accounts WHERE user_id = ?", (user_id,)
    ).fetchone()
    return int(row["lusions"]) if row is not None else None


def bank_gold_delta(db, user_id, delta, reason, detail=""):
    """
    gold_delta()'s twin, for the account-scoped pile.

    THE SAME DISCIPLINE AND THE SAME REASON. gold_delta() only ever touches
    saves.gold, because until reviving could be paid for in gold nothing
    destroyed anything out of the bank - deposits and withdrawals are transfers
    between two balances that both sit inside the supply sum, so neither writes
    a row. A BURN out of the bank is different: it takes gold out of the world
    and has to be recorded, or

        SUM(gold_ledger.delta) == SUM(saves.gold) + SUM(accounts.bank_gold)

    stops holding on the next revive somebody pays for.

    slot IS NULL on the row, deliberately. The bank belongs to the account, not
    to a character, and writing a slot on it would invite a later query to
    group bank burns by character and quietly get a different answer.
    """
    if delta == 0:
        return None

    # Guarded exactly like gold_delta: a bank burn that would go below zero
    # matches no row and is refused atomically; a mint always clears the guard.
    cur = db.execute(
        "UPDATE accounts SET bank_gold = bank_gold + ?"
        " WHERE user_id = ? AND bank_gold + ? >= 0",
        (int(delta), user_id, int(delta)),
    )
    if cur.rowcount == 0:
        exists = db.execute(
            "SELECT 1 FROM accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
        if exists is None:
            return None
        raise InsufficientGold(
            "bank debit of %d would take user %s below zero" % (delta, user_id))

    db.execute(
        "INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
        " VALUES (?, ?, NULL, ?, ?, ?)",
        (int(time.time()), user_id, int(delta), str(reason), str(detail)[:120]),
    )
    row = db.execute(
        "SELECT bank_gold FROM accounts WHERE user_id = ?", (user_id,)
    ).fetchone()
    return int(row["bank_gold"]) if row is not None else None


def _ensure_account(user_id):
    """
    Every user gets an accounts row on first touch.

    Created lazily rather than at registration so existing accounts - which
    registered before this table existed - get one the first time they bank
    anything, with no migration step and no backfill to remember to run.
    """
    db = get_db()
    db.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (user_id,))
    return db.execute(
        # score IS IN THE SELECT, and leaving it out cost a test run. A
        # sqlite3.Row raises IndexError for a column that was not fetched, so
        # account_payload() guards with .keys() - and that guard turned a
        # missing column into a silent 0 rather than a crash, which is the
        # worse of the two failures. The guard stays; this is what makes it
        # never fire.
        "SELECT lusions, bank_gold, score FROM accounts WHERE user_id = ?", (user_id,)
    ).fetchone()


def account_payload(user_id):
    row = _ensure_account(user_id)
    rows = get_db().execute(
        "SELECT position, item_id, quantity FROM bank_items WHERE user_id = ? ORDER BY position",
        (user_id,),
    ).fetchall()

    cells = [None] * BANK_CAPACITY
    for item in rows:
        position = int(item["position"])
        if 0 <= position < BANK_CAPACITY:
            cells[position] = {"item_id": item["item_id"], "quantity": int(item["quantity"])}

    return {
        "lusions": int(row["lusions"]),
        "bank_gold": int(row["bank_gold"]),
        # KEYED WITH .get() RATHER THAN row["score"], because a sqlite3.Row
        # raises IndexError for a column the SELECT did not fetch, and
        # _ensure_account() predates this one. The narrow-SELECT trap is how a
        # correct refusal turned into a 500 once already - see E-11.
        "score": int(row["score"]) if "score" in row.keys() else 0,
        "bank_inventory": cells,
        "capacity": BANK_CAPACITY,
    }


@app.get("/api/account")
@require_auth
def read_account():
    """
    Account-shared state: lusions, bank gold, bank inventory
    ---
    tags:
      - Account
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200:
        description: Everything shared across this user's characters
      401:
        description: Missing, invalid or expired token
    """
    # NO slot parameter, deliberately. That absence is the fix.
    return account_payload(g.user["id"]), 200


@app.put("/api/account/bank")
@require_auth
def write_bank():
    """
    Replace the bank's contents
    ---
    tags:
      - Account
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [bank_inventory]
          properties:
            bank_inventory:
              type: array
              description: "Positional; null for an empty cell. At most 50 entries."
    responses:
      200:
        description: The account as stored
      400:
        description: Oversized array, or a malformed entry
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]
    _ensure_account(user_id)

    cells = payload.get("bank_inventory")
    if not isinstance(cells, list):
        return bad_request("bank_inventory must be an array")
    if len(cells) > BANK_CAPACITY:
        return bad_request("bank_inventory has %d entries, capacity is %d" % (len(cells), BANK_CAPACITY))

    parsed, error = _parse_positional_items(cells, "bank_inventory")
    if error is not None:
        return error

    # PROVENANCE, AFTER SHAPE. Everything above answers "is this a valid bank?"
    # and nothing above asks "did this player come by these items legitimately?"
    # - which is the distinction SECURITY_NOTES.md is built around, and the one
    # this endpoint used to miss entirely. See _reconcile_bank().
    #
    # ACCEPTED AND TRIMMED, NOT REFUSED. Same choice as the backpack: a 400 here
    # would break an honest client that is one item out of step for any innocent
    # reason, and a modified client learns nothing from a silent trim that it
    # would not learn faster from an error naming the field.
    parsed = _reconcile_bank(g.user, parsed)

    db = get_db()
    db.execute("DELETE FROM bank_items WHERE user_id = ?", (user_id,))
    if parsed:
        db.executemany(
            "INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, ?, ?, ?)",
            [(user_id, position, item_id, quantity) for position, item_id, quantity in parsed],
        )
    db.commit()

    return account_payload(user_id), 200


@app.put("/api/account/lusions")
@require_auth
def write_lusions():
    """
    Read the account's lusion balance (writes are ignored)
    ---
    tags:
      - Account
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: false
        schema:
          type: object
          properties:
            lusions: {type: integer, example: 40}
    responses:
      200:
        description: The account as the SERVER holds it; any lusions sent are ignored
      401:
        description: Missing, invalid or expired token
    """
    # THIS ENDPOINT USED TO STORE WHATEVER ARRIVED, and that was the finding.
    #
    #     lusions = parse_stat(payload.get("lusions"))
    #     UPDATE accounts SET lusions = ? WHERE user_id = ?
    #
    # An absolute figure, from the client, with no check beyond "is it a
    # non-negative integer". The same mistake as gold on the status endpoint
    # (E-8), one endpoint over.
    #
    # WHY IT MATTERED MORE THAN THE NUMBER SUGGESTS. Lusions have exactly one
    # sink in the game: reviving after death, at GameConstants.revive_cost a
    # go. So the balance IS the death penalty, and a client that could write it
    # could make dying free - which it did, because gameover.gd deducted the
    # cost locally and restored the health itself too.
    #
    # NOTHING HONEST NEEDS TO WRITE THIS ANY MORE. Lusions are created by the
    # server when a duplicate pet converts (see /api/loot/take) and spent by
    # the server at /api/character/revive. Both ends are now inside the
    # building.
    #
    # KEPT RATHER THAN DELETED, and answering 200. An un-updated client still
    # PUTs its lusion figure on a routine sync, and a 404 or a 400 would turn
    # an honest save into an error the player sees. It reads back the server's
    # own number instead, which is also how the client learns it was corrected.
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]
    _ensure_account(user_id)

    body = account_payload(user_id)
    if "lusions" in payload:
        body["ignored"] = ["lusions"]
    return body, 200


@app.post("/api/bank/items")
@require_auth
def move_bank_items():
    """
    Move an item between a character's backpack and the shared bank
    ---
    tags:
      - Account
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, op, item_id]
          properties:
            slot:     {type: integer, example: 0}
            op:       {type: string,  enum: [deposit, withdraw]}
            item_id:  {type: string,  example: "tinyhealthpotion"}
            quantity: {type: integer, example: 5}
    responses:
      200:
        description: Both grids afterwards, plus the account
      400:
        description: Bad slot, op, item id or quantity, or not enough on the source side
      404:
        description: That slot is empty
      409:
        description: The destination is full
      401:
        description: Missing, invalid or expired token
    """
    # CONSERVES THE TOTAL, which a whole-array replace cannot.
    #
    # The server holds both sides of this - carry_items and bank_items are both
    # its rows - so a transfer is the one shape where it can check that what left
    # one side is exactly what arrived at the other. PUT /api/account/bank takes
    # two arrays on trust and has no way to tell a legal pair from an invented
    # one; two arrays that do not add up look exactly like two that do.
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)
    if not _slot_exists(user_id, slot):
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    op = str(payload.get("op", "")).strip().lower()
    if op not in ("deposit", "withdraw"):
        return bad_request("op must be 'deposit' or 'withdraw'")

    item_id = str(payload.get("item_id", "")).strip()
    if not item_id or len(item_id) > 64:
        return bad_request("item_id must be 1-64 characters")

    raw_quantity = payload.get("quantity", 1)
    if isinstance(raw_quantity, bool):
        return bad_request("quantity must be a positive integer")
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError):
        return bad_request("quantity must be a positive integer")
    if quantity <= 0:
        return bad_request("quantity must be a positive integer")
    if quantity > QUANTITY_CEILING:
        return bad_request("quantity must be at most %d" % QUANTITY_CEILING)

    db = get_db()

    if op == "deposit":
        source_rows = db.execute(
            "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ?",
            (user_id, slot),
        ).fetchall()
    else:
        source_rows = db.execute(
            "SELECT position, item_id, quantity FROM bank_items WHERE user_id = ?",
            (user_id,),
        ).fetchall()

    changes, available = _take_from_cells(source_rows, item_id, quantity)
    if changes is None:
        where = "carried" if op == "deposit" else "banked"
        return bad_request(
            "Cannot move %d %s - only %d %s." % (quantity, item_id, available, where)
        )

    # REMOVE FIRST, THEN ADD, THEN CHECK. The add is the half that can fail on
    # capacity, and rolling back is how the item gets home - not an "if it fits"
    # test beforehand, which would be a second implementation of the packing rule
    # and would disagree with the real one the first time a stack was part-used.
    if op == "deposit":
        _apply_cell_changes("carry_items", ("user_id", "slot"), (user_id, slot), changes)
        written = _add_to_bank(user_id, item_id, quantity)
        full_message = "The bank is full (%d slots)." % BANK_CAPACITY
    else:
        _apply_cell_changes("bank_items", ("user_id",), (user_id,), changes)
        written = _add_to_backpack(user_id, slot, item_id, quantity)
        full_message = "Your backpack is full (%d slots)." % CARRY_CAPACITY

    if written is None:
        # Nothing was committed, so the rollback puts the source rows back
        # exactly as they were. The item is never in neither place.
        db.rollback()
        return {"error": "Conflict", "message": full_message}, 409

    db.commit()

    result = account_payload(user_id)
    result["slot"] = slot
    result["op"] = op
    result["item_id"] = item_id
    result["quantity"] = quantity
    result["inventory"] = inventory_payload(user_id, slot)
    return result, 200


@app.post("/api/bank/gold")
@require_auth
def move_bank_gold():
    """
    Move gold between a character's pocket and the shared bank
    ---
    tags:
      - Account
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, op, amount]
          properties:
            slot:   {type: integer, example: 0}
            op:     {type: string,  enum: [deposit, withdraw]}
            amount: {type: integer, example: 500}
    responses:
      200:
        description: Carried gold and the account afterwards
      400:
        description: Bad slot, op or amount, or not enough gold on the relevant side
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    # STILL TAKES A SLOT, and still an op rather than a replace.
    #
    # The slot is the CHARACTER whose pocket the gold comes out of - that half
    # is genuinely per-character. The bank half is not, and now comes from
    # accounts rather than from saves.bank_gold.
    #
    # It stays an op because this is the one operation on this whole server that
    # can be verified: it holds both balances, so it can enforce that the total
    # is conserved. Everything else here takes the client's word for what it has.
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    op = str(payload.get("op", "")).strip().lower()
    if op not in ("deposit", "withdraw"):
        return bad_request("op must be 'deposit' or 'withdraw'")

    amount = parse_stat(payload.get("amount"))
    if amount is None or amount <= 0:
        return bad_request("amount must be a positive integer")

    db = get_db()
    row = db.execute(
        "SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    account = _ensure_account(user_id)
    carried = int(row["gold"])
    banked = int(account["bank_gold"])

    if op == "deposit":
        if amount > carried:
            return bad_request("Cannot deposit %d - only %d carried." % (amount, carried))
        carried -= amount
        banked += amount
    else:
        if amount > banked:
            return bad_request("Cannot withdraw %d - only %d banked." % (amount, banked))
        banked -= amount
        carried += amount

    # Two tables now, so one transaction rather than one statement. sqlite3
    # opens a transaction on the first write and holds it until commit, so
    # there is still no moment where the gold exists in neither place.
    db.execute(
        "UPDATE saves SET gold = ?, updated_at = ? WHERE user_id = ? AND slot = ?",
        (carried, int(time.time()), user_id, slot),
    )
    db.execute("UPDATE accounts SET bank_gold = ? WHERE user_id = ?", (banked, user_id))
    db.commit()

    result = account_payload(user_id)
    result["slot"] = slot
    result["carried_gold"] = carried
    return result, 200


# =============================================================================
# COMBAT - KILL REWARDS
# =============================================================================
#
# -----------------------------------------------------------------------------
# WHAT THIS CHANGES, AND WHAT IT DOES NOT
# -----------------------------------------------------------------------------
# Before: BaseEnemy._die() granted the XP, rolled the loot and spawned the bag,
# all on the player's machine. A modified client could award itself every pet in
# the game.
#
# After: the client reports WHICH enemy died. The server decides what that is
# worth, rolls the loot with its own entropy, applies the level-ups, and commits
# the result before the client learns any of it.
#
# THIS IS HALF OF A TWO-PART CHANGE AND IS NOT YET A SECURITY FIX ON ITS OWN.
# The server now knows what a player SHOULD have earned, but PUT /api/save and
# PUT /api/player/status still accept a client-asserted level, xp and inventory.
# A modified client can still simply declare itself level 60 holding six pets.
#
# The second half is refusing those assertions and treating the server's own
# record as authoritative - and that half is only POSSIBLE once this one exists,
# because until now the server had no independent record to compare against. It
# is worth being plain about that ordering rather than shipping this and calling
# combat secured.
#
# WHAT IT DOES CLOSE TODAY: the loot roll itself. The 1-in-864 slime pet is now
# decided by the server's SystemRandom, which the client cannot observe, predict
# or re-run. No amount of patching the game can make that roll come up more
# often than it should.
# -----------------------------------------------------------------------------

# A TOKEN BUCKET, NOT A MINIMUM GAP.
#
# This was "one kill per 0.35 seconds", and it refused legitimate kills within
# an hour of shipping:
#
#     [KILL] bushmage refused — Kills are limited to one per 0.35s.
#     [KILL] poisonslimesmall refused — Kills are limited to one per 0.35s.
#
# A large poison slime splits into four smalls. An area attack kills several
# enemies in the same frame. Real combat is BURSTY, and no minimum gap can ever
# allow a burst - that is what a minimum gap is for. The player lost those
# rewards, which is the worst possible failure for a limit that cannot prove
# anything in the first place.
#
# A bucket separates the two shapes. Twenty tokens covers any burst a player can
# actually produce: a slime split, an aura wipe, a slashwave through a group.
# One token per second refills it, so sustained farming is capped at sixty kills
# a minute - roughly double the fastest honest rate observed - while a script
# asking for ten thousand kills now needs about three hours and shows up plainly
# in the data.
#
# WHAT THIS IS NOT: proof. The server cannot tell a real kill from a claimed
# one; it has no idea where anything is. This caps the rate and nothing more.
# The real answer is the server owning which enemies exist and having handed
# this client that one.
# SIZED FOR A GAME THAT THROWS CROWDS AT YOU. Darza's Dominion - one of this
# project's stated inspirations - puts dozens of enemies on screen and expects
# you to clear them without pausing, and Elusion is aiming at the same feel. A
# bucket that a good fight can empty is a bucket that punishes playing well.
#
# Fifty covers any burst a screen can hold. Five a second sustained is three
# hundred kills a minute, comfortably above the fastest honest rate and still
# bounded: a script wanting ten thousand kills needs half an hour rather than a
# few seconds, which is the difference between an exploit and a chore.
KILL_BUCKET_CAPACITY = 50.0
KILL_TOKENS_PER_SECOND = 5.0

# THE SPAWN CEILING: how many of one enemy could physically have died by now.
#
# The token bucket above caps the RATE of claims at a number somebody chose.
# This caps the COUNT at what the world contains, which is a number nobody
# chose - it falls out of how many of that enemy are placed and how fast a
# respawner returns them.
#
# THE ARITHMETIC, because the window size is not arbitrary. One spawn point
# yields at most (W / respawn) + 1 kills in a window of W seconds: one per
# respawn, plus the one that happened at the very instant the window opened.
# That +1 is pure slack handed to a cheater, and it dilutes as W grows - so a
# LONGER window bounds tighter while still refusing nobody honest:
#
#     W = 30s    ->  8,400 kills/hour   28.0x honest play
#     W = 300s   ->  4,620 kills/hour   15.4x
#     W = 1800s  ->  4,270 kills/hour   14.2x   (the asymptote)
#
# 300 is where the curve flattens; past it the table scan grows for nothing.
# The token bucket alone allows 18,000/hour, or 60x honest play, so this is
# about four times tighter and the two compose - a claim must satisfy both.
#
# RESPAWN_FLOOR IS THE MINIMUM, NOT THE AVERAGE. enemyrespawner.gd waits
# respawn_seconds and then adds up to respawn_jitter MORE, never less. Using
# the floor means the ceiling is always at least as generous as reality, so
# jitter can only ever make an honest player use less of their allowance.
# If respawn_seconds is ever lowered in a scene, lower this with it.
KILL_WINDOW_SECONDS = 300
KILL_RESPAWN_FLOOR_SECONDS = 30.0


@app.post("/api/combat/kill")
@require_auth
def combat_kill():
    """
    Report a kill and receive its rewards
    ---
    tags:
      - Combat
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, enemy_id]
          properties:
            slot:     {type: integer, example: 0}
            enemy_id: {type: string,  example: poisonslimesmall}
    responses:
      200:
        description: XP granted, level-ups applied, and whatever the loot roll produced
      400:
        description: Bad slot, unknown enemy_id, or an enemy that awards nothing
      404:
        description: That slot is empty
      429:
        description: Kills are arriving faster than the cooldown allows
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    enemy_id = str(payload.get("enemy_id", "")).strip()
    if not enemy_id or len(enemy_id) > 64:
        return bad_request("enemy_id must be 1-64 characters")

    enemy = gamedata.ENEMIES.get(enemy_id)
    if enemy is None:
        # Deliberately names the id back. This is not user input in the hostile
        # sense - it is the client and the server disagreeing about the roster,
        # which in practice means gamedata.json is stale on one side. Saying
        # which id was not found is the difference between a five-minute fix
        # and an afternoon.
        return bad_request("Unknown enemy_id '%s'. Is gamedata.json current?" % enemy_id)

    # The large poison slime is the case this exists for: it never dies, it
    # splits, and everything it is worth walks away as four smalls. A client
    # reporting it as a kill is either broken or lying, and either way there is
    # nothing to pay.
    if not enemy.get("grants_rewards", True):
        return bad_request("'%s' awards no rewards - it cannot be killed for profit." % enemy_id)

    db = get_db()
    row = db.execute(
        "SELECT class_id, level, xp, xp_to_next, last_kill_at, kill_tokens FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    now_ms = int(time.time() * 1000)

    # Refill first, then spend. last_kill_at of 0 means this character has never
    # reported a kill, and the elapsed time since 1970 would overfill the bucket
    # - min() against the capacity handles that without a special case.
    elapsed_seconds = max(now_ms - int(row["last_kill_at"]), 0) / 1000.0
    tokens = min(
        KILL_BUCKET_CAPACITY,
        float(row["kill_tokens"]) + elapsed_seconds * KILL_TOKENS_PER_SECOND,
    )

    if tokens < 1.0:
        # 429 rather than 400: nothing about the request is malformed, it simply
        # arrived faster than the bucket allows. An honest client can read that
        # and retry; a 400 would tell it to give up.
        return {
            "error": "Too Many Requests",
            "message": "Kills are arriving faster than %g per second." % KILL_TOKENS_PER_SECOND,
        }, 429

    tokens -= 1.0

    # THE SPAWN CEILING. Read KILL_WINDOW_SECONDS for the arithmetic; this is
    # the half that asks whether this many of THIS enemy could have died.
    #
    # Counted out of kill_reports rather than a new table: it already holds one
    # row per PAID kill, indexed on (user_id, at), and a refused kill leaves no
    # row - so the count is exactly "kills this account was paid for" and
    # cannot drift from what was granted.
    #
    # FAILS OPEN TWICE, both deliberate. A gamedata.json exported before the
    # spawn tally existed knows nothing about the world and must not refuse
    # every kill on a half-upgraded server. And a placed_count of 0 means
    # "this comes from somewhere I cannot see" - the poison slime's smalls are
    # spawned by its own script and appear in no scene - where refusing would
    # break a real fight. See gamedata.spawn_count_for().
    placed = gamedata.spawn_count_for(enemy_id) if gamedata.SPAWNS_EXPORTED else 0
    if placed > 0:
        window_start = int(time.time()) - KILL_WINDOW_SECONDS
        recent = db.execute(
            "SELECT COUNT(*) AS n FROM kill_reports"
            " WHERE user_id = ? AND enemy_id = ? AND at >= ?",
            (user_id, enemy_id, window_start),
        ).fetchone()
        ceiling = int(placed * (KILL_WINDOW_SECONDS / KILL_RESPAWN_FLOOR_SECONDS + 1))
        if int(recent["n"] or 0) >= ceiling:
            # 429, matching the bucket above: this is "not yet", not "never".
            # The message names the world rather than a policy, because that is
            # where the number came from and a player reading it can check it.
            return {
                "error": "Too Many Requests",
                "message": "Only %d '%s' exist in the world - that is more kills "
                           "than could have respawned in the last %d minutes."
                           % (placed, enemy_id, KILL_WINDOW_SECONDS // 60),
            }, 429

    # ---- everything above this line is validation; everything below commits --

    rewards = gamedata.roll_kill_rewards(enemy_id)

    level, xp, xp_to_next, levels_gained = gamedata.apply_xp(
        int(row["level"]), int(row["xp"]), int(row["xp_to_next"]), rewards["xp"]
    )

    # A LEVEL-UP MOVES THE MAXIMA WITH IT.
    #
    # Without this the level changed here and max_hp did not, so a character who
    # levelled from a kill carried the previous level's maximum until something
    # happened to write a status - and /api/player/status GET returns the stored
    # row, so the player would simply see the wrong number.
    #
    # Found by a test asserting max_hp against the curve rather than against a
    # hardcoded 180: the moment a kill-burst test started levelling the
    # character up, the stale value stopped matching.
    derived = gamedata.max_stats_for(row["class_id"], level) if levels_gained else None

    if levels_gained:
        # THE REFILL THAT COMES WITH A LEVEL, recorded so the healing
        # reconciler does not read it as a cheat. player.gd::level_up() fills
        # all three pools, which is a jump from wherever you were to full in no
        # time at all - indistinguishable, from the outside, from a client that
        # simply claimed it.
        #
        # Written here rather than where the client syncs, because this is the
        # moment the server DECIDED the level-up happened. Same table and same
        # reasoning as the revive grant.
        db.execute(
            "INSERT INTO consume_grants (user_id, slot, item_id, target, amount, at) "
            "VALUES (?, ?, ?, '', 0, ?)",
            (user_id, slot, LEVELUP_GRANT_ID, int(time.time())),
        )

    # ONE UPDATE. The XP, the level and the cooldown stamp move together or not
    # at all - the same reasoning as the bank gold transfer. Two statements
    # would leave a window where a crash could bank the XP and lose the level,
    # or stamp the cooldown for a kill that was never paid.
    if derived is None:
        db.execute(
            """
            UPDATE saves
               SET level = ?, xp = ?, xp_to_next = ?,
                   last_kill_at = ?, kill_tokens = ?, updated_at = ?
             WHERE user_id = ? AND slot = ?
            """,
            (level, xp, xp_to_next, now_ms, tokens, int(time.time()), user_id, slot),
        )
    else:
        # One statement, so the level and the maxima it implies can never be
        # stored apart from each other.
        db.execute(
            """
            UPDATE saves
               SET level = ?, xp = ?, xp_to_next = ?,
                   max_hp = ?, max_mana = ?, max_stamina = ?,
                   last_kill_at = ?, kill_tokens = ?, updated_at = ?
             WHERE user_id = ? AND slot = ?
            """,
            (level, xp, xp_to_next,
             derived["max_hp"], derived["max_mana"], derived["max_stamina"],
             now_ms, tokens, int(time.time()), user_id, slot),
        )

    # ATTACK XP IS BANKED HERE, not returned for the client to apply.
    #
    # The number was always the server's - rewards["attack_xp"] comes from the
    # enemy's own profile and the client never names it. What used to happen is
    # that it was handed back and the client was trusted to add it up and PUT
    # the total to /api/character/skills, so an authored reward became a claim
    # somewhere in the round trip. Granting it at the moment of the kill removes
    # the trip the lie lived in.
    #
    # BEFORE THE COMMIT, AND THAT IS NOT INCIDENTAL. _grant_skill_xp() does not
    # commit - it expects its caller's transaction, the way the fishing and
    # cooking routes use it. Called after this commit it inserts into a
    # transaction nothing ever finishes, and the row is discarded at the end of
    # the request: the response still reports a level, the database never sees
    # it. Sharing the commit also means the character's XP and the attack XP
    # from one kill can never be stored apart from each other.
    attack_level, attack_xp, attack_levels = _grant_skill_xp(
        user_id, slot, "attack", rewards["attack_xp"]
    )

    # Recorded in the same transaction, for the same reason the grant is: a kill
    # that paid out and left no trace, or a trace for a kill that was rolled
    # back, would both make the log a thing you cannot reason from.
    _record_kill(db, user_id, slot, enemy_id, rewards, level, now_ms // 1000)

    db.commit()

    # THE BAG IS STORED, and its id goes back with the contents. The client
    # spawns a node to render it, but the node is a picture: taking anything out
    # of it goes through /api/loot/take, against these rows.
    #
    # An empty bag_id means nothing dropped, and the client spawns nothing.
    #
    # `stored` is the roll with a position stamped on every entry - which is
    # what the client renders into its grid and what it sends back to take
    # anything out. The roll itself is never returned: position is not a detail
    # the two sides should be inferring separately.
    bag_id, stored = _create_loot_bag(user_id, slot, enemy_id, rewards["contents"])

    return {
        "bag_id": bag_id,
        "enemy_id": enemy_id,
        "xp_gained": rewards["xp"],
        "attack_xp_gained": rewards["attack_xp"],
        "attack_level": attack_level,
        "attack_levelled_up": attack_levels > 0,
        "levels_gained": levels_gained,
        "level": level,
        "xp": xp,
        "xp_to_next": xp_to_next,
        "pet_won": rewards["pet_won"],
        "contents": stored,
    }, 200


# =============================================================================
# MODERATION
# =============================================================================
#
# Every route here is @require_role("mod") at the door and can_act_on() inside,
# and the two do different jobs: the decorator says whether you are staff at
# all, can_act_on() says whether this particular person is within your reach.
#
# Refusals are 404, and the same 404 whether the account does not exist or
# simply outranks you. A mod who could tell those apart could map out who is
# above them by guessing names.

# How long a mod may mute someone for. Anything longer, and anything permanent,
# needs a dev or the owner.
#
# The point is not that thirty days is special. It is that a permanent removal
# and a timeout are different decisions, and the person having a bad night at
# 2am should only be able to make the reversible one.
MAX_MOD_BAN_DAYS = 30

# WHO COUNTS AS ONLINE: a session whose client called GET /api/auth/session
# within this many seconds. The game sends that heartbeat every 15 seconds, so
# 45 is three missed beats - one slow request does not flicker someone
# offline, and a closed game drops off the list within the minute.
#
# NOT "has a live session". Sessions last thirty days and survive the game
# being closed, so that count answered "who has logged in this month", and a
# kick list sorted by it put last week's visitors at the top.
ONLINE_WINDOW_SECONDS = 45


@app.post("/api/staff/ban")
@require_auth
@require_role("mod")
def ban_account():
    """
    Ban an account
    ---
    tags:
      - Moderation
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [username, reason]
          properties:
            username: {type: string}
            reason:   {type: string}
            days:     {type: integer, description: "Omit for a permanent ban."}
    responses:
      200:
        description: The ban as stored
      400:
        description: Missing reason, or a days value out of range
      403:
        description: A mod attempting a permanent or over-long ban
      404:
        description: No such account, or one you may not act on
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}

    target, error = _moderation_target(payload)
    if error is not None:
        return error

    reason = str(payload.get("reason", "")).strip()
    if not reason or len(reason) > 500:
        # REQUIRED, not optional. A ban with no reason is one nobody can review
        # later, including the person who issued it.
        return bad_request("reason must be 1-500 characters")

    raw_days = payload.get("days")
    permanent = raw_days is None

    expires_at = None
    if not permanent:
        days = parse_stat(raw_days)
        if days is None or days < 1 or days > 3650:
            return bad_request("days must be an integer 1-3650, or omitted for a permanent ban")
        expires_at = int(time.time()) + days * 86400

    # A PERMANENT BAN IS A HIGHER PERMISSION THAN A TEMPORARY ONE.
    if not role_at_least(g.user, "dev"):
        if permanent:
            return {
                "error": "Forbidden",
                "message": "Only a dev or the owner can ban permanently.",
            }, 403
        if parse_stat(raw_days) > MAX_MOD_BAN_DAYS:
            return {
                "error": "Forbidden",
                "message": "A mod may ban for at most %d days." % MAX_MOD_BAN_DAYS,
            }, 403

    db = get_db()
    now = int(time.time())

    db.execute(
        """
        UPDATE users
           SET is_banned = 1, ban_expires_at = ?, ban_reason = ?,
               banned_by = ?, banned_at = ?
         WHERE id = ?
        """,
        (expires_at, reason, g.user["username"], now, target["id"]),
    )

    # IN THE SAME TRANSACTION. Crossing the name off the list does nothing
    # about the person already inside - a banned player holding a live token
    # keeps playing until it expires, which on this server is thirty days.
    db.execute("DELETE FROM sessions WHERE user_id = ?", (target["id"],))

    log_staff_action(
        g.user, "ban", target["username"], target["id"],
        "permanent: %s" % reason if permanent else "%d days: %s" % (parse_stat(raw_days), reason),
    )
    db.commit()

    return {
        "username": target["username"],
        "banned": True,
        "permanent": permanent,
        "expires_at": expires_at,
        "reason": reason,
        "banned_by": g.user["username"],
    }, 200


@app.post("/api/staff/kick")
@require_auth
@require_role("mod")
def kick_account():
    """
    Sign an account out everywhere, without banning it
    ---
    tags:
      - Moderation
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [username]
          properties:
            username: {type: string, example: someplayer}
            reason:   {type: string, example: "suspected shared account"}
    responses:
      200:
        description: Every session for that account was destroyed
      401:
        description: Missing, invalid or expired token
      403:
        description: Not staff, or the target outranks you
      404:
        description: No such account
    """
    # THE SANCTION BETWEEN "NOTHING" AND "BANNED", and the reason it needs to
    # exist separately is that /api/staff/ban already deletes sessions - so the
    # only way to get somebody out of the game was to ban them. That makes the
    # smallest available response to "this account is behaving oddly" the
    # largest one, and a mod who only wants to interrupt something has to
    # choose between overreacting and doing nothing.
    #
    # WHAT IT IS FOR: a shared account to re-secure, a session left open on a
    # machine somebody no longer controls, a stuck client, or buying a minute
    # to look at a report before deciding. It revokes access; it does not
    # revoke permission. The account logs straight back in.
    #
    # THAT IS THE WHOLE POINT and also its honest limit: against someone
    # actively cheating this is a speed bump, because they still hold the
    # password. It is not a ban and must not be presented as one.
    #
    # SAME REACH AS EVERY OTHER SANCTION. can_act_on() via _moderation_target,
    # so a mod cannot kick another mod and nobody can kick the owner - a
    # weaker gate here would make this the cheap way to harass staff.
    #
    # LOGGED LIKE A BAN. A session that ends for no visible reason is a support
    # ticket; staff_actions is what answers it.
    payload = request.get_json(silent=True) or {}
    target, refusal = _moderation_target(payload)
    if refusal is not None:
        return refusal

    reason = str(payload.get("reason", "")).strip()[:200]

    db = get_db()
    # COUNTED BEFORE THE DELETE, because rowcount after a DELETE is the number
    # removed and the useful number is "how many places were they signed in" -
    # which a mod reads as evidence. Zero is a real answer: the account holds
    # no live session and the kick changed nothing.
    live = int(db.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ? AND expires_at > ?",
        (target["id"], int(time.time())),
    ).fetchone()["n"] or 0)

    # Every row, not only the unexpired ones: an expired session grants nothing
    # but leaving it behind means "signed out everywhere" is not quite true.
    db.execute("DELETE FROM sessions WHERE user_id = ?", (target["id"],))

    log_staff_action(
        g.user, "kick", target["username"], target["id"],
        "%d session(s)%s" % (live, (": " + reason) if reason else ""),
    )
    db.commit()

    return {
        "username": target["username"],
        "sessions_ended": live,
        "banned": False,
        "by": g.user["username"],
    }, 200


@app.post("/api/staff/unban")
@require_auth
@require_role("mod")
def unban_account():
    """
    Lift a ban
    ---
    tags:
      - Moderation
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [username]
          properties:
            username: {type: string}
    responses:
      200:
        description: The account is no longer banned
      404:
        description: No such account, or one you may not act on
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}

    target, error = _moderation_target(payload)
    if error is not None:
        return error

    db = get_db()
    db.execute(
        """
        UPDATE users
           SET is_banned = 0, ban_expires_at = NULL, ban_reason = '',
               banned_by = '', banned_at = 0
         WHERE id = ?
        """,
        (target["id"],),
    )
    log_staff_action(g.user, "unban", target["username"], target["id"])
    db.commit()

    # Idempotent on purpose: unbanning someone who is not banned is a 200 that
    # changed nothing. The caller wanted them not-banned, and they are not.
    return {"username": target["username"], "banned": False}, 200


@app.put("/api/staff/role")
@require_auth
@require_role("mod")
def set_account_role():
    """
    Change an account's rank
    ---
    tags:
      - Moderation
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [username, role]
          properties:
            username: {type: string}
            role:     {type: string, description: "player, mod or dev"}
    responses:
      200:
        description: The rank as stored
      400:
        description: Not a settable rank
      403:
        description: Granting a rank at or above your own
      404:
        description: No such account, or one you may not act on
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}

    target, error = _moderation_target(payload)
    if error is not None:
        return error

    new_role = str(payload.get("role", "")).strip().lower()
    if new_role not in SETTABLE_ROLES:
        return bad_request(
            "role must be one of: %s. 'owner' comes from the environment."
            % ", ".join(SETTABLE_ROLES)
        )

    # YOU CANNOT GRANT A RANK AT OR ABOVE YOUR OWN.
    #
    # Without this, one dev promotes another dev, or promotes a player to dev,
    # and a single compromised staff account spreads sideways for as long as
    # nobody is looking. Only the owner makes a dev.
    if ROLES.index(new_role) >= ROLES.index(role_for(g.user)):
        return {
            "error": "Forbidden",
            "message": "You cannot grant a rank at or above your own.",
        }, 403

    was = target["role"]

    db = get_db()
    db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, target["id"]))
    log_staff_action(
        g.user, "role", target["username"], target["id"], "%s -> %s" % (was, new_role)
    )
    db.commit()

    return {"username": target["username"], "role": new_role, "was": was}, 200


@app.post("/api/staff/grant")
@require_auth
@require_role("mod")
def staff_grant():
    """
    Give yourself an item (staff only)
    ---
    tags:
      - Staff
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, item_id]
          properties:
            slot: {type: integer, example: 0}
            item_id: {type: string, example: "petsniper"}
            quantity: {type: integer, example: 1}
    responses:
      200:
        description: The backpack as stored, after the grant
      400:
        description: Bad slot, item id or quantity
      404:
        description: That slot is empty, or you are not staff
      409:
        description: Backpack full
      401:
        description: Missing, invalid or expired token
    """
    # THE DEBUG KEYS, MOVED TO WHERE THEY CAN BE ENFORCED.
    #
    # F1-F7 and the P O I U Y T pet row used to add items to the client's own
    # inventory, which then pushed the whole bag here on the next save. The rank
    # check lived in the client - so a patched build that set Api.role to
    # "owner" got the keys back, and could have skipped them entirely and just
    # written the item into the array it was going to send anyway.
    #
    # Now the client asks and the SERVER decides. require_role above is the real
    # gate; _staff_debug_allowed() in player.gd is only there to stop an honest
    # player pressing a key that would be refused.
    #
    # SELF ONLY. There is no target parameter and there should not be one until
    # there is a reason: granting to someone else is a different action with
    # different consequences, and can_act_on() exists for when that day comes.
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)
    if not _slot_exists(user_id, slot):
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    item_id = str(payload.get("item_id", "")).strip()
    if not item_id or len(item_id) > 64:
        return bad_request("item_id must be 1-64 characters")

    raw_quantity = payload.get("quantity", 1)
    if isinstance(raw_quantity, bool):
        return bad_request("quantity must be a positive integer")
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError):
        return bad_request("quantity must be a positive integer")
    if quantity <= 0:
        return bad_request("quantity must be a positive integer")

    # The same ceiling an ordinary write gets. Staff is not a reason to be
    # allowed to create a cell holding a billion potions - that is a corrupt
    # row, not a privilege, and it would be this endpoint's fault.
    limit = _stack_limit(item_id)
    if quantity > limit:
        return bad_request(
            "quantity is %d, the most %s stacks to is %d" % (quantity, item_id, limit)
        )

    db = get_db()
    written = _add_to_backpack(user_id, slot, item_id, quantity)
    if written is None:
        return {
            "error": "Conflict",
            "message": "Your backpack is full (%d slots)." % CARRY_CAPACITY,
        }, 409

    # IN THE SAME TRANSACTION AS THE GRANT. An item that appears with no line in
    # the log is exactly what this endpoint exists to prevent, and "log it
    # afterwards" is how that happens the first time something raises in
    # between.
    log_staff_action(
        g.user, "grant", g.user["username"], g.user["id"],
        "%d x %s into slot %d" % (quantity, item_id, slot),
    )
    db.commit()

    return {
        "slot": slot,
        "granted_item_id": item_id,
        "granted_quantity": quantity,
        "carry_positions": written,
        "inventory": inventory_payload(user_id, slot),
    }, 200


@app.get("/api/staff/users")
@require_auth
@require_role("mod")
def list_accounts():
    """
    Every account, with rank, ban state and whether they are online
    ---
    tags:
      - Moderation
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
    responses:
      200:
        description: The account list
      401:
        description: Missing, invalid or expired token
    """
    db = get_db()
    rows = db.execute(
        "SELECT * FROM users ORDER BY id"
    ).fetchall()

    # PRESENCE IN ONE QUERY, not one per account. Only unexpired sessions: an
    # expired row is dead whatever its last heartbeat says.
    now = int(time.time())
    seen = {
        int(r["user_id"]): int(r["last_seen"] or 0)
        for r in db.execute(
            "SELECT user_id, MAX(last_seen_at) AS last_seen FROM sessions"
            " WHERE expires_at > ? GROUP BY user_id",
            (now,),
        ).fetchall()
    }

    accounts = []
    for row in rows:
        ban = ban_state(row)
        last_seen = seen.get(int(row["id"]), 0)
        accounts.append({
            "id": row["id"],
            "username": row["username"],
            "role": role_for(row),
            "banned": ban is not None,
            "ban": ban,
            # Whether YOU can act on this person, so a client can grey out the
            # buttons rather than offering them and being refused.
            "actionable": can_act_on(g.user, row),
            # See ONLINE_WINDOW_SECONDS. 0 means no live session at all.
            "online": last_seen > 0 and now - last_seen <= ONLINE_WINDOW_SECONDS,
            "last_seen_at": last_seen,
        })

    # THE SERVER'S CLOCK, so "last seen 4 min ago" is worked out against the
    # same clock that wrote last_seen_at - a client whose own clock is off by
    # an hour would otherwise say so about everyone.
    return {"accounts": accounts, "now": now}, 200


@app.get("/api/staff/user/<username>")
@require_auth
@require_role("mod")
def staff_read_user(username):
    """
    Everything staff may know about one account
    ---
    tags:
      - Moderation
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
      - in: path
        name: username
        type: string
        required: true
    responses:
      200:
        description: The account, its characters, and its recent activity
      401:
        description: Missing, invalid or expired token
      403:
        description: Not staff
      404:
        description: No such account
    """
    # THE READ SIDE OF MODERATION, and the reason the two log tables are worth
    # having. login_attempts and kill_reports answer real questions - is this
    # account being brute-forced, is it reporting kills faster than a person
    # could - but until now the only way to ask was sqlite3 on the box holding
    # elusion.db. A ban decision made without being able to look is a guess.
    #
    # WHAT IS NEVER HERE: password_hash and session tokens. Not redacted, not
    # included - a SELECT * on users would carry the hash into this payload the
    # moment someone stopped reading carefully, so the columns are named.
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No such account."}, 404

    # IP ADDRESSES ARE GATED HIGHER THAN THE REST, deliberately.
    #
    # Rank, ban state and characters are what the owner panel's view button
    # needs, and any mod may see them for anyone. An address is different: it is
    # personal data about a real person, and the honest test for whether a mod
    # should have it is whether they could act on that person at all. So it
    # follows can_act_on() - strictly above - which means a mod sees a player's
    # addresses and not another mod's, and nobody sees the owner's.
    #
    # The COUNT is always shown. "Seen from 4 addresses" is the shape of the
    # answer a mod needs for "is this account shared"; the addresses themselves
    # are what they need only if they are going to act.
    may_see_addresses = can_act_on(g.user, row)

    characters = []
    for save in db.execute(
        "SELECT slot, class_id, name, level, area, updated_at FROM saves"
        " WHERE user_id = ? ORDER BY slot", (row["id"],)
    ).fetchall():
        characters.append({
            "slot": int(save["slot"]),
            "class_id": save["class_id"],
            "name": save["name"],
            "level": int(save["level"]),
            "area": save["area"],
            "updated_at": int(save["updated_at"] or 0),
        })

    # LOGINS. Recent rows for the timeline, plus a summary so a long quiet
    # history does not hide a burst - a page of the last twenty attempts looks
    # identical whether the account has had twenty failures or two thousand.
    logins = []
    for attempt in db.execute(
        "SELECT ok, reason, ip, at FROM login_attempts WHERE username = ?"
        " ORDER BY at DESC LIMIT 20", (row["username"],)
    ).fetchall():
        entry = {
            "ok": bool(attempt["ok"]),
            "reason": attempt["reason"],
            "at": int(attempt["at"]),
        }
        if may_see_addresses:
            entry["ip"] = attempt["ip"]
        logins.append(entry)

    totals = db.execute(
        "SELECT COUNT(*) AS n,"
        "       SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS good,"
        "       COUNT(DISTINCT ip) AS addresses"
        " FROM login_attempts WHERE username = ?", (row["username"],)
    ).fetchone()
    good = int(totals["good"] or 0)
    total = int(totals["n"] or 0)

    # KILLS, grouped rather than listed. A thousand rows is not a view; "boss
    # x412, most recent 9 seconds ago" is.
    kills = []
    for kill in db.execute(
        "SELECT enemy_id, COUNT(*) AS n, MAX(at) AS latest, MIN(at) AS earliest,"
        "       SUM(xp) AS xp"
        " FROM kill_reports WHERE user_id = ? GROUP BY enemy_id ORDER BY n DESC",
        (row["id"],)
    ).fetchall():
        kills.append({
            "enemy_id": kill["enemy_id"],
            "count": int(kill["n"]),
            "xp_total": int(kill["xp"] or 0),
            "first_at": int(kill["earliest"] or 0),
            "last_at": int(kill["latest"] or 0),
        })

    # WHAT STAFF HAVE ALREADY DONE TO THEM. A second mod arriving at the same
    # account should see the first one's decision before making their own.
    history = []
    for entry in db.execute(
        "SELECT action, actor_name, detail, created_at FROM staff_actions"
        " WHERE target_id = ? ORDER BY created_at DESC LIMIT 20", (row["id"],)
    ).fetchall():
        history.append({
            "action": entry["action"],
            "by": entry["actor_name"],
            "detail": entry["detail"],
            "at": int(entry["created_at"]),
        })

    account = db.execute(
        "SELECT lusions FROM accounts WHERE user_id = ?", (row["id"],)
    ).fetchone()

    # WHERE THIS ACCOUNT IS SIGNED IN RIGHT NOW.
    #
    # login_attempts above answers "who has been trying"; this answers "who is
    # holding a key". They are different questions and the second one was not
    # askable at all - a mod deciding whether to kick had no way to see whether
    # there was anything to kick, and after a ban no way to confirm the person
    # was actually out.
    #
    # NO TOKENS. Not truncated, not masked - absent. A staff view is exactly
    # where a readable token would be most damaging, because the people reading
    # it are the ones with reach. What is useful is the SHAPE: how many keys
    # exist, how old they are, when they run out.
    #
    # EXPIRED ROWS EXCLUDED. user_for_token() refuses them and deletes them on
    # sight, so a row past its expiry grants nothing - listing it would invent
    # a session that is not there. _prune_sessions() clears them on the login
    # path anyway; this simply does not count on that having run.
    # NO issued_at, BECAUSE THE TABLE DOES NOT HOLD ONE. sessions is
    # (token, user_id, expires_at) and nothing else, so "when did they sign in"
    # would have to be derived as expires_at - TOKEN_TTL - which is right only
    # for rows issued under the CURRENT ttl and silently wrong for every row
    # issued before it last changed. A migration could add the column; it is
    # not worth one, because expires_at already carries the same information
    # against a fixed ttl: a session with 29 of its 30 days left is minutes
    # old, and one with two days left is nearly a month old.
    now_secs = int(time.time())
    sessions = []
    for s in db.execute(
        "SELECT expires_at FROM sessions"
        " WHERE user_id = ? AND expires_at > ? ORDER BY expires_at DESC LIMIT 20",
        (row["id"], now_secs),
    ).fetchall():
        sessions.append({
            "expires_at": int(s["expires_at"]),
            "expires_in": int(s["expires_at"]) - now_secs,
        })

    # LINKED ACCOUNTS, behind the same gate as the addresses themselves.
    #
    # "Who else plays from this person's connection" is a statement about other
    # people, several of whom are not the subject of this lookup and have done
    # nothing. If a mod may not see this account's addresses, they certainly
    # may not see a list of strangers derived from them - so this follows
    # can_act_on() rather than the mod rank that opens the route.
    #
    # THIS IS A READ, AND ONLY A READ. Nothing here bans, flags or scores an
    # account. It exists because the evader's NEXT account used to be invisible
    # until somebody already suspected its name.
    linked, linked_truncated = ([], False)
    if may_see_addresses:
        linked, linked_truncated = _linked_accounts(db, row["id"])

    return {
        "id": row["id"],
        "username": row["username"],
        "role": role_for(row),
        "created_at": int(row["created_at"] or 0),
        "ban": ban_state(row),
        "actionable": may_see_addresses,
        # ALWAYS SHOWN, unlike the addresses and the linked accounts.
        #
        # The gate on those two is that they are facts about OTHER PEOPLE - an
        # address identifies a household, a link names strangers who share it.
        # A session count is a fact about this account only, and it is the one
        # number a mod needs before deciding between a kick and a ban. Hiding
        # it would gate the sanction rather than the personal data.
        "sessions": {
            "active": len(sessions),
            "list": sessions,
        },
        "linked_accounts": {
            "visible": may_see_addresses,
            "accounts": linked,
            "truncated": linked_truncated,
            # Named in the payload so a client does not have to hard-code the
            # threshold to explain its own "weak" badge.
            "crowded_at": SHARED_ADDRESS_ACCOUNTS,
        },
        "lusions": int(account["lusions"]) if account else 0,
        "characters": characters,
        "logins": {
            "recent": logins,
            "total": total,
            "failed": total - good,
            "addresses": int(totals["addresses"] or 0),
            "addresses_visible": may_see_addresses,
            "locked_until": _row_int(row, "lockout_until"),
            "consecutive_failures": _row_int(row, "failed_logins"),
        },
        "kills": kills,
        "staff_history": history,
    }, 200


@app.get("/api/status")
def server_status():
    """
    Server health and maintenance window
    ---
    tags:
      - Status
    responses:
      200:
        description: The server is up; message and back_at describe any planned downtime
    """
    # NO @require_auth, deliberately. The login screen needs to be able to ask
    # "are you there?" before anyone has logged in, and a 401 is not an answer
    # to that question.
    #
    # WHY THIS EXISTS: the client currently infers the difference between "your
    # internet is down" and "the host's server is down" from an HTTP failure
    # code, and that inference is unreliable - a dead port on localhost reports
    # a timeout rather than a refused connection, and a timeout looks identical
    # either way. It cannot be inferred. It has to be told.
    #
    # A reachable /api/status also means a scheduled restart can ANNOUNCE
    # itself rather than surfacing as an error. Downtime the player was warned
    # about is maintenance; the same downtime unannounced is a broken game.
    return {
        "online": True,
        "gamedata_schema": gamedata.EXPECTED_SCHEMA,
        "items": len(gamedata.ITEMS),
        "enemies": len(gamedata.ENEMIES),
        # Set these by hand before a planned restart. back_at is an RFC3339
        # timestamp the client can render in the player's own timezone; null
        # means no downtime is planned.
        "message": "",
        "back_at": None,
    }, 200


# =============================================================================
# CHARACTER - CARRY INVENTORY AND SKILLS
# =============================================================================
#
# The two things a character is made of that the server could not hold.
#
# `saves` already covers identity and vitals - class, name, level, area, hp,
# mana, stamina, gold, xp. `bank_items` covers storage. Between them they still
# could not represent a character, because the BACKPACK and every SKILL lived
# only in the client's local file. A server that cannot describe a character
# cannot be the authority on one, so this is the prerequisite for moving saves
# off the player's disk at all.
#
# WHY carry_items IS KEYED ON POSITION AND bank_items IS NOT
# ---------------------------------------------------------
# The bank is a set: forty stacks, order irrelevant, so (user_id, slot, item_id)
# is the natural key and topping up a stack is an upsert.
#
# The backpack is a GRID. The player drags a potion into the third cell and it
# stays in the third cell; two different stacks of the same item in two cells is
# a legal, ordinary state. Keying on item_id would silently merge them and shuffle
# everything else, which the player would experience as their bag rearranging
# itself on login. So the key is (user_id, slot, position) - one item per cell,
# which is exactly the invariant the inventory UI already enforces.
#
# WHY BOTH ENDPOINTS REPLACE RATHER THAN PATCH
# --------------------------------------------
# The bank has per-operation endpoints because a deposit is a discrete act the
# server can reason about. An inventory is not: it is the result of picking
# things up, dropping them, dragging them around and using them, and the client
# always holds the whole picture. Twenty cells is small enough to send in full,
# and a whole-array replace has no partial-failure state to reconcile.

# The client's backpack is twenty cells. Kept here rather than inferred from
# whatever arrives so an oversized array is a 400 rather than a slow leak into
# the database.
INVENTORY_CAPACITY = 20

# Skills a character can have. An unknown id is refused rather than stored,
# because a typo'd skill name would otherwise sit in the table forever, be
# returned on every load, and never match anything the client looks for.
# Spelled to match characterdata.gd's SKILL_GROWTH_FACTORS exactly. It says
# "defense", not "defence" - and a skill name that does not match is a level the
# player earned that the server refuses on every save.
VALID_SKILLS = {"attack", "magic", "agility", "defense", "fishing", "cooking"}


# The carry_items and skills tables are created in init_db()'s schema block
# alongside every other table, NOT here. init_db() runs at import time, at the
# top of this file - a helper defined further down does not exist yet when it
# is called, which is exactly the NameError this replaced.


# The most of anything a single cell may hold when the server does not
# recognise the item. Known items are capped at their own max_stack instead.
#
# 9999 matches the largest real stack in the game (a gold pile). It is not a
# guess at what is reasonable, it is the biggest thing the game itself makes.
QUANTITY_CEILING = 9999


def _stack_limit(item_id):
    """How many of item_id may sit in one cell."""
    definition = gamedata.ITEMS.get(item_id)
    if definition is None:
        return QUANTITY_CEILING
    if not definition.get("stackable", False):
        return 1
    return max(1, min(int(definition.get("max_stack", 1)), QUANTITY_CEILING))


def _parse_positional_items(cells, field):
    """
    Validate a positional item array - the backpack and the bank are the same
    shape and this is the only place that shape is checked.

    Returns (parsed, None) or (None, an error response). The WHOLE array is
    validated before the caller writes any of it: a bad entry at index 17 must
    not leave the first seventeen stored, because a half-saved bag with no error
    is worse than a refused write.
    """
    parsed = []
    for index, cell in enumerate(cells):
        if cell is None:
            continue
        if not isinstance(cell, dict):
            return None, bad_request("%s[%d] must be an object or null" % (field, index))

        item_id = str(cell.get("item_id", "")).strip()
        if not item_id or len(item_id) > 64:
            return None, bad_request("%s[%d].item_id must be 1-64 characters" % (field, index))

        raw_quantity = cell.get("quantity", 1)
        if isinstance(raw_quantity, bool):
            return None, bad_request("%s[%d].quantity must be a positive integer" % (field, index))
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            return None, bad_request("%s[%d].quantity must be a positive integer" % (field, index))
        if quantity <= 0:
            return None, bad_request("%s[%d].quantity must be a positive integer" % (field, index))

        # The ceiling, which "positive integer" alone does not give you. Without
        # it a client could declare a cell holding a billion potions and this
        # would store it: the table only checks quantity > 0.
        #
        # A known item is capped at its own max_stack, which is the rule the
        # game already plays by. An unknown one falls back to QUANTITY_CEILING,
        # because item ids are deliberately NOT validated against a list here -
        # that is what lets the game add an item without a matching server
        # deploy, and it is worth keeping.
        limit = _stack_limit(item_id)
        if quantity > limit:
            return None, bad_request(
                "%s[%d].quantity is %d, the most %s stacks to is %d"
                % (field, index, quantity, item_id, limit)
            )

        parsed.append((index, item_id, quantity))

    return parsed, None


def _inventory_totals(user_id, slot):
    """item_id -> total quantity the server currently records for this slot."""
    totals = {}
    rows = get_db().execute(
        "SELECT item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchall()
    for row in rows:
        totals[row["item_id"]] = totals.get(row["item_id"], 0) + int(row["quantity"])
    return totals


def _bank_totals(user_id):
    """item_id -> total quantity the server currently records in the bank.

    ACCOUNT-WIDE, NOT PER SLOT. The bank is shared between a player's four
    characters, which is the one structural difference from the backpack and the
    reason this cannot just be _inventory_totals with a slot passed in.
    """
    totals = {}
    rows = get_db().execute(
        "SELECT item_id, quantity FROM bank_items WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    for row in rows:
        totals[row["item_id"]] = totals.get(row["item_id"], 0) + int(row["quantity"])
    return totals


def _trim_to_recorded(claimed, recorded):
    """
    THE TRIMMING ITSELF, shared by the backpack and the bank.

    Walks the claimed cells in order, hands each one as much of its item as the
    record still has unallotted, and returns (kept_cells, trims). Position is
    preserved, so reordering is free; quantity is capped, so a gain is not.

    ONE FUNCTION BECAUSE IT IS ONE RULE. The bank version started as a copy of
    the backpack loop, which is how this codebase has already been burned twice:
    the character XP formula lived in two places, drifted, and the sanitizer
    rewrote honest saves; the six skill growth factors then did it again in
    player.gd. A second hand-kept copy of the AUTHORITY rule would be that same
    mistake in the place it matters most.
    """
    running = {}
    kept = []
    trims = {}
    for position, item_id, quantity in claimed:
        room = max(int(recorded.get(item_id, 0)) - running.get(item_id, 0), 0)
        granted = min(quantity, room)
        if granted > 0:
            kept.append((position, item_id, granted))
            running[item_id] = running.get(item_id, 0) + granted
        if quantity > granted:
            trims[item_id] = trims.get(item_id, 0) + (quantity - granted)
    return kept, trims


def _reconcile_bank(user, claimed):
    """
    SERVER AUTHORITY OVER THE BANK - the backpack's remaining sibling.

    PUT /api/account/bank replaced the whole bank with whatever arrived, which
    is exactly the hole E-1 described for the backpack: the shape was validated
    impeccably and provenance was never asked about, so a modified client could
    fabricate anything in gamedata.json straight into storage.

    SAFE FOR THE SAME REASON THE BACKPACK WAS, and this was checked rather than
    assumed: every legitimate way an item ENTERS the bank already writes
    bank_items server-side first. POST /api/bank/items handles both deposit and
    withdraw. There is no honest client-side path that puts something in the
    bank, so an honest sync always matches the record. What is left for this
    endpoint is REORDERING, which passes through untouched - position is kept
    and only quantity is capped.

    Staff are exempt for the same reason as the backpack: a mod can already
    self-grant through POST /api/staff/grant, so clamping them closes no door.
    """
    if role_at_least(user, "mod"):
        return claimed

    kept, trims = _trim_to_recorded(claimed, _bank_totals(user["id"]))
    if trims:
        detail = ", ".join("%s -%d" % (i, q) for i, q in sorted(trims.items()))
        print("[LEDGER] trimmed unearned bank items for %s: %s"
              % (user["username"], detail))
    return kept


def _reconcile_inventory(user, slot, claimed):
    """
    SERVER AUTHORITY OVER GAINS. Returns the claimed backpack with any item the
    client holds MORE of than the server recorded trimmed back down to what the
    server recorded, and logs the trim. Reordering and reductions pass through
    untouched. This is the enforce half of what used to be a shadow log.

    WHY THIS IS SAFE FOR THE LIVE CLIENT. Every legitimate way to GAIN an item
    already writes carry_items on the server BEFORE the client would sync the
    bag back: loot (POST /api/loot/take), bank withdrawals (POST /api/bank/items),
    and the staff grant (POST /api/staff/grant). The client only ever holds what
    one of those wrote, so an honest sync always matches the record and is never
    trimmed. Shops, crafting and cooking do not exist server-side yet; the day
    they do, each must grant through the server the same way - and until then
    there is no honest client-side gain for this to catch by mistake.

    A client holding LESS is honest (ate a potion, deposited, dropped) and passes.
    A client holding MORE than the server ever granted is a modified client, and
    the excess is trimmed to what was actually granted - usually to nothing.

    STAFF ARE EXEMPT. A mod or above can already grant themselves anything via
    POST /api/staff/grant, so clamping them closes no door - it would only make
    staff tooling and the test fixtures fight the server for zero security gain.
    """
    if role_at_least(user, "mod"):
        return claimed

    kept, trims = _trim_to_recorded(claimed, _inventory_totals(user["id"], slot))
    if trims:
        detail = ", ".join("%s -%d" % (i, q) for i, q in sorted(trims.items()))
        print("[LEDGER] trimmed unearned items for %s slot %d: %s"
              % (user["username"], slot, detail))
    return kept


def _slot_exists(user_id, slot):
    row = get_db().execute(
        "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    return row is not None


def inventory_payload(user_id, slot):
    """
    The backpack as a POSITIONAL ARRAY of length INVENTORY_CAPACITY, with null
    in every empty cell - not as a list of the items that happen to exist.

    The client's inventory is an array where index IS the grid cell, and
    returning a packed list would make the client responsible for rebuilding
    the gaps. It would get that right the first time and wrong the first time
    someone changed the capacity.
    """
    rows = get_db().execute(
        "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ? ORDER BY position",
        (user_id, slot),
    ).fetchall()

    cells = [None] * INVENTORY_CAPACITY
    for row in rows:
        position = int(row["position"])
        # Defensive: a row outside the current capacity means the capacity was
        # reduced after it was written. Drop it from the view rather than
        # crashing on the index - the next write prunes it for real.
        if 0 <= position < INVENTORY_CAPACITY:
            cells[position] = {"item_id": row["item_id"], "quantity": int(row["quantity"])}

    return cells


def skills_payload(user_id, slot):
    rows = get_db().execute(
        "SELECT skill_id, level, xp FROM skills WHERE user_id = ? AND slot = ? ORDER BY skill_id",
        (user_id, slot),
    ).fetchall()
    return {row["skill_id"]: {"level": int(row["level"]), "xp": int(row["xp"])} for row in rows}


@app.post("/api/shop/buy")
@require_auth
def shop_buy():
    """
    ---
    post:
      summary: Buy from a vendor
      description: >
        Server-authoritative purchase. The server checks the shop stocks the
        item, prices it from its OWN ItemData copy, takes the gold and grants
        the item in one transaction. The gold is DESTROYED, not moved - this is
        the first real sink in the economy.
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [slot, shop_id, item_id]
              properties:
                slot:     {type: integer, example: 0}
                shop_id:  {type: string,  example: generalstore}
                item_id:  {type: string,  example: tinyhealthpotion}
                quantity: {type: integer, example: 1}
      responses:
        200:
          description: Bought. Returns the new gold balance and backpack.
        400:
          description: Bad slot, unknown shop, item not stocked, or not enough gold
        404:
          description: That slot is empty
        409:
          description: Backpack full
        401:
          description: Missing, invalid or expired token
    """
    # WHY THIS EXISTS AT ALL, rather than the client taking gold and adding an
    # item locally. _reconcile_inventory() trims a client holding MORE of an
    # item than the server granted - but a client-side shop asserts both halves
    # of the trade, the item gained AND the gold spent, and the trim only
    # checks the first. A shop that ran on the client would be an item printer
    # with a receipt.
    #
    # It is also the sink. Every gold that leaves here leaves the economy for
    # good, which is the whole reason vendors sell rather than buy.
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)
    if not _slot_exists(user_id, slot):
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    shop_id = str(payload.get("shop_id", "")).strip()
    if not shop_id or len(shop_id) > 64:
        return bad_request("shop_id must be 1-64 characters")

    item_id = str(payload.get("item_id", "")).strip()
    if not item_id or len(item_id) > 64:
        return bad_request("item_id must be 1-64 characters")

    raw_quantity = payload.get("quantity", 1)
    if isinstance(raw_quantity, bool):
        return bad_request("quantity must be a positive integer")
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError):
        return bad_request("quantity must be a positive integer")
    if quantity <= 0:
        return bad_request("quantity must be a positive integer")

    # The same ceiling an ordinary write gets. One cell cannot hold more than
    # the item stacks to, and a purchase is not a reason to create a row that
    # breaks that rule everywhere else in the game.
    limit = _stack_limit(item_id)
    if quantity > limit:
        return bad_request(
            "quantity is %d, the most %s stacks to is %d" % (quantity, item_id, limit)
        )

    if shop_id not in gamedata.SHOPS:
        # Named back deliberately, the same way an unknown enemy_id is: this is
        # the client and the server disagreeing about the world, which almost
        # always means gamedata.json is stale on one side.
        return bad_request("Unknown shop '%s'. Is gamedata.json current?" % shop_id)

    # PRICED BY THE SERVER, FROM THE SERVER'S CATALOGUE. The request does not
    # carry a price and would not be believed if it did.
    unit_price = gamedata.shop_price(shop_id, item_id)
    if unit_price is None:
        return bad_request("'%s' does not stock '%s'." % (shop_id, item_id))

    total = unit_price * quantity

    db = get_db()
    row = db.execute(
        "SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    carried = int(row["gold"]) if row is not None else 0
    if carried < total:
        return bad_request(
            "That costs %d gold and you have %d." % (total, carried)
        )

    # ---- everything above this line is validation; below it commits --------

    # THE ITEM FIRST, because this is the half that can still fail. It writes
    # nothing unless the whole quantity fits, so a refusal here leaves the
    # backpack exactly as it was and the gold untouched.
    written = _add_to_backpack(user_id, slot, item_id, quantity)
    if written is None:
        db.rollback()
        return {
            "error": "Conflict",
            "message": "Your backpack is full (%d slots)." % CARRY_CAPACITY,
        }, 409

    # THE BURN. Negative delta, through gold_delta() so it lands in the ledger
    # in the same transaction as the balance - see gold_ledger in init_db().
    # This is gold leaving the world, not moving to a vendor's pocket: nothing
    # holds it afterwards, which is what makes it a sink.
    remaining = gold_delta(
        db, user_id, slot, -total, "shop_buy",
        "%s x%d @%d from %s" % (item_id, quantity, unit_price, shop_id),
    )

    db.commit()

    return {
        "slot": slot,
        "shop_id": shop_id,
        "item_id": item_id,
        "quantity": quantity,
        "unit_price": unit_price,
        "total_paid": total,
        "gold": remaining,
        "inventory": inventory_payload(user_id, slot),
    }, 200


@app.get("/api/shop/<shop_id>")
@require_auth
def shop_catalogue(shop_id):
    """
    ---
    get:
      summary: What a vendor stocks, with prices
      description: >
        The prices the server will actually charge, so the panel cannot show a
        number the buy endpoint then disagrees with.
      responses:
        200:
          description: The vendor's stock
        404:
          description: No such shop
    """
    shop = gamedata.SHOPS.get(str(shop_id).strip())
    if shop is None:
        return {"error": "Not Found", "message": "No shop '%s'." % shop_id}, 404

    # PRICED HERE RATHER THAN IN THE CLIENT. The panel could multiply
    # ItemData.value by the shop's multiplier itself and usually get the same
    # answer - and the day the rounding rule changes, it would quietly get a
    # different one and show a price the server refuses. One source.
    stock = []
    for item_id in shop.get("stock", []):
        definition = gamedata.ITEMS.get(item_id)
        if definition is None:
            continue
        stock.append({
            "item_id": item_id,
            "display_name": definition.get("display_name", item_id),
            "tier": definition.get("tier", 1),
            "type_name": definition.get("type_name", ""),
            "required_level": definition.get("required_level", 1),

            # THE OTHER HALF OF THE REQUIREMENT. A cooked fish is gated on
            # COOKING, not on character level - see itemdata.gd's comment on
            # required_skill - so a row carrying only required_level would let
            # the panel print "Needs nothing" for a Reef Clown that needs 70
            # cooking. Sent from the server's own ItemData copy for the same
            # reason the price is: one source, and a stale client cannot
            # disagree with it.
            "required_skill": definition.get("required_skill", ""),
            "required_skill_level": definition.get("required_skill_level", 1),

            # THE THIRD HALF, and the one a weapon rack needs most. A sword
            # and a staff sit side by side at the same price and the same
            # level, and nothing on the row said which of them a warrior can
            # actually hold - so the first time you find out is when the
            # equip endpoint refuses it, after you have paid.
            #
            # An empty list means "anyone", which is most of the catalogue, so
            # the panel prints nothing rather than "Needs: anyone".
            #
            # FROM THE SERVER'S OWN COPY, like the price and the level, for
            # the reason those are: one source, and a stale client cannot
            # disagree with it.
            "required_classes": list(definition.get("required_classes") or []),

            # WHAT IT IS, for the same row. A tooltip that says "Ember Staff"
            # and a price is a tooltip that made you click to learn anything.
            "equip_slot_name": definition.get("equip_slot_name", ""),
            "damage": int(definition.get("damage", 0) or 0),
            "armor_value": int(definition.get("armor_value", 0) or 0),

            "max_stack": definition.get("max_stack", 1),
            "price": gamedata.shop_price(shop["shop_id"], item_id),
        })

    return {
        "shop_id": shop["shop_id"],
        "display_name": shop.get("display_name", "Shop"),
        "stock": stock,
    }, 200


# =============================================================================
# PLAYER TRADE AND THE KINGDOM TAX
# =============================================================================
# THE ONLY PLACE TWO PLAYERS' PROPERTY CHANGES HANDS, and the second gold sink
# in the game after the vendor.
#
# The rule of the world is that the kingdom takes a cut of every trade. It is
# not a fee paid to anyone - the gold is DESTROYED, through gold_delta(), and
# that is the only reason it helps. A fee collected by an NPC moves gold and
# leaves total supply exactly where it was.
#
# WHAT IS TAXED IS WHAT YOU RECEIVE, valued at ItemData.value, including gold.
# Taxing only the gold leg would make barter the untaxed route and every trade
# would instantly become barter; taxing the whole crossing is what stops the
# rule being routed around. It is charged AFTER the swap, so the gold you were
# just handed can pay it - otherwise selling something valuable would need you
# to already be rich.

# How many distinct item types one side may put up. The backpack is
# INVENTORY_CAPACITY cells and both sides need room to RECEIVE, so this is
# deliberately well under it rather than equal to it.
TRADE_MAX_ITEM_TYPES = 8

# An offer nobody has touched for this long is dead. Trades are not swept by a
# background job - there isn't one - so staleness is judged when somebody asks,
# which means an abandoned trade costs nothing until it is in the way.
TRADE_EXPIRY_SECONDS = 600


def _trade_side_of(trade, user_id):
    """'a', 'b', or None for somebody else's trade."""
    if int(trade["a_user"]) == int(user_id):
        return "a"
    if int(trade["b_user"]) == int(user_id):
        return "b"
    return None


def _trade_items(db, trade_id, side):
    rows = db.execute(
        "SELECT item_id, quantity FROM trade_items WHERE trade_id = ? AND side = ? ORDER BY item_id",
        (trade_id, side),
    ).fetchall()
    return [{"item_id": r["item_id"], "quantity": int(r["quantity"])} for r in rows]


def _trade_value(items, gold):
    """
    What one side is putting up, in gold. None when an item is not in the
    catalogue - which is a refusal, not a zero: valuing an unknown item at
    nothing would make it the tax-free way to move anything.
    """
    total = int(gold)
    for entry in items:
        worth = gamedata.stack_value(entry["item_id"], entry["quantity"])
        if worth is None:
            return None
        total += worth
    return total


def _trade_find_open(db, user_id):
    return db.execute(
        "SELECT * FROM trades WHERE state = 'open' AND (a_user = ? OR b_user = ?)",
        (user_id, user_id),
    ).fetchone()


def _trade_expired(trade):
    return (int(time.time()) - int(trade["updated_at"])) > TRADE_EXPIRY_SECONDS


def _trade_payload(db, trade):
    """
    The trade as both sides' panels draw it.

    THE TAX IS QUOTED HERE, from the same gamedata.trade_tax() the execution
    charges. A panel that computed its own would be a panel that could show a
    number the server then disagrees with, and the one moment a player must not
    be surprised is the moment they press confirm.

    Quoted, not promised: either side changing their offer re-quotes it, and the
    figure that is actually charged is computed again at execution against the
    offer as it stands then.
    """
    a_items = _trade_items(db, trade["trade_id"], "a")
    b_items = _trade_items(db, trade["trade_id"], "b")
    a_value = _trade_value(a_items, int(trade["a_gold"]))
    b_value = _trade_value(b_items, int(trade["b_gold"]))

    def names(user_id):
        row = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["username"] if row is not None else "?"

    return {
        "trade_id": trade["trade_id"],
        "state": trade["state"],
        "a": {
            "username": names(int(trade["a_user"])),
            "slot": int(trade["a_slot"]),
            "items": a_items,
            "gold": int(trade["a_gold"]),
            "confirmed": bool(int(trade["a_confirmed"])),
            "offering_value": a_value,
            # What A will pay: tax on what A RECEIVES, which is B's side.
            "tax": gamedata.trade_tax(b_value) if b_value is not None else None,
        },
        "b": {
            "username": names(int(trade["b_user"])),
            "slot": int(trade["b_slot"]),
            "items": b_items,
            "gold": int(trade["b_gold"]),
            "confirmed": bool(int(trade["b_confirmed"])),
            "offering_value": b_value,
            "tax": gamedata.trade_tax(a_value) if a_value is not None else None,
        },
    }


def _trade_touch(db, trade_id):
    """Any change to an offer un-confirms BOTH sides.

    You confirm what you were shown. If one side could add an item after the
    other had agreed, 'confirmed' would mean nothing - and the version of this
    bug that ships is the one where the change is a REMOVAL.
    """
    db.execute(
        "UPDATE trades SET a_confirmed = 0, b_confirmed = 0, updated_at = ?"
        " WHERE trade_id = ?",
        (int(time.time()), trade_id),
    )


def _trade_refuse(db, trade_id, body, status):
    """
    Undo a failed execution and put the trade back to nobody-has-agreed.

    A BARE ROLLBACK IS NOT ENOUGH, AND THE ASYMMETRY IT LEAVES IS BACKWARDS.
    The confirm that triggered execution is written in THIS transaction; the
    other side's was committed in an earlier request. So a plain rollback undoes
    the confirmation of the player who just pressed the button and keeps the one
    from the player who did not - the panel then shows the other party as
    committed to an offer that has just been refused, while the person looking
    at it appears not to have agreed at all.

    It also quietly changes what "confirmed" means. Both sides agreed to a trade
    the world then refused; whatever was true when they agreed is not true now,
    so the honest state is that the agreement lapsed and has to be made again
    against the facts as they stand.

    Rolls back FIRST - every write the attempt made has to go, including the
    claim on the trade row - and only then writes the one fact worth keeping.
    """
    db.rollback()
    db.execute(
        "UPDATE trades SET a_confirmed = 0, b_confirmed = 0, updated_at = ?"
        " WHERE trade_id = ? AND state = 'open'",
        (int(time.time()), trade_id),
    )
    db.commit()
    return body, status


def _execute_trade(db, trade):
    """
    Cross both sides, burn the tax, in ONE transaction. Returns (body, status).

    ORDER MATTERS AND IS NOT ARBITRARY:
      1. claim the trade, so two confirms racing can only fire once
      2. value both sides and compute both taxes
      3. check both purses survive the swap AND the tax
      4. take every item from both bags - all-or-nothing, so a missing item
         refuses before anything has moved
      5. add the crossed items - taking first is what frees the cells, so a
         one-for-one swap between two full backpacks still works
      6. move the gold as a TRANSFER, writing NO ledger rows
      7. burn the two taxes through gold_delta(), which does write rows
    Any refusal rolls the whole thing back; the caller does not commit on a
    non-200.

    STEP 6 IS THE ONE TO GET RIGHT. gold_delta() calls itself the only way gold
    may change, and for CREATION and DESTRUCTION it is. A transfer is neither:
    both purses count toward supply, so routing it through gold_delta() would
    write a -N and a +N that net to zero but leave the ledger claiming two
    events that never touched the money supply. gold_ledger's own comment says
    transfers write nothing. The tax is the only part of a trade that is a real
    ledger event, because it is the only part where gold stops existing.
    """
    trade_id = trade["trade_id"]

    # 1. CLAIM IT. Conditional on still being open, and we check a row actually
    # changed - this is the compare-and-set that makes a double confirm safe.
    claimed = db.execute(
        "UPDATE trades SET state = 'done', updated_at = ? WHERE trade_id = ? AND state = 'open'",
        (int(time.time()), trade_id),
    )
    if claimed.rowcount != 1:
        db.rollback()
        return {"error": "Conflict", "message": "That trade is no longer open."}, 409

    a_user, a_slot = int(trade["a_user"]), int(trade["a_slot"])
    b_user, b_slot = int(trade["b_user"]), int(trade["b_slot"])
    a_gold, b_gold = int(trade["a_gold"]), int(trade["b_gold"])

    a_items = _trade_items(db, trade_id, "a")
    b_items = _trade_items(db, trade_id, "b")

    # 2. VALUE AND TAX.
    a_value = _trade_value(a_items, a_gold)
    b_value = _trade_value(b_items, b_gold)
    if a_value is None or b_value is None:
        return _trade_refuse(db, trade_id,
                             *bad_request("This trade contains an item the server does not know."))

    a_tax = gamedata.trade_tax(b_value)   # A pays on what A receives
    b_tax = gamedata.trade_tax(a_value)

    # 3. PURSES. Read inside the transaction, never from the trade row: the
    # offer records what was PROMISED, and what is HELD is a different fact that
    # may have changed since.
    def purse(user_id, slot):
        row = db.execute(
            "SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
        ).fetchone()
        return None if row is None else int(row["gold"])

    a_purse, b_purse = purse(a_user, a_slot), purse(b_user, b_slot)
    if a_purse is None or b_purse is None:
        return _trade_refuse(db, trade_id,
                             {"error": "Not Found",
                              "message": "One of these characters no longer exists."}, 404)

    a_final = a_purse - a_gold + b_gold - a_tax
    b_final = b_purse - b_gold + a_gold - b_tax
    if a_purse < a_gold or b_purse < b_gold:
        return _trade_refuse(db, trade_id,
                             *bad_request("Somebody no longer has the gold they offered."))
    if a_final < 0 or b_final < 0:
        return _trade_refuse(db, trade_id, *bad_request(
            "The kingdom's cut is %d and %d; one of you cannot cover it." % (a_tax, b_tax)))

    # 4. TAKE. Both bags emptied of what was promised before anything is added.
    for entry in a_items:
        if not _take_from_backpack(a_user, a_slot, entry["item_id"], entry["quantity"]):
            return _trade_refuse(db, trade_id,
                                 *bad_request("Somebody no longer has the items they offered."))
    for entry in b_items:
        if not _take_from_backpack(b_user, b_slot, entry["item_id"], entry["quantity"]):
            return _trade_refuse(db, trade_id,
                                 *bad_request("Somebody no longer has the items they offered."))

    # 5. CROSS.
    for entry in b_items:
        if _add_to_backpack(a_user, a_slot, entry["item_id"], entry["quantity"]) is None:
            return _trade_refuse(db, trade_id,
                                 {"error": "Conflict",
                                  "message": "A backpack is too full for this trade."}, 409)
    for entry in a_items:
        if _add_to_backpack(b_user, b_slot, entry["item_id"], entry["quantity"]) is None:
            return _trade_refuse(db, trade_id,
                                 {"error": "Conflict",
                                  "message": "A backpack is too full for this trade."}, 409)

    # 6. THE TRANSFER. Bare UPDATEs on purpose - see this function's header.
    now = int(time.time())
    if a_gold or b_gold:
        db.execute(
            "UPDATE saves SET gold = gold - ? + ?, updated_at = ? WHERE user_id = ? AND slot = ?",
            (a_gold, b_gold, now, a_user, a_slot),
        )
        db.execute(
            "UPDATE saves SET gold = gold - ? + ?, updated_at = ? WHERE user_id = ? AND slot = ?",
            (b_gold, a_gold, now, b_user, b_slot),
        )

    # 7. THE BURN. The only ledger events in a trade.
    if a_tax > 0:
        gold_delta(db, a_user, a_slot, -a_tax, "kingdom_tax",
                   "trade %s receiving %d" % (trade_id[:8], b_value))
    if b_tax > 0:
        gold_delta(db, b_user, b_slot, -b_tax, "kingdom_tax",
                   "trade %s receiving %d" % (trade_id[:8], a_value))

    db.commit()

    return {
        "trade_id": trade_id,
        "state": "done",
        "tax_paid": {"a": a_tax, "b": b_tax},
        "kingdom_take": a_tax + b_tax,
        "a": {"gold": a_final, "inventory": inventory_payload(a_user, a_slot)},
        "b": {"gold": b_final, "inventory": inventory_payload(b_user, b_slot)},
    }, 200


@app.post("/api/trade/offer")
@require_auth
def trade_offer():
    """
    Open a trade with another player
    ---
    tags:
      - Trade
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, username]
          properties:
            slot:     {type: integer, example: 0}
            username: {type: string,  example: "otherplayer"}
            to_slot:  {type: integer, example: 0}
    responses:
      200: {description: The open trade}
      400: {description: Bad slot, unknown player, or trading with yourself}
      409: {description: One of you is already in a trade}
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)
    if not _slot_exists(user_id, slot):
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    username = str(payload.get("username", "")).strip()
    if not username:
        return bad_request("username is required")

    db = get_db()
    other = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if other is None:
        return bad_request("No player called '%s'." % username)

    other_id = int(other["id"])
    # REFUSED EXPLICITLY, not left to chance. A self-trade would run every step
    # below against one backpack, and step 4 taking from the same bag step 5
    # adds to is precisely how a duplication bug is written.
    if other_id == user_id:
        return bad_request("You cannot trade with yourself.")

    to_slot = parse_slot(payload.get("to_slot", 0))
    if to_slot is None:
        return bad_request("to_slot must be an integer 0-%d" % MAX_SLOT)
    if not _slot_exists(other_id, to_slot):
        return bad_request("%s has no character in slot %d." % (username, to_slot))

    # ONE OPEN TRADE EACH. Two concurrent trades could each validate against the
    # same sword and both pass, because neither holds it.
    for candidate in (user_id, other_id):
        existing = _trade_find_open(db, candidate)
        if existing is not None:
            if not _trade_expired(existing):
                return {
                    "error": "Conflict",
                    "message": "Already in a trade." if candidate == user_id
                               else "%s is already in a trade." % username,
                }, 409
            # Stale. Judged when it is in the way rather than by a sweeper.
            db.execute("UPDATE trades SET state = 'cancelled', updated_at = ?"
                       " WHERE trade_id = ?", (int(time.time()), existing["trade_id"]))

    trade_id = secrets.token_urlsafe(16)
    now = int(time.time())
    db.execute(
        "INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (trade_id, user_id, slot, other_id, to_slot, now, now),
    )
    db.commit()

    return _trade_payload(db, db.execute(
        "SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()), 200


@app.get("/api/trade")
@require_auth
def trade_current():
    """
    The trade this player is in, or null
    ---
    tags:
      - Trade
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200: {description: The open trade, or {"trade": null}}
    """
    db = get_db()
    trade = _trade_find_open(db, g.user["id"])
    if trade is None or _trade_expired(trade):
        return {"trade": None}, 200
    return {"trade": _trade_payload(db, trade)}, 200


@app.post("/api/trade/update")
@require_auth
def trade_update():
    """
    Replace your side of the offer
    ---
    tags:
      - Trade
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            items: {type: array, description: "[{item_id, quantity}], replaces your whole side"}
            gold:  {type: integer, example: 500}
    responses:
      200: {description: The trade as it now stands, with both sides un-confirmed}
      400: {description: Bad item, quantity, or gold you do not have}
      404: {description: You are not in a trade}
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    db = get_db()
    trade = _trade_find_open(db, user_id)
    if trade is None or _trade_expired(trade):
        return {"error": "Not Found", "message": "You are not in a trade."}, 404

    side = _trade_side_of(trade, user_id)
    slot = int(trade["%s_slot" % side])

    raw_gold = payload.get("gold", 0)
    if isinstance(raw_gold, bool) or not isinstance(raw_gold, int):
        return bad_request("gold must be an integer")
    if raw_gold < 0:
        return bad_request("gold must not be negative")

    purse = db.execute(
        "SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    if purse is None:
        return {"error": "Not Found", "message": "That character no longer exists."}, 404
    if raw_gold > int(purse["gold"]):
        return bad_request("You only have %d gold." % int(purse["gold"]))

    raw_items = payload.get("items", [])
    if not isinstance(raw_items, list):
        return bad_request("items must be an array")
    if len(raw_items) > TRADE_MAX_ITEM_TYPES:
        return bad_request("At most %d different items per side." % TRADE_MAX_ITEM_TYPES)

    # MERGED BY ITEM ID BEFORE ANYTHING IS CHECKED. Two entries naming the same
    # item would otherwise each be checked against the full holding and both
    # pass, and the pair would then be taken at execution.
    merged = {}
    for entry in raw_items:
        if not isinstance(entry, dict):
            return bad_request("each item must be an object")
        item_id = str(entry.get("item_id", "")).strip()
        if not item_id or len(item_id) > 64:
            return bad_request("item_id must be 1-64 characters")
        if item_id not in gamedata.ITEMS:
            return bad_request("No such item '%s'." % item_id)
        raw_qty = entry.get("quantity", 1)
        if isinstance(raw_qty, bool) or not isinstance(raw_qty, int) or raw_qty <= 0:
            return bad_request("quantity must be a positive integer")
        merged[item_id] = merged.get(item_id, 0) + raw_qty

    # CHECKED NOW AND AGAIN AT EXECUTION. Here so the panel can refuse
    # immediately; there because this answer is already stale by the time
    # anybody presses confirm.
    held = {}
    for row in db.execute(
        "SELECT item_id, SUM(quantity) AS total FROM carry_items"
        " WHERE user_id = ? AND slot = ? GROUP BY item_id", (user_id, slot)
    ).fetchall():
        held[row["item_id"]] = int(row["total"])
    for item_id, quantity in merged.items():
        if held.get(item_id, 0) < quantity:
            return bad_request("You only have %d x %s." % (held.get(item_id, 0), item_id))

    db.execute("DELETE FROM trade_items WHERE trade_id = ? AND side = ?",
               (trade["trade_id"], side))
    for item_id, quantity in sorted(merged.items()):
        db.execute(
            "INSERT INTO trade_items (trade_id, side, item_id, quantity) VALUES (?, ?, ?, ?)",
            (trade["trade_id"], side, item_id, quantity),
        )
    db.execute("UPDATE trades SET %s_gold = ? WHERE trade_id = ?" % side,
               (raw_gold, trade["trade_id"]))
    _trade_touch(db, trade["trade_id"])
    db.commit()

    return _trade_payload(db, db.execute(
        "SELECT * FROM trades WHERE trade_id = ?", (trade["trade_id"],)).fetchone()), 200


@app.post("/api/trade/confirm")
@require_auth
def trade_confirm():
    """
    Confirm your side; the trade executes when both have
    ---
    tags:
      - Trade
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200: {description: The trade, or its result if that confirm completed it}
      400: {description: Somebody cannot cover the tax or no longer holds their offer}
      404: {description: You are not in a trade}
      409: {description: The trade closed underneath you, or a backpack is full}
    """
    user_id = g.user["id"]
    db = get_db()
    trade = _trade_find_open(db, user_id)
    if trade is None or _trade_expired(trade):
        return {"error": "Not Found", "message": "You are not in a trade."}, 404

    side = _trade_side_of(trade, user_id)
    db.execute("UPDATE trades SET %s_confirmed = 1, updated_at = ? WHERE trade_id = ?" % side,
               (int(time.time()), trade["trade_id"]))

    # RE-READ, don't reason about the row we started from: the other side may
    # have confirmed between our read and our write.
    trade = db.execute("SELECT * FROM trades WHERE trade_id = ?",
                       (trade["trade_id"],)).fetchone()

    if int(trade["a_confirmed"]) and int(trade["b_confirmed"]):
        return _execute_trade(db, trade)

    db.commit()
    return _trade_payload(db, trade), 200


@app.post("/api/trade/cancel")
@require_auth
def trade_cancel():
    """
    Call off the trade
    ---
    tags:
      - Trade
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200: {description: Cancelled, or nothing to cancel}
    """
    db = get_db()
    trade = _trade_find_open(db, g.user["id"])
    if trade is None:
        return {"cancelled": False}, 200
    # EITHER SIDE MAY CANCEL AT ANY TIME, including after confirming. A confirm
    # that cannot be withdrawn is a trap: the other side changes nothing, waits,
    # and you are committed.
    db.execute("UPDATE trades SET state = 'cancelled', updated_at = ? WHERE trade_id = ?",
               (int(time.time()), trade["trade_id"]))
    db.commit()
    return {"cancelled": True, "trade_id": trade["trade_id"]}, 200


# How many players the board returns. Two hundred, not ten.
#
# A TOP TEN IS NOT A SCOREBOARD FOR A GAME WITH TWO HUNDRED PLAYERS - it is a
# wall with ten names on it that nobody else ever appears on, which makes the
# whole "your contribution counts" idea into something only the richest ten
# players ever see evidence of. At 200 the board IS the population, so paying
# 80% of everything you own buys a line somebody can actually find.
#
# THE CALLER'S OWN LINE IS SEPARATE AND ALWAYS PRESENT, so this number is about
# who else you can see, never about whether you can see yourself.
#
# PAST A FEW THOUSAND PLAYERS THIS NEEDS PAGING, not a bigger constant: the
# response is one row per player and the client builds a node per row. The seam
# is here - an offset parameter and a total count - and it is worth doing the
# day the number of accounts starts with a comma.
KINGDOM_BOARD_SIZE = 200


# The most names the nearby list will return. A cap rather than paging: this
# feeds a panel you glance at to find somebody to trade with, and a list longer
# than this is one nobody reads to the end of anyway.
NEARBY_LIMIT = 20


@app.get("/api/players/nearby")
@require_auth
def players_nearby():
    """
    Other players in your area, right now
    ---
    tags:
      - Players
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: query
        name: slot
        type: integer
        required: true
        description: Which of your characters is asking
    responses:
      200:
        description: Who else is here
      400:
        description: Bad slot
      404:
        description: No character in that slot
    """
    # AREA, NOT DISTANCE, AND THE NAME OF THIS ROUTE OVERSTATES IT.
    #
    # The server stores which AREA a character is in (saves.area, written by
    # PUT /api/save) and nothing finer. There is no position on the server and
    # no heartbeat carrying one, so "nearby" here means "in the same area and
    # online" - which is the honest maximum today and is genuinely what a trade
    # panel needs, because you cannot see another player at all yet.
    #
    # WHEN PRESENCE ARRIVES, this is the seam: the query gains a distance test
    # and the response gains a position, and the client does not change shape.
    user_id = g.user["id"]

    slot = parse_slot(request.args.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    db = get_db()
    me = db.execute(
        "SELECT area FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    if me is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    area = me["area"]

    # ONLINE IS A LIVE SESSION, not a recent save. A save happens on a timer
    # and on scene changes, so "saved recently" lists somebody who alt-tabbed
    # away an hour ago; an unexpired token is the closest thing the server has
    # to "that client is still there". DISTINCT because one account may hold
    # several valid tokens - two machines, or a login that did not log out.
    rows = db.execute(
        """
        SELECT DISTINCT users.username AS username,
               saves.slot   AS slot,
               saves.name   AS name,
               saves.level  AS level
          FROM saves
          JOIN users    ON users.id = saves.user_id
          JOIN sessions ON sessions.user_id = saves.user_id
         WHERE saves.area = ?
           AND saves.user_id != ?
           AND sessions.expires_at > ?
      ORDER BY saves.level DESC, users.username ASC
         LIMIT ?
        """,
        (area, user_id, int(time.time()), NEARBY_LIMIT),
    ).fetchall()

    return {
        "area": area,
        # Said out loud so the client never has to guess how precise this is,
        # and so a panel written against it can show the right wording.
        "precision": "area",
        "players": [
            {
                "username": r["username"],
                "slot": int(r["slot"]),
                "name": r["name"],
                "level": int(r["level"]),
            }
            for r in rows
        ],
    }, 200


@app.get("/api/economy/kingdom")
@require_auth
def economy_kingdom():
    """
    The Kingdom Tax board: what the realm has taken, and from whom
    ---
    tags:
      - Economy
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200:
        description: Totals, the top contributors, and the caller's own standing
      401:
        description: Missing, invalid or expired token
    """
    # OPEN TO EVERY PLAYER, unlike /api/economy/supply next door, and the
    # difference is what each one reveals. Supply is the total money in the
    # world - an operational figure, and one that tells a gold seller exactly
    # how much a currency is worth. This is the opposite: it is the gold that no
    # longer exists, which is a scoreboard rather than an asset.
    #
    # DERIVED, NEVER COUNTED. There is no kingdom_total column anywhere, on
    # purpose. A running counter is a second place the truth lives, and the
    # first time it disagrees with the ledger there is no way to tell which one
    # is lying. Summing the rows is slower and always right, and the row count
    # here grows one per taxed trade - that is an index, not a problem.
    db = get_db()
    user_id = g.user["id"]

    # CONTRIBUTION IS EVERY GOLD DESTROYED, not only the trade tax, and that is
    # a deliberate reading of "what have I given the kingdom". A player who
    # never trades but keeps the potion shop in business has still taken gold
    # out of the world, and a board that ignored them would say their spending
    # did not count. The breakdown below keeps the trade tax visible on its own
    # so nothing is hidden by the aggregate.
    rows = db.execute(
        """
        SELECT users.username AS username,
               -SUM(gold_ledger.delta) AS given
          FROM gold_ledger
          JOIN users ON users.id = gold_ledger.user_id
         WHERE gold_ledger.delta < 0
      GROUP BY gold_ledger.user_id
      ORDER BY given DESC
        """
    ).fetchall()

    board = [{"username": r["username"], "contributed": int(r["given"]), "lusions": 0}
             for r in rows]

    # LUSIONS GIVEN, FROM THEIR OWN LEDGER.
    #
    # A revive paid in lusions is a contribution too - the player gave the
    # kingdom something scarce - and counting only the gold one would have made
    # the board quietly reward whichever currency somebody happened to hold.
    #
    # A SECOND COLUMN, NOT A SECOND TERM. They are not added together, because
    # there is no exchange rate between them and inventing one would be the
    # board deciding what a lusion is worth. Twenty lusions and twenty gold are
    # different sacrifices; it shows both and lets the reader weigh them.
    by_user_lusions = {}
    for row in db.execute(
        "SELECT users.username AS username, -SUM(lusion_ledger.delta) AS given"
        "  FROM lusion_ledger"
        "  JOIN users ON users.id = lusion_ledger.user_id"
        " WHERE lusion_ledger.delta < 0"
        " GROUP BY lusion_ledger.user_id"
    ).fetchall():
        by_user_lusions[row["username"]] = int(row["given"])

    for entry in board:
        entry["lusions"] = by_user_lusions.pop(entry["username"], 0)

    # Anyone who has ONLY ever given lusions is not in the gold board at all,
    # and leaving them out would be a scoreboard that forgot the players who
    # paid in the rarer currency.
    for username, given in by_user_lusions.items():
        board.append({"username": username, "contributed": 0, "lusions": given})

    # Ranked by gold, with lusions breaking the tie. Gold leads because it is
    # what the coffers are measured in; lusions decide who sits higher among
    # players who gave the same amount of it.
    board.sort(key=lambda entry: (entry["contributed"], entry["lusions"]), reverse=True)

    # RANK IS COMPUTED HERE, ONCE, and sent with every row.
    #
    # The client used to derive it from the row's position in the list, which
    # is only the same thing while the list is complete and nothing ties. It is
    # neither: the list is capped at KINGDOM_BOARD_SIZE, and two players who
    # gave the same amount are joint, not consecutive.
    #
    # COMPETITION RANKING - 1, 1, 3 - because a tie means neither player beat
    # the other and printing 1 and 2 says one of them did. The gap after a tie
    # is the honest consequence: two people finished ahead of whoever is third.
    #
    # The key is the same pair the sort used, so the ordering and the ranking
    # can never disagree about what counts as equal.
    rank = 0
    previous_key = None
    for index, entry in enumerate(board):
        key = (entry["contributed"], entry["lusions"])
        if key != previous_key:
            rank = index + 1
            previous_key = key
        entry["rank"] = rank

    total = sum(entry["contributed"] for entry in board)
    total_lusions = sum(entry["lusions"] for entry in board)

    by_reason = {}
    for row in db.execute(
        "SELECT reason, -SUM(delta) AS given FROM gold_ledger"
        " WHERE delta < 0 GROUP BY reason ORDER BY given DESC"
    ).fetchall():
        by_reason[row["reason"]] = int(row["given"])

    lusions_by_reason = {}
    for row in db.execute(
        "SELECT reason, -SUM(delta) AS given FROM lusion_ledger"
        " WHERE delta < 0 GROUP BY reason ORDER BY given DESC"
    ).fetchall():
        lusions_by_reason[row["reason"]] = int(row["given"])

    # THE CALLER'S OWN LINE, ALWAYS, even at rank 4000. A board that shows only
    # the top ten tells everybody else they are not on it; the whole point of
    # the rule is that ordinary play adds to the total, so ordinary play has to
    # be able to see itself in it.
    me = g.user["username"]
    mine = {"username": me, "contributed": 0, "lusions": 0, "rank": None}
    for entry in board:
        if entry["username"] == me:
            # entry["rank"], not the loop index: those differ the moment two
            # players are tied above you, and the number a player checks for
            # themselves is the one that most needs to be right.
            mine = dict(entry)
            break

    return {
        "total": total,
        "total_lusions": total_lusions,
        "by_reason": by_reason,
        "lusions_by_reason": lusions_by_reason,
        "contributors": len(board),
        "top": board[:KINGDOM_BOARD_SIZE],
        "you": mine,
    }, 200


@app.get("/api/economy/supply")
@require_auth
@require_role("dev")
def economy_supply():
    """
    ---
    get:
      summary: Gold supply and the ledger invariant
      description: >
        What the server recorded creating and destroying, against what players
        actually hold. balanced=false means gold moved without going through
        gold_delta() - a bug or a dupe - and drift says by how much.
      responses:
        200:
          description: The current supply figures
        404:
          description: >
            Not staff. require_role answers 404 rather than 403 on purpose - a
            403 confirms the route exists to someone who may not know it does.
    """
    # DEV AND ABOVE, not mod. This is the audit instrument: it says how much
    # currency exists and whether the books are straight, and a moderator needs
    # neither. See role_at_least() for the ladder.
    return gold_supply(get_db())


@app.get("/api/character")
@require_auth
def read_character():
    """
    Everything one character is, in a single call
    ---
    tags:
      - Character
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: query
        name: slot
        type: integer
        required: true
    responses:
      200:
        description: Identity, vitals, backpack and skills together
      400:
        description: Bad slot
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    # ONE CALL, NOT FOUR. Loading a character used to mean status, then
    # inventory, then skills, then bank - four round trips on a screen the
    # player is staring at. They are all keyed on the same (user_id, slot) and
    # none of them is useful without the others.
    slot = parse_slot(request.args.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    user_id = g.user["id"]
    status = status_payload(user_id, slot)
    if status is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    row = get_db().execute(
        # equipment IS IN THE SELECT. sqlite3.Row raises IndexError for a
        # column the query did not fetch, so adding a key to the payload above
        # without adding it here turns a read into a 500. Third time that trap
        # has been stepped in today; it is always the same two lines apart.
        "SELECT class_id, name, area, active_pet_id, bank_gold, equipment "
        "FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()

    return {
        "slot": slot,
        "class_id": row["class_id"],
        "name": row["name"],
        "area": row["area"],
        "active_pet_id": row["active_pet_id"],
        "status": status,
        "inventory": inventory_payload(user_id, slot),
        # EQUIPMENT TRAVELS WITH THE BAG, and the two being split across
        # endpoints is what cost a character its gear on every login.
        #
        # GET /api/save carried equipment and no inventory; this carried
        # inventory and no equipment. The client merges both, and
        # characterdata.gd::_sanitize_character_slot() reconciled the gear
        # against the bag - so it ran with equipment from one response and an
        # EMPTY inventory from the other, concluded the player owned none of
        # what they were wearing, and cleared every slot. Two endpoints each
        # correct on its own, and a reconciliation firing when only one half
        # had arrived.
        #
        # The prune is going away with the move to server-owned equipment, but
        # a read that answers half a question is worth fixing on its own terms:
        # the next thing to ask this endpoint what a character is wearing
        # should not have to know to ask somewhere else.
        "equipment": _stored_json(row["equipment"], {}),
        "skills": skills_payload(user_id, slot),
    }, 200


@app.put("/api/character/inventory")
@require_auth
def write_inventory():
    """
    Replace a character's backpack
    ---
    tags:
      - Character
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, inventory]
          properties:
            slot: {type: integer, example: 0}
            inventory:
              type: array
              description: "Positional; null for an empty cell. At most 20 entries."
    responses:
      200:
        description: The backpack as stored
      400:
        description: Bad slot, oversized array, or a malformed entry
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    if not _slot_exists(user_id, slot):
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    cells = payload.get("inventory")
    if not isinstance(cells, list):
        return bad_request("inventory must be an array")
    if len(cells) > INVENTORY_CAPACITY:
        return bad_request("inventory has %d entries, capacity is %d" % (len(cells), INVENTORY_CAPACITY))

    # THE SHARED VALIDATOR, not a second copy of it. This loop used to be
    # written out here as well as in _parse_positional_items(), twenty identical
    # lines in two places - so a rule added to one of them (the stack ceiling
    # was exactly that) would silently not apply to the other.
    #
    # It validates the whole array before the caller writes any of it: a bad
    # entry at index 17 must not leave the first seventeen stored.
    items, error = _parse_positional_items(cells, "inventory")
    if error is not None:
        return error

    # SERVER AUTHORITY. Trim any item claimed beyond what the server granted,
    # down to the granted amount. Honest syncs (reorders, using/dropping, and
    # items the server itself wrote via loot/bank/staff) pass through unchanged;
    # a modified client's fabricated excess is dropped here. See
    # _reconcile_inventory() and SECURITY_NOTES.md (E-1).
    items = _reconcile_inventory(g.user, slot, items)

    parsed = [(user_id, slot, index, item_id, quantity)
              for index, item_id, quantity in items]

    db = get_db()
    # DELETE then INSERT, in one transaction. A replace has no upsert form: cells
    # the client cleared have to disappear, and an upsert would leave them.
    # sqlite3 opens a transaction implicitly on the first write and holds it
    # until commit, so the table is never observably empty.
    db.execute("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (user_id, slot))
    if parsed:
        db.executemany(
            "INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, ?, ?, ?, ?)",
            parsed,
        )
    db.commit()

    return {"slot": slot, "inventory": inventory_payload(user_id, slot)}, 200


@app.put("/api/character/skills")
@require_auth
def write_skills():
    """
    Replace a character's skills
    ---
    tags:
      - Character
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, skills]
          properties:
            slot: {type: integer, example: 0}
            skills:
              type: object
              description: '{"attack": {"level": 12, "xp": 340}, ...}'
    responses:
      200:
        description: The skills as stored
      400:
        description: Bad slot, unknown skill, or a malformed level/xp
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    if not _slot_exists(user_id, slot):
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    skills = payload.get("skills")
    if not isinstance(skills, dict):
        return bad_request("skills must be an object")

    parsed = []
    for skill_id, values in skills.items():
        name = str(skill_id).strip().lower()
        if name not in VALID_SKILLS:
            # Refused rather than ignored. A silently dropped skill is a level
            # the player earned and cannot see, with nothing anywhere to say so.
            return bad_request("Unknown skill '%s'. Known skills: %s" % (name, ", ".join(sorted(VALID_SKILLS))))

        if not isinstance(values, dict):
            return bad_request("skills.%s must be an object with level and xp" % name)

        level = parse_stat(values.get("level", 1))
        xp = parse_stat(values.get("xp", 0))
        if level is None or level < 1:
            return bad_request("skills.%s.level must be a positive integer" % name)
        if xp is None:
            return bad_request("skills.%s.xp must be a non-negative integer" % name)

        parsed.append((user_id, slot, name, level, xp))

    # SKILL CEILING. Skills have no server-side grant path yet, so the server
    # cannot prove a level was earned - but it can refuse an impossible one. A
    # non-staff claim over the cap is clamped rather than rejected, so a single
    # over-cap skill never discards the whole (otherwise honest) sync. Staff are
    # exempt for the same reason as the backpack. See SECURITY_NOTES.md (E-2).
    if not role_at_least(g.user, "mod"):
        capped, hits = [], []
        for (uid, s, name, level, xp) in parsed:
            if level > MAX_SKILL_LEVEL or xp > MAX_SKILL_XP:
                hits.append(name)
                level = min(level, MAX_SKILL_LEVEL)
                xp = min(xp, MAX_SKILL_XP)
            capped.append((uid, s, name, level, xp))
        if hits:
            print("[SKILLS] capped over-ceiling skills for %s slot %d: %s"
                  % (g.user["username"], slot, ", ".join(sorted(hits))))
        parsed = capped

    # THE SERVER OWNS FISHING AND COOKING NOW - the client does not get to
    # replace them.
    #
    # This endpoint replaces the whole skill set with whatever arrives, which is
    # fine for the four skills the client still grants. It is fatal for the two
    # it does not: /api/fishing/catch and /api/cooking/cook write those rows
    # against items the server consumed, and the client's very next sync would
    # overwrite that with its own stale figure. The grant would survive exactly
    # until the player picked up a potion.
    #
    # Dropped silently rather than refused. A client that has not been updated
    # still sends all six every save, and 400-ing an otherwise honest sync over
    # a field it is not allowed to set would break saving entirely.
    parsed = [row for row in parsed if row[2] not in SERVER_OWNED_SKILLS]

    db = get_db()
    db.execute(
        "DELETE FROM skills WHERE user_id = ? AND slot = ? AND skill_id NOT IN (%s)"
        % ",".join("?" * len(SERVER_OWNED_SKILLS)),
        (user_id, slot, *sorted(SERVER_OWNED_SKILLS)),
    )
    if parsed:
        db.executemany(
            "INSERT INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, ?, ?, ?, ?)",
            parsed,
        )
    db.commit()

    return {"slot": slot, "skills": skills_payload(user_id, slot)}, 200


# =============================================================================
# CONSUMING AN ITEM
# =============================================================================
#
# WHY THIS ENDPOINT EXISTS AT ALL, when the client could simply drop the item
# from its next inventory sync and be done with it.
#
# ItemData has carried `required_level` and `required_skill` for a while, and
# inventoryscreen.gd checked both before letting a use through. That check runs
# on the player's machine, which made it a courtesy rather than a rule - and its
# own comment said so, and named the day it would stop being good enough:
#
#     "It becomes load-bearing the day trade exists."
#
# Trade exists. Before it, the only person the gate protected against was an
# honest player using something they had not earned, because an item you hold is
# an item you found. Now a level 1 character can be HANDED a level 22 potion by
# a friend, and the only thing between them is a check they control.
#
# WHAT THIS CLOSES AND WHAT IT DOES NOT. It makes the requirement real and the
# destruction real: the item leaves the bag because the server took it, not
# because the client announced it. It does NOT stop a determined cheat, because
# PUT /api/player/status still accepts any hp up to the server's derived maximum
# - a patched client heals without drinking anything. That is a separate and
# larger finding, and this endpoint is what makes it addressable: once healing
# has an authorised source, an unexplained rise becomes a question the server
# can ask. See consume_grants in init_db().
#
# THE EFFECT IS STILL THE CLIENT'S. The server does not know what a potion
# restores - `restore_target` and `restore_amount` are not in gamedata.json - so
# it authorises and destroys, and the client applies. That is deliberately where
# the line sits today rather than a pretence that it sits further along.

# =============================================================================
# EQUIPMENT IS A MOVE, NOT A REFERENCE
# =============================================================================
# WHAT THIS REPLACES, and why it had to become an endpoint rather than a
# client gesture.
#
# Equipping used to be a POINTER: `equipment` named an item that was also
# sitting in the backpack, and characterdata.gd's prune_equipment() cleared
# any slot naming something the bag did not contain. The bag was the record of
# ownership and the doll was a view onto it - which is why an equipped chest
# piece showed up twice on screen, once in each panel.
#
# That arrangement carried the ownership check for free: the bag is reconciled
# against what the server granted (E-1), so gear pointing into it was
# reconciled too. The moment equipping MOVES the item out of the bag, both of
# those stop applying, and `equipment` becomes a client-written field that
# nothing reconciles - in the column combat reads to decide what a hit is
# worth. That is E-1 again, one column over, and it is the reason this is a
# server endpoint instead of two lines in the UI.
#
# SO THE SERVER MOVES IT. Taking from the bag IS the ownership check: you
# cannot equip what _take_from_backpack() cannot find. Nothing needs
# reconciling afterwards because the client never gets to assert the result.
# Same shape as /api/character/consume, for the same reason - the client asks,
# the server moves, the client renders the answer.

def _equipment_of(row):
    """The stored map, parsed. Always a dict, even for a row that predates it."""
    return _stored_json(row["equipment"], {})


def _equip_row(user_id, slot):
    return get_db().execute(
        "SELECT class_id, level, equipment FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()


@app.post("/api/character/equip")
@require_auth
def equip_item():
    """
    Move one item from the backpack onto the character
    ---
    tags: [Character]
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, item_id]
          properties:
            slot:    {type: integer, example: 0}
            item_id: {type: string,  example: cobaltrobe}
    responses:
      200: {description: Equipped. Returns the new equipment map and bag layout}
      400: {description: Bad slot or item_id}
      403: {description: Wrong slot for this item, wrong class, or too low a level}
      404: {description: No character in that slot, or you are not carrying that}
      409: {description: The piece coming off has nowhere to go}
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    item_id = str(payload.get("item_id", "")).strip()
    if not item_id or len(item_id) > 64:
        return bad_request("item_id must be 1-64 characters")

    row = _equip_row(user_id, slot)
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    # THE SLOT IS DERIVED FROM THE ITEM, never taken from the request. There is
    # then nothing for the two to disagree about, and equipmentpanel.gd already
    # works this way - "the square's name is ignored on purpose".
    target = gamedata.equip_slot_for(item_id)
    if not target:
        if item_id not in gamedata.ITEMS:
            return bad_request("No such item '%s'." % item_id)
        return {"error": "Forbidden", "message": "That is not equipment."}, 403

    # THE SERVER'S OWN LEVEL AND CLASS, never the body's. Same reason
    # parse_equipment() takes them as arguments rather than reading a payload:
    # a client that could name its own level could wear anything.
    verdict = gamedata.equip_check(item_id, target, str(row["class_id"]), int(row["level"]))
    if not verdict.get("ok", False):
        reason = verdict.get("reason", "")
        if reason == "unknown":
            return bad_request("No such item '%s'." % item_id)
        if reason == "notgear":
            return {"error": "Forbidden", "message": "That is not equipment."}, 403
        if reason == "class":
            return {"error": "Forbidden",
                    "message": "Your class cannot wear that.",
                    "allowed": verdict.get("allowed", [])}, 403
        if reason == "level":
            return {"error": "Forbidden",
                    "message": "That needs level %d." % int(verdict.get("needs", 0)),
                    "needs": int(verdict.get("needs", 0))}, 403
        return {"error": "Forbidden", "message": "That cannot be equipped."}, 403

    worn = _equipment_of(row)
    coming_off = str(worn.get(target, ""))

    # ---- everything above this line is validation; everything below commits --

    db = get_db()

    # TAKE FIRST, THEN PUT BACK, and the order is load-bearing rather than
    # tidy. Taking the new item may free the cell the old one needs - swapping
    # your only chest piece for another should always work, and it only does
    # if the bag is measured after the incoming item has left it.
    if not _take_from_backpack(user_id, slot, item_id, 1):
        return {"error": "Not Found", "message": "You are not carrying that."}, 404

    if coming_off:
        if _add_to_backpack(user_id, slot, coming_off, 1) is None:
            # ALL OR NOTHING. The take above is undone rather than left
            # standing - an item that left the bag and reached neither the
            # doll nor the floor is the worst outcome available here.
            _add_to_backpack(user_id, slot, item_id, 1)
            return {"error": "Conflict",
                    "message": "Your bag is full - nowhere to put what you are wearing."}, 409

    worn[target] = item_id
    db.execute(
        "UPDATE saves SET equipment = ?, updated_at = ? WHERE user_id = ? AND slot = ?",
        (json.dumps(worn), int(time.time()), user_id, slot),
    )
    db.commit()

    return {
        "slot": slot,
        "equipped": item_id,
        "equip_slot": target,
        "unequipped": coming_off,
        "equipment": worn,
        # THE AUTHORITATIVE LAYOUT, handed back so the client renders what the
        # server did rather than guessing at it. /api/loot/take does the same.
        "inventory": inventory_payload(user_id, slot),
    }


@app.post("/api/character/unequip")
@require_auth
def unequip_item():
    """
    Move one item off the character and back into the backpack
    ---
    tags: [Character]
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, equip_slot]
          properties:
            slot:       {type: integer, example: 0}
            equip_slot: {type: string,  example: chest}
    responses:
      200: {description: Unequipped. Returns the new equipment map and bag layout}
      400: {description: Bad slot or equip_slot}
      404: {description: No character in that slot, or nothing worn there}
      409: {description: The bag is full}
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    target = str(payload.get("equip_slot", "")).strip().lower()
    if target not in EQUIP_SLOTS:
        return bad_request("equip_slot must be one of: %s" % ", ".join(EQUIP_SLOTS))

    row = _equip_row(user_id, slot)
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    worn = _equipment_of(row)
    item_id = str(worn.get(target, ""))
    if not item_id:
        # 404 RATHER THAN A CHEERFUL 200. Taking off a slot that is already
        # empty means the client and the server disagree about what is worn,
        # and answering "done" would let that disagreement persist silently.
        return {"error": "Not Found", "message": "Nothing is worn there."}, 404

    # ---- everything above this line is validation; everything below commits --

    if _add_to_backpack(user_id, slot, item_id, 1) is None:
        return {"error": "Conflict",
                "message": "Your bag is full."}, 409

    worn.pop(target, None)
    db = get_db()
    db.execute(
        "UPDATE saves SET equipment = ?, updated_at = ? WHERE user_id = ? AND slot = ?",
        (json.dumps(worn), int(time.time()), user_id, slot),
    )
    db.commit()

    return {
        "slot": slot,
        "unequipped": item_id,
        "equip_slot": target,
        "equipment": worn,
        "inventory": inventory_payload(user_id, slot),
    }


@app.post("/api/character/consume")
@require_auth
def character_consume():
    """
    Use one consumable from the backpack
    ---
    tags:
      - Character
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, item_id]
          properties:
            slot:    {type: integer, example: 0}
            item_id: {type: string,  example: smallhealthpotion}
    responses:
      200:
        description: The item was destroyed; apply its effect
      400:
        description: Bad slot or item_id
      403:
        description: Character level or skill level too low
      404:
        description: That slot is empty, or you are not carrying that item
      409:
        description: That item is not something you can consume
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    item_id = str(payload.get("item_id", "")).strip()
    if not item_id or len(item_id) > 64:
        return bad_request("item_id must be 1-64 characters")

    db = get_db()
    row = db.execute(
        "SELECT level FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    # WHAT YOU HOLD IS CHECKED BEFORE WHETHER YOU MAY USE IT, and the order is
    # not arbitrary. "You are not carrying that" is true regardless of level,
    # and answering 403 first would tell a client which items it does NOT hold
    # are gated and at what level - the same shape as the 404-not-403 decision
    # in require_role(), for the same reason.
    held = db.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM carry_items "
        "WHERE user_id = ? AND slot = ? AND item_id = ?",
        (user_id, slot, item_id),
    ).fetchone()[0]
    if int(held) <= 0:
        return {"error": "Not Found", "message": "You are not carrying that."}, 404

    levels = {
        skill: int(value["level"])
        for skill, value in skills_payload(user_id, slot).items()
    }

    # VALID_SKILLS IS PASSED IN, and it is the whole difference between a gate
    # and a bypass. Skill rows are written when a skill is first trained, so a
    # new character has none at all - and without this set, "cooking is missing
    # from your levels" reads as "cooking is not a skill" and every fresh
    # account walks through every skill gate in the game. See consume_check().
    verdict = gamedata.consume_check(
        item_id, int(row["level"]), levels, known_skills=VALID_SKILLS)

    if verdict.get("unknown_skill"):
        # Loud and permissive, exactly as the client is. A skill name the game
        # does not have is a typo in the .tres, and refusing would take a
        # working item away from every player over an editor slip. Logged,
        # because a gate that has quietly stopped gating is the outcome worth
        # avoiding.
        app.logger.error(
            "consume: '%s' requires unknown skill '%s' - check its .tres",
            item_id, verdict["unknown_skill"],
        )

    if not verdict["ok"]:
        reason = verdict["reason"]
        if reason == "level":
            return {
                "error": "Forbidden",
                "message": "You need level %d." % verdict["needs"],
            }, 403
        if reason == "skill":
            return {
                "error": "Forbidden",
                "message": "You need %s level %d." % (
                    verdict["skill"].capitalize(), verdict["needs"]),
            }, 403
        # "unknown" and "nottype" are both "the server will not destroy this",
        # and neither answer tells the caller anything it did not itself send.
        return {"error": "Conflict", "message": "That cannot be used."}, 409

    # ---- everything above this line is validation; everything below commits --

    if not _take_from_backpack(user_id, slot, item_id, 1):
        # Only reachable if the stack vanished between the count above and here.
        # All-or-nothing, so nothing is written.
        return {"error": "Not Found", "message": "You are not carrying that."}, 404

    # WHAT THIS AUTHORISED, not merely that it happened. The healing reconciler
    # reads these rows, and with only a timestamp to go on the cheapest potion
    # in the game explained a rise of any size in any pool. The amount comes
    # from the server's own catalogue, never from the request.
    target, amount = gamedata.restore_for(item_id)
    db.execute(
        "INSERT INTO consume_grants (user_id, slot, item_id, target, amount, at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, slot, item_id, target, int(amount), int(time.time())),
    )
    db.commit()

    return {
        "slot": slot,
        "item_id": item_id,
        "consumed": 1,
        "remaining": int(held) - 1,
        # Returned so the client applies the server's number rather than its
        # own copy. It still owns the effect - hp is client-written, see E-9 -
        # but there is no reason for the two to disagree about the size of it.
        "restores": {"target": target, "amount": int(amount)},
    }, 200


# =============================================================================
# REVIVING
# =============================================================================
#
# DEATH WAS FREE, AND THE WHOLE TRANSACTION HAPPENED ON THE PLAYER'S MACHINE.
# gameover.gd read the lusion balance, compared it to revive_cost, called
# add_account_lusions(-cost), wrote full hp/mana/stamina into the save slot
# itself, and reloaded the world. The server saw a smaller lusion figure and a
# healthier character arrive by PUT, and believed both.
#
# It believed them because it had no choice: PUT /api/account/lusions stored
# whatever number it was sent (see that endpoint for the finding), so there was
# no authoritative balance to charge against in the first place.
#
# THE PENALTY FOR DYING IS THE ONLY THING LUSIONS DO. That is what makes this
# worth an endpoint rather than a shrug - a currency with one sink IS that sink,
# and a client that can write the balance has removed the cost of death from
# the game.
#
# ALSO THE OTHER HALF OF THE HEALING RECONCILER. _reconcile_heals()
# flags a rise that regeneration and consumables cannot account for, and an
# honest revive is exactly such a rise - a jump from 0 to full in no time at
# all. Before this endpoint existed the server could not tell that from a cheat,
# so every legitimate revive was going to land in the log as a false positive.
# Now it leaves a row like a potion does.

@app.post("/api/character/revive")
@require_auth
def character_revive():
    """
    Revive a dead character, paying in lusions or in gold
    ---
    tags:
      - Character
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot]
          properties:
            slot: {type: integer, example: 0}
            pay:  {type: string,  example: gold, enum: [lusions, gold]}
    responses:
      200:
        description: Revived; the cost was taken and the character restored
      400:
        description: Bad slot
      402:
        description: Not enough lusions, or no gold at all
      404:
        description: No character in that slot
      409:
        description: That character is not dead
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    # TWO WAYS TO PAY, and gold is the one everybody always has.
    #
    # Lusions come from duplicate pets, so a player who has never rolled one had
    # no way back at all - which made the death screen a dead end rather than a
    # decision. Banked gold is the pile death cannot touch, and spending it here
    # is the only thing that makes "bound" mean anything.
    #
    # CHECKED HERE, WITH THE SLOT, because it is a question about the REQUEST
    # rather than about the character. Validating it further down meant a
    # nonsense currency on a living character answered "that character is not
    # dead" - a true sentence about the wrong problem, and a test caught it.
    method = str(payload.get("pay", "lusions")).strip().lower()
    if method not in ("lusions", "gold"):
        return bad_request("pay must be 'lusions' or 'gold'")

    db = get_db()
    row = db.execute(
        "SELECT class_id, level, hp, gold FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    # ONLY THE DEAD MAY BE REVIVED, and the check is against the server's own
    # stored hp rather than anything the client asserts. Without it this becomes
    # "pay 20 lusions for a full heal at any moment", which is a different
    # feature with different balance consequences that nobody designed.
    if int(row["hp"] or 0) > 0:
        return {
            "error": "Conflict",
            "message": "That character is not dead.",
        }, 409

    account = _ensure_account(user_id)

    carry_gold = int(row["gold"] or 0)
    bank_gold = int(account["bank_gold"])

    if method == "gold":
        # A SHARE OF EVERYTHING, not a price - see GameConstants.REVIVE_GOLD_RATE
        # for why a flat figure cannot sting the same at level 3 and level 30.
        cost = gamedata.revive_gold_cost(carry_gold + bank_gold)
        if cost <= 0:
            return {
                "error": "Payment Required",
                "message": "You have no gold to pay with.",
                "cost": 0,
                "gold": 0,
            }, 402
    else:
        cost = int(gamedata.CONSTANTS.get("revive_cost", 20))
        have = int(account["lusions"])
        if have < cost:
            # 402 rather than 403: this is not a permission, it is a price. The
            # client shows the shortfall, so both numbers go back.
            return {
                "error": "Payment Required",
                "message": "Reviving costs %d lusions; you have %d." % (cost, have),
                "cost": cost,
                "lusions": have,
            }, 402

    # ---- everything above this line is validation; everything below commits --

    derived = gamedata.max_stats_for(row["class_id"], int(row["level"]))
    if derived is None:
        # A class the server has no curve for. Refusing is right: the
        # alternative is inventing a maximum and writing it into the save.
        app.logger.error("revive: no stat curve for class '%s'", row["class_id"])
        return {"error": "Conflict", "message": "That character cannot be revived."}, 409

    if method == "gold":
        # CARRY FIRST, THEN BANK. The carry pile is the one true death would
        # have taken anyway, so it pays before the pile that survives - what is
        # left over ends up where it is safe, which is the point of banking it.
        #
        # DESTROYED, NOT MOVED. Through gold_delta() and bank_gold_delta() so
        # the burn lands in the ledger in the same transaction as the balance.
        # A bare UPDATE here would break the supply invariant on the first
        # revive anybody paid for - the same mistake E-8 was.
        from_carry = min(carry_gold, cost)
        from_bank = cost - from_carry
        if from_carry:
            gold_delta(db, user_id, slot, -from_carry, "revive",
                       "revive from slot %d" % slot)
        if from_bank:
            bank_gold_delta(db, user_id, -from_bank, "revive",
                            "revive from slot %d" % slot)
    else:
        # THROUGH THE LEDGER, not a bare UPDATE, for the same reason the gold
        # burn is: a balance says what you have, and the kingdom board needs to
        # know what you GAVE. It is also the only record that a lusion was ever
        # spent on anything.
        lusion_delta(db, user_id, slot, -cost, "revive",
                     "revive from slot %d" % slot)

    # THE SCORE IS WHAT DYING TOOK, and it is the one number in the game that
    # goes up when you lose.
    #
    # BOTH PAYMENT PATHS COUNT. The 20-lusion revive keeps everything you were
    # carrying; the gold revive burns 80% of what you hold. They cost very
    # different things and both are a price paid for dying, so both score.
    #
    # LUSIONS AND GOLD ARE COUNTED 1:1, and that is a decision rather than an
    # oversight - they are different currencies and this adds them into one
    # figure. It is the simplest rule that is not arbitrary, and if the two
    # should ever weigh differently this is the single line to change. Naming
    # it here because an unweighted sum of two currencies is exactly the kind
    # of thing that later reads as a bug nobody noticed.
    #
    # IN THE SAME TRANSACTION as the payment above and the heal below, so a
    # crash cannot bank the score for a revive that did not happen or take the
    # payment for one that scored nothing.
    db.execute(
        "UPDATE accounts SET score = score + ? WHERE user_id = ?",
        (int(cost), user_id),
    )

    db.execute(
        "UPDATE saves SET hp = ?, mana = ?, stamina = ?, "
        "max_hp = ?, max_mana = ?, max_stamina = ?, updated_at = ? "
        "WHERE user_id = ? AND slot = ?",
        (derived["max_hp"], derived["max_mana"], derived["max_stamina"],
         derived["max_hp"], derived["max_mana"], derived["max_stamina"],
         int(time.time()), user_id, slot),
    )

    # The marker the healing reconciler reads. Same table as a potion, because
    # it answers the same question - "did the server authorise this rise" - and
    # a second table would be a second place to forget to look.
    db.execute(
        "INSERT INTO consume_grants (user_id, slot, item_id, at) VALUES (?, ?, ?, ?)",
        (user_id, slot, REVIVE_GRANT_ID, int(time.time())),
    )
    db.commit()

    account_after = _ensure_account(user_id)
    return {
        "slot": slot,
        "revived": True,
        "paid_with": method,
        "cost": cost,
        "lusions": int(account_after["lusions"]),
        "bank_gold": int(account_after["bank_gold"]),
        "status": status_payload(user_id, slot),
    }, 200


# =============================================================================
# LOOT BAGS
# =============================================================================
#
# THE LAST THING THE CLIENT GETS TO ASSERT.
#
# /api/combat/kill already decides what drops. But the bag then existed only in
# the client's world: it spawned a node, the player walked over it, items landed
# in the local inventory, and the next save simply TOLD the server what was now
# being carried. The server rolled a potion and had no idea whether you picked it
# up, dropped it, or invented forty more.
#
# That is why `gold` is still on the client-asserted list next to
# SERVER_OWNED_STATS. Closing it means the bag has to exist HERE:
#
#   1. A kill that drops something creates a bag with an unguessable id.
#   2. The client renders it, but the contents it renders are a copy.
#   3. Taking an item is a REQUEST. The server checks the bag is yours, that it
#      still holds that item, and moves it into your backpack or your purse.
#
# After this, gold and carried items only change through an endpoint that
# verified where they came from.
#
# WHY POSITION-KEYED, LIKE EVERY OTHER CONTAINER HERE
# ---------------------------------------------------
# Same reason as carry_items and bank_items: "take the item in slot 2" is a
# request the server can answer exactly once, whereas "take a smallhealthpotion"
# is ambiguous when the bag holds two stacks of them, and a client that sends it
# twice would be asking for a duplication bug.

# How long the server will honour a bag after it is created.
#
# Deliberately far longer than the client's LOOT_BAG_DESPAWN_SECONDS (45). The
# client despawning the node is a display decision; if the two disagree the
# player should lose the bag to the ANIMATION, never to a 410 from a server that
# expired it a moment early. This exists to stop bags accumulating forever, not
# to enforce the despawn.
LOOT_BAG_TTL_SECONDS = 600

# Matches BANK_CAPACITY's role for the backpack. The client's grid is 20 cells.
CARRY_CAPACITY = INVENTORY_CAPACITY


# What a fishing spot takes as bait, one per fish landed.
#
# NAMED HERE RATHER THAN SENT BY THE CLIENT. fishingspot.gd has its own
# bait_item_id export so a spot can want something else, but that is a display
# and early-refusal concern - a client that named its own bait would name the
# cheapest thing it had.
FISHING_BAIT_ID = "fishingworm"

# Rods are matched on this suffix, matching fishingspot.gd's _best_rod_tier().
FISHING_ROD_SUFFIX = "fishingrod"

# Cast rate limit, the same token bucket /api/combat/kill uses.
#
# SIZED FOR THE REAL ACTIVITY. A cast is a 2-6.5s wait plus a bite window, so an
# honest player lands well under one fish every three seconds and never sees
# this. It exists so a patched client cannot turn the endpoint into a printing
# press by removing the wait.
CAST_BUCKET_CAPACITY = 6.0
CAST_TOKENS_PER_SECOND = 0.4

# The loot panel's grid, in cells. lootbaginventory.gd's LOOT_SIZE.
#
# The position IS the grid cell on both sides - that is what lets the client
# send "take cell 2" and mean the thing the player is looking at. A bag rolled
# with more entries than the panel can show would put loot behind a cell that
# does not exist, and the player would never be able to ask for it. Rolls are
# currently capped at five (one gold pile, three item slots, one pet), so this
# is a guard against a future enemy config, not a live condition.
LOOT_BAG_CAPACITY = 6

# The item_id the client uses for the lusion pile - lootbaginventory.gd's
# LUSION_ITEM_ID. There is no constant for it in gamedata.json because the game
# has never needed to name it; the server does, because it is what a duplicate
# pet turns into.
LUSIONS_ITEM_ID = "lusions"


def _create_loot_bag(user_id, slot, enemy_id, contents):
    """
    Store a rolled bag and return (bag_id, stored_contents).

    stored_contents carries the POSITION of every entry, because that position
    is the only thing /api/loot/take accepts. Returning the roll unstamped and
    letting the client infer position from array order would work right up until
    something reordered or dropped an entry between here and there - and the
    failure would be the client asking for the wrong item, silently.

    bag_id is secrets.token_urlsafe, not a row counter: a sequential id would
    let a client ask for bag 4,102 and find out what someone else killed.
    """
    if not contents:
        return "", []

    if len(contents) > LOOT_BAG_CAPACITY:
        # Logged rather than raised. A player mid-fight should not lose a kill
        # because an enemy was configured to drop seven things; they lose the
        # overflow, and the log says why.
        app.logger.warning(
            "loot: %s rolled %d entries, truncating to the panel's %d cells",
            enemy_id, len(contents), LOOT_BAG_CAPACITY,
        )
        contents = contents[:LOOT_BAG_CAPACITY]

    stored = [
        {"position": position, "item_id": entry["item_id"], "quantity": int(entry["quantity"])}
        for position, entry in enumerate(contents)
    ]

    bag_id = secrets.token_urlsafe(16)
    now = int(time.time())

    db = get_db()
    db.execute(
        "INSERT INTO loot_bags (bag_id, user_id, slot, enemy_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (bag_id, user_id, slot, enemy_id, now),
    )
    db.executemany(
        "INSERT INTO loot_bag_items (bag_id, position, item_id, quantity) VALUES (?, ?, ?, ?)",
        [(bag_id, e["position"], e["item_id"], e["quantity"]) for e in stored],
    )

    # Opportunistic cleanup. Bags are small and expire on their own terms, so
    # this runs here rather than on a schedule - a player who is killing things
    # is exactly the player generating rows worth clearing.
    db.execute(
        "DELETE FROM loot_bags WHERE created_at < ?", (now - LOOT_BAG_TTL_SECONDS,)
    )

    db.commit()
    return bag_id, stored


def _owns_item(user_id, slot, item_id):
    """
    True when this account already holds item_id in the backpack or the bank.

    Only pets ask. The bank is account-scoped and the backpack is per-character,
    which is deliberate: a pet is a collectible, and having caught one on your
    warrior should stop it dropping again for your mage - which is exactly what
    the bank half covers, since that is where a collection ends up.
    """
    if get_db().execute(
        "SELECT 1 FROM carry_items WHERE user_id = ? AND slot = ? AND item_id = ? LIMIT 1",
        (user_id, slot, item_id),
    ).fetchone():
        return True
    return get_db().execute(
        "SELECT 1 FROM bank_items WHERE user_id = ? AND item_id = ? LIMIT 1",
        (user_id, item_id),
    ).fetchone() is not None


def _take_from_backpack(user_id, slot, item_id, quantity):
    """
    Remove quantity of item_id from the backpack. Returns True, or False when
    the player does not hold that many - in which case NOTHING is written.

    The mirror of _add_to_backpack() below, and all-or-nothing for the same
    reason: a half-completed consume is an ingredient that left the bag without
    producing anything.

    HIGHEST POSITION FIRST, so a partly-used stack is emptied before a full one
    is broken into. That keeps the bag tidy across a long cooking run instead of
    leaving a trail of one-item stacks.
    """
    quantity = int(quantity)
    if quantity <= 0:
        return True

    db = get_db()
    rows = db.execute(
        "SELECT position, quantity FROM carry_items "
        "WHERE user_id = ? AND slot = ? AND item_id = ? ORDER BY position DESC",
        (user_id, slot, item_id),
    ).fetchall()

    held = sum(int(r["quantity"]) for r in rows)
    if held < quantity:
        return False

    remaining = quantity
    for row in rows:
        if remaining <= 0:
            break
        position = int(row["position"])
        have = int(row["quantity"])
        taken = min(have, remaining)
        remaining -= taken

        if taken >= have:
            # The row is deleted rather than set to 0: carry_items carries a
            # CHECK (quantity > 0) and an empty cell is an absent row.
            db.execute(
                "DELETE FROM carry_items WHERE user_id = ? AND slot = ? AND position = ?",
                (user_id, slot, position),
            )
        else:
            db.execute(
                "UPDATE carry_items SET quantity = ? "
                "WHERE user_id = ? AND slot = ? AND position = ?",
                (have - taken, user_id, slot, position),
            )

    return True


def _best_rod_tier(user_id, slot):
    """Highest fishing rod tier in this character's backpack, or 0 for none."""
    rows = get_db().execute(
        "SELECT DISTINCT item_id FROM carry_items WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchall()

    best = 0
    for row in rows:
        item_id = row["item_id"]
        if not item_id.endswith(FISHING_ROD_SUFFIX):
            continue
        item = gamedata.ITEMS.get(item_id)
        if item is None:
            continue
        best = max(best, int(item["tier"]))
    return best


# How long a kill report is kept. Long enough to see a slow, patient cheat in
# the log; short enough that the table does not grow forever unattended. The
# same reasoning and the same self-pruning shape as LOGIN_LOG_RETENTION_SECONDS.
KILL_LOG_RETENTION_SECONDS = 60 * 60 * 24 * 14


def _record_kill(db, user_id, slot, enemy_id, rewards, level_at, now):
    """Write one row for this claimed kill, and prune the old ones.

    NO COMMIT - this shares combat_kill's transaction on purpose, so a kill is
    never banked without its record, and a record never exists for a kill that
    was rolled back.
    """
    db.execute(
        "INSERT INTO kill_reports (user_id, slot, enemy_id, xp, attack_xp, level_at, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, slot, str(enemy_id)[:64], int(rewards.get("xp", 0)),
         int(rewards.get("attack_xp", 0)), int(level_at), now),
    )
    db.execute("DELETE FROM kill_reports WHERE at < ?",
               (now - KILL_LOG_RETENTION_SECONDS,))


def _grant_skill_xp(user_id, slot, skill_id, gained):
    """
    Add XP to one skill and return (level, xp, levels_gained).

    THE FIRST SERVER-SIDE SKILL GRANT IN THIS FILE. Until now the skills table
    was written only by PUT /api/character/skills, which replaces the whole set
    with whatever the client sends - see the note there about why fishing and
    cooking are now carved out of that.

    AN UPSERT, NOT A DELETE-THEN-INSERT. write_skills() replaces every row
    because it is given every row; this is given one skill and must leave the
    rest of the character's progress exactly where it was.

    THE THRESHOLD IS RECOMPUTED, NOT STORED. The skills table is (level, xp)
    with no xp_to_next column, unlike saves. Deriving it each grant from the
    same curve the client uses costs one exponent and removes a column that
    could disagree with the level sitting next to it.
    """
    gained = max(int(gained), 0)
    if gained <= 0:
        row = get_db().execute(
            "SELECT level, xp FROM skills WHERE user_id = ? AND slot = ? AND skill_id = ?",
            (user_id, slot, skill_id),
        ).fetchone()
        return (int(row["level"]), int(row["xp"]), 0) if row else (1, 0, 0)

    db = get_db()
    row = db.execute(
        "SELECT level, xp FROM skills WHERE user_id = ? AND slot = ? AND skill_id = ?",
        (user_id, slot, skill_id),
    ).fetchone()

    level = int(row["level"]) if row else 1
    xp = int(row["xp"]) if row else 0

    level, xp, _next, levels_gained = gamedata.apply_xp(
        level, xp, gamedata.xp_needed_for_skill_level(skill_id, level), gained
    )

    level = min(level, MAX_SKILL_LEVEL)
    xp = min(xp, MAX_SKILL_XP)

    db.execute(
        """
        INSERT INTO skills (user_id, slot, skill_id, level, xp)
             VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, slot, skill_id)
          DO UPDATE SET level = excluded.level, xp = excluded.xp
        """,
        (user_id, slot, skill_id, level, xp),
    )

    return level, xp, levels_gained


def _add_to_backpack(user_id, slot, item_id, quantity):
    """
    Put quantity of item_id into the backpack the way the CLIENT would, and
    return the cells touched - or None when it does not fit.

    TOPS UP EXISTING STACKS FIRST, then fills empty cells. That is not a nicety:
    the response hands `inventory` back as the authoritative layout, and the
    client applies it. If this dropped every pickup into the lowest free cell
    while InventoryContainer.add_stack_partial() merged onto a part-used stack,
    the two would lay the same bag out differently and the player would watch
    their potions split across cells on every loot.

    Writes nothing unless the whole quantity fits. A half-completed pickup is
    the shape of bug that ends with an item in neither the bag nor the bag.
    """
    definition = gamedata.ITEMS.get(item_id, {})
    stackable = bool(definition.get("stackable", False))
    max_stack = int(definition.get("max_stack", 1)) if stackable else 1
    if max_stack < 1:
        max_stack = 1

    rows = get_db().execute(
        "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ? ORDER BY position",
        (user_id, slot),
    ).fetchall()

    occupied = {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in rows}

    remaining = int(quantity)
    writes = []  # (position, item_id, new_quantity)

    if stackable:
        for position in sorted(occupied):
            if remaining <= 0:
                break
            held_id, held_qty = occupied[position]
            if held_id != item_id or held_qty >= max_stack:
                continue
            room = max_stack - held_qty
            moved = min(room, remaining)
            writes.append((position, item_id, held_qty + moved))
            remaining -= moved

    for position in range(CARRY_CAPACITY):
        if remaining <= 0:
            break
        if position in occupied:
            continue
        moved = min(max_stack, remaining)
        writes.append((position, item_id, moved))
        occupied[position] = (item_id, moved)
        remaining -= moved

    if remaining > 0:
        return None

    db = get_db()
    for position, written_id, written_qty in writes:
        db.execute(
            """
            INSERT INTO carry_items (user_id, slot, position, item_id, quantity)
                 VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, slot, position)
              DO UPDATE SET item_id = excluded.item_id, quantity = excluded.quantity
            """,
            (user_id, slot, position, written_id, written_qty),
        )

    return [position for position, _, _ in writes]


@app.post("/api/loot/take")
@require_auth
def take_loot():
    """
    Take one item out of a loot bag
    ---
    tags:
      - Loot
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [bag_id, position]
          properties:
            bag_id:   {type: string,  example: k3Jx9_QpZ2mNvRt1}
            position: {type: integer, example: 0}
    responses:
      200:
        description: What was taken, and the state it landed in
      400:
        description: Bad bag_id or position
      404:
        description: No such bag, not yours, or that cell is already empty
      409:
        description: Backpack full - the item stays in the bag
      410:
        description: The bag has expired
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    bag_id = str(payload.get("bag_id", "")).strip()
    if not bag_id or len(bag_id) > 64:
        return bad_request("bag_id must be 1-64 characters")

    # Bounded by the BAG's capacity, not the backpack's. They were the same
    # number by accident, and a cell index from a 6-cell grid validated against
    # a 20-cell one is a check that reads as if it is doing something.
    position = parse_stat(payload.get("position"))
    if position is None or position >= LOOT_BAG_CAPACITY:
        return bad_request("position must be an integer 0-%d" % (LOOT_BAG_CAPACITY - 1))

    db = get_db()

    # SCOPED TO user_id IN THE QUERY ITSELF, not checked afterwards. A bag that
    # belongs to someone else and a bag that does not exist return exactly the
    # same 404 - there is nothing to learn from asking.
    bag = db.execute(
        "SELECT slot, created_at FROM loot_bags WHERE bag_id = ? AND user_id = ?",
        (bag_id, user_id),
    ).fetchone()
    if bag is None:
        return {"error": "Not Found", "message": "No such loot bag."}, 404

    if int(bag["created_at"]) < int(time.time()) - LOOT_BAG_TTL_SECONDS:
        db.execute("DELETE FROM loot_bags WHERE bag_id = ?", (bag_id,))
        db.commit()
        return {"error": "Gone", "message": "That loot bag has expired."}, 410

    item = db.execute(
        "SELECT item_id, quantity FROM loot_bag_items WHERE bag_id = ? AND position = ?",
        (bag_id, position),
    ).fetchone()
    if item is None:
        # Already taken, or never there. Same answer either way, and it is the
        # answer that makes a duplicate request harmless: the second one finds
        # nothing and changes nothing.
        return {"error": "Not Found", "message": "Nothing in that slot of the bag."}, 404

    slot = int(bag["slot"])
    item_id = item["item_id"]
    quantity = int(item["quantity"])
    definition = gamedata.ITEMS.get(item_id, {})
    kind = definition.get("type_name", "")

    # WHAT WAS IN THE BAG vs WHAT THE PLAYER ACTUALLY GETS. Those are the same
    # thing for everything except a pet you already own, and the client needs
    # both: item_id/quantity to know which cell to clear, granted_* to know what
    # to say in the notice.
    result = {"bag_id": bag_id, "position": position, "item_id": item_id, "quantity": quantity}
    granted_id = item_id
    granted_qty = quantity

    # A DUPLICATE PET BECOMES LUSIONS, AND THAT DECISION MOVED HERE.
    #
    # lootbaginventory.gd used to make it, in _load_contents(), by checking the
    # local inventory and the local bank. Both of those are now views of rows
    # this process owns, so the client was checking a copy to decide what a pet
    # was worth - and a client that decides what it is owed is the thing this
    # whole endpoint exists to stop.
    #
    # The panel still DISPLAYS the substitution so the player sees lusions in
    # the bag rather than a pet that turns into lusions. That is cosmetic. This
    # is the grant.
    if kind == "PET" and _owns_item(user_id, slot, item_id):
        kind = "CURRENCY"
        granted_id = LUSIONS_ITEM_ID
        granted_qty = int(gamedata.CONSTANTS.get("dupe_pet_lusions", 20))
        result["duplicate_pet"] = True

    if kind == "CURRENCY":
        # CURRENCY RESOLVES INTO A BALANCE RATHER THAN A BACKPACK CELL. This is
        # the whole point of the exercise: gold entering the game now goes
        # through a statement the server wrote, against a bag the server rolled.
        if granted_id in (CONSTANTS_GOLD_SMALL, CONSTANTS_GOLD_LARGE):
            # THE ONLY PLACE GOLD IS CREATED. Through gold_delta() rather than a
            # bare UPDATE so the mint lands in the ledger in the same
            # transaction as the balance - see gold_ledger in init_db(). If a
            # second mint site is ever added it goes through here too, or the
            # invariant stops meaning anything.
            gold_delta(
                db, user_id, slot, granted_qty,
                "loot", "%s x%d from bag %s" % (granted_id, granted_qty, bag_id),
            )
            result["credited"] = "gold"
        else:
            # Lusions, and anything else account-scoped that shows up later.
            #
            # THE ONLY PLACE LUSIONS ARE CREATED, and it goes through
            # lusion_delta() so the mint lands in lusion_ledger in the same
            # transaction as the balance - the same discipline the gold mint
            # two branches up has followed since the ledger existed.
            _ensure_account(user_id)
            lusion_delta(db, user_id, slot, granted_qty, "duplicate_pet",
                         "%s converted from bag %s" % (granted_id, bag_id))
            result["credited"] = "lusions"
    else:
        written = _add_to_backpack(user_id, slot, granted_id, granted_qty)
        if written is None:
            # 409, and the item stays where it is. Refusing beats dropping it on
            # the floor: the player can make room and ask again. Nothing was
            # written - _add_to_backpack is all-or-nothing.
            return {
                "error": "Conflict",
                "message": "Your backpack is full (%d slots)." % CARRY_CAPACITY,
            }, 409

        result["credited"] = "inventory"
        result["carry_positions"] = written

    result["granted_item_id"] = granted_id
    result["granted_quantity"] = granted_qty

    # Removed only after it has landed somewhere. If the insert above had failed
    # the transaction carries the delete with it, so an item cannot evaporate
    # between the two.
    db.execute(
        "DELETE FROM loot_bag_items WHERE bag_id = ? AND position = ?", (bag_id, position)
    )

    remaining = db.execute(
        "SELECT COUNT(*) FROM loot_bag_items WHERE bag_id = ?", (bag_id,)
    ).fetchone()[0]
    if remaining == 0:
        db.execute("DELETE FROM loot_bags WHERE bag_id = ?", (bag_id,))

    db.commit()

    result["bag_empty"] = remaining == 0

    # THE BALANCES GO BACK AS TOTALS, NOT AS THE DELTA THAT WAS JUST APPLIED.
    #
    # The client still pushes `gold` on every save, so a client that added the
    # delta to its own figure and got it wrong once would overwrite this row
    # with the wrong number on the very next save - and the loss would look like
    # nothing at all. Handing back the balance it landed on means a client that
    # misses a response is corrected by the next one it does get.
    result["status"] = status_payload(user_id, slot)
    result["inventory"] = inventory_payload(user_id, slot)
    result["lusions"] = int(_ensure_account(user_id)["lusions"])
    return result, 200


# =============================================================================
# FISHING AND COOKING
# =============================================================================
# BOTH BUILT TO THE RULE IN docs/inventoryauthority.md, which named them before
# either existed: "Anything that creates, destroys or moves an item is a server
# endpoint." The client sends that it fished, never what it caught; it sends
# which fish it wants cooked, never whether that fish burned.
#
# Both copy /api/loot/take's shape exactly - validate and 4xx before the first
# write, then one transaction ending in a single commit, then hand back the
# totals rather than the delta so a client that drops a response is corrected by
# the next one it gets.


@app.post("/api/fishing/catch")
@require_auth
def fishing_catch():
    """
    Land a fish
    ---
    tags:
      - Fishing
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot]
          properties:
            slot: {type: integer, example: 0}
    responses:
      200:
        description: What was caught, and the state it landed in
      400:
        description: Bad slot
      403:
        description: No rod, or no bait
      404:
        description: That slot is empty
      409:
        description: Backpack full
      429:
        description: Casting faster than the bucket allows
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    row = get_db().execute(
        "SELECT last_cast_at, cast_tokens FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    # THE ROD AND THE BAIT ARE CHECKED AGAINST carry_items, not against anything
    # the client said. fishingspot.gd runs the same two checks so it can refuse
    # early and explain why, but that copy is a courtesy - this one decides.
    rod_tier = _best_rod_tier(user_id, slot)
    if rod_tier <= 0:
        return {"error": "Forbidden", "message": "You need a fishing rod."}, 403

    bait_held = get_db().execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM carry_items "
        "WHERE user_id = ? AND slot = ? AND item_id = ?",
        (user_id, slot, FISHING_BAIT_ID),
    ).fetchone()[0]
    if int(bait_held) <= 0:
        return {"error": "Forbidden", "message": "You need worms for bait."}, 403

    # Refill then spend, exactly as /api/combat/kill does.
    now_ms = int(time.time() * 1000)
    elapsed_seconds = max(now_ms - int(row["last_cast_at"]), 0) / 1000.0
    tokens = min(
        CAST_BUCKET_CAPACITY,
        float(row["cast_tokens"]) + elapsed_seconds * CAST_TOKENS_PER_SECOND,
    )
    if tokens < 1.0:
        return {
            "error": "Too Many Requests",
            "message": "Casts are arriving faster than %g per second." % CAST_TOKENS_PER_SECOND,
        }, 429
    tokens -= 1.0

    skills = skills_payload(user_id, slot)
    fishing_level = int(skills.get("fishing", {}).get("level", 1))

    catch = gamedata.roll_fishing_catch(rod_tier, fishing_level)
    if catch is None:
        # No FISH-typed items in the catalogue at all. A server misconfiguration
        # rather than a player problem, and it must not eat their bait.
        return {
            "error": "Conflict",
            "message": "There is nothing to catch here.",
        }, 409

    # ---- everything above this line is validation; everything below commits --
    db = get_db()

    if not _take_from_backpack(user_id, slot, FISHING_BAIT_ID, 1):
        # Re-checked inside the transaction. The count above was read before the
        # roll, and two casts racing would both have seen the last worm.
        return {"error": "Forbidden", "message": "You need worms for bait."}, 403

    written = _add_to_backpack(user_id, slot, catch["item_id"], catch["quantity"])
    if written is None:
        # The bait is not spent: nothing has been committed, and returning here
        # abandons the transaction rather than charging for a fish that had
        # nowhere to go.
        return {
            "error": "Conflict",
            "message": "Your backpack is full (%d slots)." % CARRY_CAPACITY,
        }, 409

    level, xp, levels_gained = _grant_skill_xp(user_id, slot, "fishing", catch["xp"])

    db.execute(
        "UPDATE saves SET last_cast_at = ?, cast_tokens = ? WHERE user_id = ? AND slot = ?",
        (now_ms, tokens, user_id, slot),
    )
    db.commit()

    return {
        "item_id": catch["item_id"],
        "quantity": catch["quantity"],
        "xp": catch["xp"],
        "carry_positions": written,
        "levelled_up": levels_gained > 0,
        "fishing_level": level,
        "status": status_payload(user_id, slot),
        "inventory": inventory_payload(user_id, slot),
        "skills": skills_payload(user_id, slot),
    }, 200


@app.post("/api/cooking/cook")
@require_auth
def cooking_cook():
    """
    Cook one raw fish
    ---
    tags:
      - Cooking
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, item_id]
          properties:
            slot:    {type: integer, example: 0}
            item_id: {type: string,  example: rawmudfish}
    responses:
      200:
        description: What happened, and the state it landed in
      400:
        description: Bad slot or item_id
      403:
        description: Cooking level too low
      404:
        description: That slot is empty, or you are not carrying that fish
      409:
        description: Backpack full, or the item is not cookable
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)
    if not _slot_exists(user_id, slot):
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    item_id = str(payload.get("item_id", "")).strip()
    if not item_id or len(item_id) > 64:
        return bad_request("item_id must be 1-64 characters")

    # THE INPUT IS NAMED BY THE CLIENT AND NOTHING ELSE IS. Which of their own
    # fish to cook is a choice, not a claim - the recipe, the level gate and the
    # burn roll are all read from data the server holds.
    held = get_db().execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM carry_items "
        "WHERE user_id = ? AND slot = ? AND item_id = ?",
        (user_id, slot, item_id),
    ).fetchone()[0]
    if int(held) <= 0:
        return {"error": "Not Found", "message": "You are not carrying that."}, 404

    skills = skills_payload(user_id, slot)
    cooking_level = int(skills.get("cooking", {}).get("level", 1))

    outcome = gamedata.roll_cook(item_id, cooking_level)
    if not outcome["ok"]:
        if outcome["reason"] == "level":
            return {
                "error": "Forbidden",
                "message": "You need cooking level %d." % outcome["needs"],
            }, 403
        return {
            "error": "Conflict",
            "message": "That cannot be cooked.",
        }, 409

    # ---- everything above this line is validation; everything below commits --
    db = get_db()

    if not _take_from_backpack(user_id, slot, item_id, 1):
        return {"error": "Not Found", "message": "You are not carrying that."}, 404

    written = []
    if not outcome["burnt"]:
        written = _add_to_backpack(user_id, slot, outcome["output"], 1)
        if written is None:
            # The raw fish is not consumed - nothing is committed.
            return {
                "error": "Conflict",
                "message": "Your backpack is full (%d slots)." % CARRY_CAPACITY,
            }, 409

    level, xp, levels_gained = _grant_skill_xp(user_id, slot, "cooking", outcome["xp"])
    db.commit()

    return {
        "item_id": item_id,
        "burnt": outcome["burnt"],
        "output": outcome["output"],
        "xp": outcome["xp"],
        "carry_positions": written,
        "levelled_up": levels_gained > 0,
        "cooking_level": level,
        "status": status_payload(user_id, slot),
        "inventory": inventory_payload(user_id, slot),
        "skills": skills_payload(user_id, slot),
    }, 200


@app.get("/api/loot/bag")
@require_auth
def read_loot_bag():
    """
    What is still in a loot bag
    ---
    tags:
      - Loot
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: query
        name: bag_id
        type: string
        required: true
    responses:
      200:
        description: The bag's remaining contents
      404:
        description: No such bag, or not yours
      401:
        description: Missing, invalid or expired token
    """
    # Exists so a client that reconnects, or one that is unsure whether a take
    # landed, can ask rather than guess. A bag the server has already emptied is
    # a 404, which is the same answer as never having existed - and the right
    # one, because in both cases there is nothing to collect.
    bag_id = str(request.args.get("bag_id", "")).strip()
    if not bag_id:
        return bad_request("bag_id is required")

    user_id = g.user["id"]
    bag = get_db().execute(
        "SELECT slot, enemy_id, created_at FROM loot_bags WHERE bag_id = ? AND user_id = ?",
        (bag_id, user_id),
    ).fetchone()
    if bag is None:
        return {"error": "Not Found", "message": "No such loot bag."}, 404

    rows = get_db().execute(
        "SELECT position, item_id, quantity FROM loot_bag_items WHERE bag_id = ? ORDER BY position",
        (bag_id,),
    ).fetchall()

    return {
        "bag_id": bag_id,
        "slot": int(bag["slot"]),
        "enemy_id": bag["enemy_id"],
        "contents": [
            {"position": int(r["position"]), "item_id": r["item_id"], "quantity": int(r["quantity"])}
            for r in rows
        ],
    }, 200


# =============================================================================
# ENTRY POINT
# =============================================================================
#
# LAST LINE OF THE FILE, AND IT HAS TO BE.
#
# app.run() BLOCKS. Every @app.route below it would be parsed but never
# executed, so those routes would simply not exist on the running server - with
# no error anywhere, because nothing is wrong with the code. The client would
# get a 404 from an endpoint that is plainly right there in the file.
#
# This block sat in the middle for a while after endpoints were appended below
# it, and /api/combat/kill was silently unregistered the whole time. Keep new
# routes ABOVE this line.

if __name__ == "__main__":
    # DEBUG IS OFF UNLESS EXPLICITLY ASKED FOR. debug=True enables the Werkzeug
    # interactive debugger, which turns any unhandled exception on a reachable
    # build into arbitrary code execution on the box that holds elusion.db and
    # its password hashes. Turn it on for a local dev loop with ELUSION_DEBUG=1;
    # anything anyone else can reach leaves it off. See SECURITY_NOTES.md (E-4).
    #
    # DEBUGGER_PERMITTED, not the raw flag: the same decision the import-time
    # refusal and the per-request guard make, so there is one rule and three
    # places it is enforced rather than three rules.
    app.run(debug=DEBUGGER_PERMITTED)
