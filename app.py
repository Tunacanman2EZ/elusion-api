# THE .env IS READ BEFORE ANYTHING ELSE, and that ordering is the fix for a real
# bug. It used to be read by a helper four hundred lines down, after
# `import gamedata` had taken ELUSION_GAMEDATA, DB_PATH had taken ELUSION_DB and
# TRUSTED_PROXY_HOPS had taken ELUSION_TRUSTED_PROXIES - so those three in a
# .env were silently ignored. Nothing goes above these two lines. See envfile.py;
# test_deploy.py boots this file with only a .env and checks every setting.
import envfile
envfile.load()

from flask import Flask, request, g, has_request_context
from flask.json.provider import DefaultJSONProvider
from flasgger import Swagger
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

import gamedata
import json
import collections
import sqlite3
import secrets
import time
import os
import re
import sys
import threading
import unicodedata
import math
import smtplib
import hashlib
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from functools import wraps

app = Flask(__name__)


# A LONE SURROGATE IS REFUSED AT THE DOOR. "\ud800" with no partner is legal
# JSON, and json.loads turns it into a Python str that cannot be encoded as
# UTF-8 - so the first database write that touched it raised, and a chat
# message, a character save or an email address came back as a 500. No
# keyboard types one; a script does. Refused here, once, for every route:
# get_json(silent=True) sees None, exactly like any other body that is not
# JSON, and the route answers 400 in its own words.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _holds_lone_surrogate(value):
    if isinstance(value, str):
        return _LONE_SURROGATE.search(value) is not None
    if isinstance(value, dict):
        return any(_holds_lone_surrogate(k) or _holds_lone_surrogate(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_holds_lone_surrogate(v) for v in value)
    return False


class _StrictJSONProvider(DefaultJSONProvider):
    def loads(self, s, **kwargs):
        parsed = super().loads(s, **kwargs)
        if _holds_lone_surrogate(parsed):
            raise ValueError("a lone UTF-16 surrogate in the JSON body")
        return parsed


app.json = _StrictJSONProvider(app)

# HARD BODY-SIZE CAP. A save can carry ~64 KB per explored area (see
# MAX_EXPLORED_BYTES, saves/services.py), so this sits far above any
# legitimate request while still refusing the multi-megabyte bodies whose
# only purpose is to exhaust
# memory before the JSON is even parsed. Werkzeug returns a clean 413 on its
# own once this is set - no handler ever sees an oversized body.
#
# DELIBERATELY ONE MEGABYTE ABOVE IMAGE_MAX_BYTES. Pictures are the only thing
# here that is legitimately large, and a request just over the picture limit
# should be refused BY THE PICTURE ROUTE, in its own words, naming the real
# number - not by Werkzeug, which cuts the connection before any handler runs
# and can only say "too large". The headroom is what buys that sentence.
app.config["MAX_CONTENT_LENGTH"] = 9 * 1024 * 1024

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
# DEFENSE, AGILITY AND MAGIC NOW HAVE THEIR SERVER-OBSERVED EVENT: /api/skill/train.
# They train on damage taken, distance moved and magic damage dealt - none of
# which involves killing anything, so there was no server-side moment to hang a
# grant on, and a character-level bound would have punished the honest level-1
# who walked far. The client cannot be made to prove those events happened, but
# it CAN be stopped from claiming more than physically possible: /api/skill/train
# takes the raw XP the client accumulated and clamps each skill to a generous
# MAX_TRAIN_XP_PER_SEC x elapsed, then grants it here. "Instant level 99" becomes
# "spend the real hours", the same posture as the kill rate limit - and, exactly
# like attack, the number is now decided and recorded server-side. See E-2.
SERVER_OWNED_SKILLS = {"fishing", "cooking", "attack", "defense", "agility", "magic"}

# SKILL CEILING.
#
# THIS COMMENT USED TO SAY "the three skills above are granted server-side; the
# rest are still the client's to compute and PUT back" - directly above a line
# naming SIX. It was written when the set was three and never revisited, so the
# file contradicted itself by two lines, and the same stale fact had reached
# CLAUDE.md's "Known gaps" and the README's findings table. E-2 has been CLOSED
# since /api/skill/train landed; see SECURITY_NOTES.md.
#
# WHAT THE CAP IS FOR NOW. Nothing a client sends about a skill is stored -
# write_skills() drops every name it accepts, because VALID_SKILLS and
# SERVER_OWNED_SKILLS are the same six. So this ceiling guards no live path
# today. It stays because it is GENERIC: 99 stops a modified client asserting an
# absurd level, which is both a power cheat and a source of integer/DB nonsense,
# and the day a seventh skill is the client's to grant it is the thing standing
# there. The ceiling that IS load-bearing today is in _grant_skill_xp(), which
# caps every server grant.
#
# 99 matches the skill set's RuneScape lineage. A legitimate client never
# reaches it, so capping costs honest players nothing.
MAX_SKILL_LEVEL = 99
MAX_SKILL_XP = 200_000_000

# =============================================================================
# SKILL TRAINING - the server-observed event for defense, agility and magic
# =============================================================================
# These three train on continuous play - damage taken, distance moved, magic
# damage dealt - not on a discrete moment like a kill. So the client accumulates
# the RAW xp it earned and reports it to /api/skill/train, and the server does
# the two things the client cannot be trusted to do: it CLAMPS the report to what
# is physically possible in the elapsed time, and it applies class proficiency.
# It cannot prove the play happened - no more than /api/combat/kill can prove a
# fight did - but it caps the RATE, which turns "claim 99 instantly" into "report
# at the honest ceiling for the real number of hours", and it makes the value
# server-decided and server-recorded instead of client-asserted.
#
# THE CAPS ARE DELIBERATELY GENEROUS. Too tight is the E-2 mistake - clamping an
# honest player to catch a cheat - so each sits well above the fastest real earn
# rate: agility from distance (~2000 px/s -> 6 xp/s) plus sprint (~1 xp/s),
# defense at half the damage a crowd can land, magic at a mage's burst. Tune them
# DOWN only with playtest data showing the honest ceiling is lower; start loose.
MAX_TRAIN_XP_PER_SEC = {
    "agility": 20.0,
    "defense": 100.0,
    "magic": 300.0,
}
# The most a single report may bank, so a client whose last_train_at is 0 (a
# fresh character) or which was offline for hours cannot cash one giant report -
# it gets at most this many seconds of budget. The client reports every few
# seconds, so this is pure slack for an honest one.
MAX_TRAIN_ELAPSED_SECONDS = 60.0

# Class proficiency: the specialty each class trains 50% faster. This is static
# class data that lived only in the client (_set_skill_proficiency); it belongs
# here now that the grant does. Anything unlisted is 1.0.
SKILL_PROFICIENCY = {
    "warrior": {"attack": 1.5},
    "mage":    {"magic": 1.5},
    "healer":  {"magic": 1.5},
    "tank":    {"defense": 1.5},
}
# The skills /api/skill/train grants against. attack, fishing and cooking have
# their own server-observed events already.
TRAINABLE_SKILLS = ("agility", "defense", "magic")


def proficient_amount(class_id, skill_id, raw):
    """XP after the class specialty: int(raw x SKILL_PROFICIENCY), never negative.

    ONE RULE FOR BOTH GRANTS. /api/skill/train applied the table and
    /api/combat/kill did not - and attack trains ONLY at the kill, so the
    warrior's specialty was the one entry in this table nothing honoured. The
    client applied it on screen, so a warrior watched attack climb half again
    as fast as the server recorded and lost the difference at the next login.
    """
    factor = SKILL_PROFICIENCY.get(class_id, {}).get(skill_id, 1.0)
    return int(max(0.0, float(raw)) * factor)

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
# ...or put it in a .env file next to this one, which envfile.load() at the
# very top of this file reads. The variable has to be set in the EXACT shell
# that launches the server, every time, and forgetting is silent: the server
# starts fine, nobody is the owner, and the only symptom is that your debug
# keys stop working. That happened, which is why the boot line below now says
# who the owner is.
#
# Unset means no owner, and every owner check fails closed - a server with no
# configured owner has no owner, rather than everyone being one.


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


# THE CO-OWNERS (game 0.21.0). The owner, 10 Oct: "i would promote allmind to
# owner but there can only be 1 however thats why i want to build another gate
# that allows him to enter", and then "maybe a switch in my gm panel that gives
# him access as long as i leave it on".
#
# TWO KEYS, AND BOTH ARE THE OWNER'S:
#
#   1. WHO MAY BE ONE is named here, in the environment, beside ELUSION_OWNER -
#      ELUSION_CO_OWNERS=AllMind (commas between names, for more than one).
#      For the owner's reason: no request can write it, it is not in
#      elusion.db, and a stolen backup does not carry it. Nothing the game
#      sends can make anybody a co-owner who is not named here.
#   2. WHETHER THEY ARE ONE NOW is the owner's switch on the GM panel's Server
#      tab (POST /api/server/coowners, THE owner only - a co-owner cannot keep
#      it on). Off, which is how a server starts, a named account is the rank
#      its row says and nothing more. On, it is a co-owner: every power the
#      owner has, the GM panel with it, and the owner still above - a co-owner
#      cannot kick, ban, mute, rank, level, roll back or give to the owner,
#      and the owner can do all of those to a co-owner.
#
# A name equal to the owner's is dropped: the owner is already above it.
CO_OWNER_USERNAMES = tuple(
    name for name in (part.strip() for part in os.environ.get("ELUSION_CO_OWNERS", "").split(","))
    if name and not (OWNER_USERNAME and name.casefold() == OWNER_USERNAME.casefold())
)
if CO_OWNER_USERNAMES:
    print("[BOOT] co-owners, while the owner's switch is on: %s" % ", ".join(CO_OWNER_USERNAMES))


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


# After envfile.load() (top of the file), so a debug flag in .env is judged
# exactly like one in the shell - a .env copied from a dev machine is the
# likeliest way to ship one.
_DEBUGGER_REFUSAL = debugger_refusal(os.environ, sys.argv)
if _DEBUGGER_REFUSAL is not None:
    raise SystemExit("[BOOT] refusing to start: " + _DEBUGGER_REFUSAL)
DEBUGGER_PERMITTED = debugger_permitted(os.environ, sys.argv)


# =============================================================================
# OBSERVABILITY - per-endpoint request timing
# =============================================================================
# A server you cannot see is a server you cannot trust to stay fast. This keeps
# a rolling window of every endpoint's response times in memory and serves the
# percentiles at GET /api/metrics (owner only), so a latency regression shows up
# as a number the day it happens instead of as a player complaint next week. It
# is the live counterpart to loadtest.py: that one measures BEFORE you ship,
# this one watches AFTER.
#
# PER WORKER, exactly like the rate limiter. Under `gunicorn -w 4` each worker
# keeps its own window and /api/metrics reports whichever worker answered the
# call - enough to spot a slow endpoint or a latency creep, not a fleet-wide
# total. One shared view would need the counters in Redis or a table; that is a
# deliberate later step, not smuggled in under an in-memory dict.
#
# BOUNDED BY CONSTRUCTION. Each endpoint holds at most METRICS_SAMPLES recent
# timings in a deque that drops the oldest, so memory cannot grow without bound
# however long the process lives or however many routes it serves.
METRICS_SAMPLES = 2048
SLOW_REQUEST_MS = 1000.0

_metrics_lock = threading.Lock()
_metrics = {}
_metrics_started = time.time()


def _metrics_bucket(key):
    bucket = _metrics.get(key)
    if bucket is None:
        bucket = {"dur": collections.deque(maxlen=METRICS_SAMPLES), "n": 0, "err": 0}
        _metrics[key] = bucket
    return bucket


def _pct(ordered, p):
    m = len(ordered)
    return round(ordered[min(m - 1, int(m * p))], 1) if m else 0.0


@app.before_request
def _metrics_start():
    # The FIRST before_request, so even a request refused by a later guard - the
    # debugger block, the JSON-object check, the rate limiter - is still timed.
    g._m_t0 = time.perf_counter()
    return None


@app.after_request
def _metrics_record(resp):
    t0 = g.pop("_m_t0", None)
    if t0 is None:
        return resp
    dur_ms = (time.perf_counter() - t0) * 1000.0
    # request.endpoint is the view's stable name; path is the fallback for a 404
    # that matched no route. The method is included so a GET and a PUT on one
    # path are told apart.
    key = "%s %s" % (request.method, request.endpoint or request.path)
    with _metrics_lock:
        bucket = _metrics_bucket(key)
        bucket["dur"].append(dur_ms)
        bucket["n"] += 1
        if resp.status_code >= 500:
            bucket["err"] += 1
    if dur_ms >= SLOW_REQUEST_MS:
        app.logger.warning("[SLOW] %s took %.0f ms -> %d", key, dur_ms, resp.status_code)
    return resp


def _metrics_summary():
    with _metrics_lock:
        snapshot = [(k, list(v["dur"]), v["n"], v["err"]) for k, v in _metrics.items()]
    endpoints = {}
    total = 0
    for key, durs, n, err in snapshot:
        total += n
        ordered = sorted(durs)
        endpoints[key] = {
            "count": n,
            "error_rate": round(err / n, 4) if n else 0.0,
            "p50_ms": _pct(ordered, 0.50),
            "p95_ms": _pct(ordered, 0.95),
            "p99_ms": _pct(ordered, 0.99),
            "max_ms": round(ordered[-1], 1) if ordered else 0.0,
            "recent_samples": len(ordered),
        }
    return {
        "uptime_seconds": int(time.time() - _metrics_started),
        "total_requests": total,
        "window_per_endpoint": METRICS_SAMPLES,
        "scope": "one worker (per-process); under gunicorn -w N this is that worker's view",
        "endpoints": dict(sorted(endpoints.items(), key=lambda kv: -kv[1]["count"])),
    }


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
    # HTTPS ONLY, FROM NOW ON (game 0.44.0, TOKEN LIFECYCLE): a browser that
    # has reached this API over HTTPS once will not send a token over plain
    # HTTP to it for a year. Only on an HTTPS answer - the header means
    # nothing over HTTP, and a local run is HTTP.
    if request.scheme == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


@app.errorhandler(413)
def _handle_too_large(_exc):
    # WERKZEUG'S OWN 413 IS AN HTML PAGE, and the game parses JSON. Before
    # pictures this never came up - every other body here is a few hundred
    # bytes of JSON - but a player dragging a phone photo into chat will hit
    # it, and "Parse JSON failed" is a useless thing to show them. The number
    # is stated because the only useful thing to say is how much too big.
    return {
        "error": "Payload Too Large",
        "message": "That file is bigger than %d MB." % (
            app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)),
    }, 413


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


# THE ROUTES A REFUSED CLIENT MUST STILL REACH. /api/status is the whole reason
# this list exists: it is how a client finds out it is too old, and gating it
# would mean an outdated build got refused everywhere with no way to learn why -
# a player seeing "something is broken" instead of "update the game". The docs
# paths ride along for the same reason they are exempt from the rate limiter.
#
# NOTE WHAT IS NOT EXEMPT: login and register. An old build is refused at the
# door rather than let in to fail on its next request, because being signed in
# and then getting 426 on everything is a worse experience than being told at
# the login screen, which is the one place the client already knows how to
# display a refusal.
_BUILD_EXEMPT = ("/api/status", "/apidocs", "/flasgger_static", "/apispec")


@app.before_request
def _refuse_outdated_client():
    """Turn away a client older than the minimum, when there is one.

    426 UPGRADE REQUIRED, and it is worth being exact about why not 403. A 403
    says "you may not", which sends a player to check their account. This is not
    about the account at all - the same credentials on a newer build walk
    straight in. 426 is the one status that means "the thing you are speaking
    is too old", which is precisely what happened. Same reasoning that made a
    closed server answer 503 rather than 403.

    DISARMED BY DEFAULT. min_client_build() is 0 until an owner sets it, and 0
    lets everything through - so this hook costs one integer comparison per
    request and changes nothing until the day it is needed.

    IT STOPS AN HONEST OLD BUILD AND NOTHING ELSE, and that has to be said
    plainly because the shape of it looks like a security control and is not.
    The build number is a header, headers are client-controlled, and anyone who
    can edit their client or type a curl command can claim to be any build they
    like. The same posture as the debug-key gate in player.gd, written down for
    the same reason: a defence whose limits are not stated gets relied on past
    them.

    What it is FOR is the protocol. It stops a build that predates a wire change
    from half-working against a server that moved on, which is a correctness
    problem rather than an adversarial one - the player running it is not
    attacking anybody, they just never updated, and today they would get
    confusing failures instead of an answer.

    THAT LIMITATION IS ALSO THE WAY BACK IN. Because the gate runs before
    authentication it has no idea who the owner is, so there is no exemption to
    build - but an owner shut out by their own switch can send the header by
    hand and disarm it. Since 0.44.0 (TOKEN LIFECYCLE) a login is bound to the
    install id it came with, and the switch wants the password typed in the
    last fifteen minutes, so the way back is a fresh login by hand with a
    made-up id (32 or more hex characters), then the switch with the same two
    headers and the token it answered:

        curl -X POST https://host/api/auth/login \\
             -H "X-Elusion-Build: 999" -H "X-Elusion-Install: <id>" \\
             -H "Content-Type: application/json" \\
             -d '{"username": "<owner>", "password": "<password>"}'
        curl -X POST https://host/api/server/minbuild \\
             -H "Authorization: Bearer <token>" \\
             -H "X-Elusion-Build: 999" -H "X-Elusion-Install: <id>" \\
             -H "Content-Type: application/json" -d '{"build": 0}'

    (with staff login codes on, the first answers 202 and mails a code; send
    it again with "code"). A bound token sent WITHOUT its id is theft and ends
    every session, so the game's own token is no use here. None of it needs a
    shell on the box. set_min_build() refuses a minimum above
    CURRENT_CLIENT_BUILD as well, so reaching that state takes deliberate effort
    rather than a typo.
    """
    minimum = min_client_build()
    if minimum <= 0:
        return None
    if any(request.path.startswith(p) for p in _BUILD_EXEMPT):
        return None
    if client_build() >= minimum:
        return None
    return {
        "error": "Upgrade Required",
        "message": "This version of the game is too old to connect. "
                   "Please update to keep playing.",
        # NAMED, BOTH OF THEM. A player can read the numbers back to you and a
        # support conversation becomes one line instead of twenty. Neither is a
        # secret: the minimum is on /api/status already, and a client knows its
        # own build.
        "min_build": minimum,
        "your_build": client_build(),
    }, 426


# =============================================================================
# DATABASE
# =============================================================================

# THE WRITE LOCK: A REQUEST THAT CHANGES SOMETHING HOLDS IT FROM ITS FIRST READ.
#
# Most routes here read a row, work out the new numbers in Python, and write
# them back - `xp = row["xp"] + gained`. sqlite3 only opens the transaction at
# the first WRITE, so the read is outside it, and two requests for the same
# character could both read the old figure; the second write then replaced the
# first. Found by playing: a warrior's slash wave killed two enemies at once,
# the game sent both kill reports together, and one kill's XP was gone. Six
# kills sent together banked 99 XP of the 676 their answers had promised. The
# client added up all 676, levelled up before the server did, refilled its
# mana, and the healing check clamped that as a cheat.
#
# BEGIN IMMEDIATE takes SQLite's one write lock before the first read, so a
# second write request waits for the first to commit (busy_timeout) and then
# reads what it wrote. Readers are not affected - WAL lets them carry on.
#
# EVERY POST/PUT/PATCH/DELETE, NOT A LIST OF THE RISKY ONES. There are some
# seventy write routes and most of them read before they write. A list would be
# right today and wrong for the next route anyone adds; this is right for it
# without anyone remembering. The exceptions are named instead
# (@no_write_lock), and each one is there because it does SLOW work that must
# not hold up every other player's writes - a password hash (about 100 ms of
# scrypt) or fetching a picture from another server (up to six seconds).
#
# A COMMIT DOES NOT LET GO OF IT. Some routes commit part-way and go on reading
# and writing (write_save, the guild and friends routes). _WriteLockedConnection
# takes the lock again straight after each commit, so the rest of the request is
# still covered. Teardown closes the connection, which rolls back that last,
# empty transaction and releases the lock.
#
# test_concurrency.py fires real requests at once from threads against a real
# server and checks nothing was lost: XP, gold between the bank and two
# characters, a potion drunk five times, one loot cell taken twice.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def no_write_lock(view):
    """Mark a write route that must NOT hold the write lock from its first read.

    Only for routes that do slow work before they write - hashing a password,
    fetching a picture. They keep sqlite3's ordinary behaviour (the lock is taken
    at their first write), so whatever they read before that can be stale; none
    of them adds up a balance from what it read."""
    view._elusion_no_write_lock = True
    return view


def _request_takes_write_lock():
    if not has_request_context() or request.method not in WRITE_METHODS:
        return False
    view = app.view_functions.get(request.endpoint)
    return view is not None and not getattr(view, "_elusion_no_write_lock", False)


class _WriteLockedConnection(sqlite3.Connection):
    """A connection that keeps the write lock for the rest of the request after
    a commit. See THE WRITE LOCK above."""
    keep_write_lock = False

    def commit(self):
        super().commit()
        if self.keep_write_lock:
            self.execute("BEGIN IMMEDIATE")


def get_db():
    """One connection per request, closed automatically in teardown."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, factory=_WriteLockedConnection)
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

        # Last, after the PRAGMAs: journal_mode cannot change inside a
        # transaction. See THE WRITE LOCK above.
        if _request_takes_write_lock():
            g.db.keep_write_lock = True
            g.db.execute("BEGIN IMMEDIATE")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# THE PACKAGES' TABLES (19 Oct). A table whose rows one area adds keeps its
# CREATE in that package's schema.py, and init_db() calls each create(), in
# the order it writes out. Imported here, above it, because init_db() runs at
# import time (below) - long before the register() calls further down.
import kill.schema as kill_schema  # noqa: E402
import bank.schema as bank_schema  # noqa: E402
import loot.schema as loot_schema  # noqa: E402
import players.schema as players_schema  # noqa: E402
import trade.schema as trade_schema  # noqa: E402
import saves.schema as saves_schema  # noqa: E402
import world.schema as world_schema  # noqa: E402
import chat.schema as chat_schema  # noqa: E402
import moderation.schema as moderation_schema  # noqa: E402
import friends.schema as friends_schema  # noqa: E402
import guilds.schema as guilds_schema  # noqa: E402
import parties.schema as parties_schema  # noqa: E402


def init_db():
    """Create tables if they don't exist. Safe to call on every boot."""
    db = sqlite3.connect(DB_PATH)

    # Before the schema block, not after - see the docstring.
    _migrate_rename_admin_actions(db)

    # THE CORE'S TABLES: what app.py writes itself, and what more than one
    # area adds rows to - accounts and sessions, the characters, the two
    # ledgers, the carry, the skills, the heal grants. Each package's own
    # tables are in its schema.py, called just below this.
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

        -- THE LOG'S OWN LOOKUPS. GET /api/staff/actions reads this table
        -- newest-first, filtered by who it was about, who did it, or what was
        -- done - and the table only ever grows. Every index here carries the
        -- rowid (which IS id), so "about this account, newest first, before
        -- id N" is one range walk rather than a sort of everything that
        -- account ever had done to it.
        --
        -- NOCASE ON THE NAMES because usernames are NOCASE, and an index
        -- only serves a comparison made in its own collation. The one above
        -- is BINARY, which is why these are new indexes and not a change to
        -- it: CREATE INDEX IF NOT EXISTS does nothing to an index that is
        -- already there, the same trap as the tables.
        CREATE INDEX IF NOT EXISTS idx_staff_actions_target_id
            ON staff_actions(target_id);
        CREATE INDEX IF NOT EXISTS idx_staff_actions_target_nc
            ON staff_actions(target_name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_staff_actions_actor_id
            ON staff_actions(actor_id);
        CREATE INDEX IF NOT EXISTS idx_staff_actions_actor_nc
            ON staff_actions(actor_name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_staff_actions_action
            ON staff_actions(action);

        -- token_hash, NOT the token - see _migrate_hash_session_tokens(). The
        -- game holds the token; this holds only its SHA-256, so a database
        -- that leaks hands over no working logins.
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash   TEXT    PRIMARY KEY,
            user_id      INTEGER NOT NULL,
            expires_at   INTEGER NOT NULL,
            -- The last time this client proved it was running. See
            -- ONLINE_WINDOW_SECONDS: a session lives thirty days, so "has a
            -- session" says nothing about whether anyone is playing.
            last_seen_at INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

        -- SESSIONS ENDED BY THE SAME ACCOUNT SIGNING IN SOMEWHERE ELSE. See
        -- ONE LOGIN AT A TIME. The session row is deleted, as a kick deletes
        -- it; this keeps only enough to tell the game it left behind WHY, so
        -- the player reads "signed in somewhere else" and not "signed out by
        -- the server". A hash, not the token: a dead token is still nobody's
        -- business. Pruned after a day with the sessions.
        CREATE TABLE IF NOT EXISTS ended_sessions (
            token_hash TEXT    PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            ended_at   INTEGER NOT NULL
        );

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

        -- WHICH COPIES OF THE GAME AN ACCOUNT HAS SIGNED IN FROM - the same
        -- question as account_ips, asked of the computer instead of the
        -- connection, because a VPN changes the address and not the computer.
        -- The game keeps a random install id in its own folder and sends it
        -- with every login; only its SHA-256 is stored, and no route ever
        -- returns it. See INSTALL IDS for what it does and does not stop.
        CREATE TABLE IF NOT EXISTS account_installs (
            user_id      INTEGER NOT NULL,
            install_hash TEXT    NOT NULL,
            first_seen   INTEGER NOT NULL,
            last_seen    INTEGER NOT NULL,
            PRIMARY KEY (user_id, install_hash),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_account_installs_hash
            ON account_installs(install_hash);

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

        -- THE BOOKS ON EVERY MONSTER (0.10.0; E3_SCOPE.md option C, step 1).
        -- presence.py keeps its own count of every monster's health from what
        -- the area's leader says and every hit any game sends (combatbook.py),
        -- and writes down what it saw here - and into nothing else: these two
        -- tables are the only ones that process writes. NOTHING READS THEM TO
        -- DECIDE ANYTHING YET: a kill is paid as it always was. killwatch.py
        -- matches kill_reports against combat_kills ("THE BOOKS"), and step 2
        -- makes a kill need an agreed row. Kept two weeks, like kill_reports;
        -- presence.py prunes them.
        --
        -- One row per player who hit a monster that died: AGREED (the books'
        -- count reached zero too), SHORT (the leader said it died with health
        -- the books still counted) or NOT DUE (it should not have been there).
        CREATE TABLE IF NOT EXISTS combat_kills (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            at         INTEGER NOT NULL,
            area       TEXT    NOT NULL,
            enemy_id   TEXT    NOT NULL,
            origin     TEXT    NOT NULL DEFAULT '',
            leader_id  INTEGER NOT NULL DEFAULT 0,
            user_id    INTEGER NOT NULL,
            slot       INTEGER NOT NULL DEFAULT -1,
            verdict    TEXT    NOT NULL,
            damage     INTEGER NOT NULL DEFAULT 0,
            refused    INTEGER NOT NULL DEFAULT 0,
            hp_left    INTEGER NOT NULL DEFAULT 0,
            max_hp     INTEGER NOT NULL DEFAULT 0,
            seconds    REAL    NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_combat_kills_user ON combat_kills(user_id, at);
        CREATE INDEX IF NOT EXISTS idx_combat_kills_at ON combat_kills(at);
        -- What did not fit, counted per player, area and kind per write: a
        -- hit too big, too fast or too far, a walk too quick, a monster the
        -- map does not explain (combatbook.FLAG_KINDS).
        CREATE TABLE IF NOT EXISTS combat_flags (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            area       TEXT    NOT NULL DEFAULT '',
            kind       TEXT    NOT NULL,
            count      INTEGER NOT NULL DEFAULT 1,
            detail     TEXT    NOT NULL DEFAULT '',
            first_at   INTEGER NOT NULL,
            last_at    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_combat_flags_user ON combat_flags(user_id, last_at);
        CREATE INDEX IF NOT EXISTS idx_combat_flags_at ON combat_flags(last_at);

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
            -- which pet this character currently has out, as the client's
            -- item_id (e.g. 'petpoisonslimesmall'). Empty string means none.
            -- Deliberately NOT validated against a list of known pets: the
            -- server has no item registry and inventing one here would mean
            -- every new pet the game adds needs a matching server deploy
            -- before it could be equipped. Same reasoning as bank item_ids.
            -- It IS checked against what the player holds: /api/save stores
            -- a pet only when the carry or the bank has it (_owns_item).
            active_pet_id TEXT NOT NULL DEFAULT '',
            -- WHAT THIS CHARACTER IS WEARING, as a JSON object of item ids.
            -- Here for the same reason active_pet_id is: a string the client
            -- cannot keep for itself.
            --
            -- JSON IN A TEXT COLUMN, not eight columns. The server does not
            -- query inside it; it stores it, hands it back, and refuses ids the
            -- catalogue has never heard of. A column per equip slot would be a
            -- schema change every time a slot is added, to buy a query nobody
            -- writes.
            --
            -- There was a `hotbar` column beside this, holding which item id
            -- each key pointed at. The hotbar holds real items now - they are
            -- carry_items rows past the bag, see CARRY_CAPACITY - so the column
            -- is gone from the schema. A database created before that still has
            -- it, and nothing reads or writes it; it defaults to '[]'.
            equipment     TEXT NOT NULL DEFAULT '{}',

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

        -- The backpack. Keyed on POSITION, not item_id (as the bank's cells
        -- are, in bank/schema.py): the backpack is a grid where the player
        -- expects a potion to stay in the cell they dragged it to, and two
        -- stacks of the same item in two different cells is an ordinary
        -- legal state. Keying on item_id would
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

        CREATE TABLE IF NOT EXISTS skills (
            user_id  INTEGER NOT NULL,
            slot     INTEGER NOT NULL,
            skill_id TEXT    NOT NULL,
            level    INTEGER NOT NULL DEFAULT 1 CHECK (level > 0),
            xp       INTEGER NOT NULL DEFAULT 0 CHECK (xp >= 0),
            PRIMARY KEY (user_id, slot, skill_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- A COMPUTER A MEMBER OF STAFF HAS ALREADY PROVED, so their logins from
        -- it need no emailed code. See TRUSTED DEVICES by STAFF LOGIN CODES.
        -- token_hash, NOT the token: the game keeps the token, and a database
        -- that leaks must not hand over working trust along with it. `role` is
        -- the rank it was trusted at - a promotion or demotion needs a new code.
        CREATE TABLE IF NOT EXISTS trusted_devices (
            token_hash   TEXT    PRIMARY KEY,
            user_id      INTEGER NOT NULL,
            role         TEXT    NOT NULL,
            created_at   INTEGER NOT NULL,
            last_used_at INTEGER NOT NULL,
            expires_at   INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_trusted_devices_user
            ON trusted_devices(user_id, created_at);

        -- A CHARACTER AS IT WAS, a few times over, so the owner can put it
        -- back. See SAVE SNAPSHOTS AND ROLLBACK. One row is everything about a
        -- character that a mistake, a bug or a dupe can change: level, xp,
        -- purse, what is worn, the pet, every carried cell and every skill, as
        -- JSON in `snapshot`. level, gold and items are copies of what is in
        -- it, so the list can be drawn without parsing every row.
        --
        -- NOT the bank and not lusions: those are the account's, shared by all
        -- four characters, and putting one character back must not reach into
        -- the other three.
        --
        -- `fingerprint` is the snapshot's sha1, so a character that has not
        -- changed takes no new row - an afternoon standing in town does not
        -- push the useful history out. The newest SNAPSHOTS_KEPT per character,
        -- pruned on write. A new table, so CREATE TABLE IF NOT EXISTS is the
        -- whole migration.
        CREATE TABLE IF NOT EXISTS save_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            slot        INTEGER NOT NULL,
            taken_at    INTEGER NOT NULL,
            reason      TEXT    NOT NULL,
            level       INTEGER NOT NULL,
            gold        INTEGER NOT NULL,
            items       INTEGER NOT NULL,
            fingerprint TEXT    NOT NULL,
            snapshot    TEXT    NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_save_snapshots_character
            ON save_snapshots(user_id, slot, taken_at);

        -- WHAT THE OWNER HAS GIVEN AWAY, one row a gift (THE GIFTS LEDGER, by
        -- the owner's give routes). A RECORD, NOT MONEY: nothing here is in
        -- the gold supply, and gold_ledger still holds every gold that moved.
        -- What each gift was worth is written when it is given, so the answer
        -- does not move when a price does. user_id is who got it - the owner
        -- too, for a gift to himself - and username is kept beside it, so the
        -- row still says who after the account is gone.
        CREATE TABLE IF NOT EXISTS owner_gifts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            at          INTEGER NOT NULL,
            actor_id    INTEGER,
            actor_name  TEXT    NOT NULL,
            user_id     INTEGER,
            username    TEXT    NOT NULL,
            slot        INTEGER,
            kind        TEXT    NOT NULL,
            item_id     TEXT    NOT NULL DEFAULT '',
            quantity    INTEGER NOT NULL,
            gold        INTEGER NOT NULL DEFAULT 0,
            lusions     INTEGER NOT NULL DEFAULT 0,
            value       INTEGER NOT NULL DEFAULT 0,
            sells_for   INTEGER NOT NULL DEFAULT 0,
            source      TEXT    NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_owner_gifts_user
            ON owner_gifts(user_id, at);
        CREATE INDEX IF NOT EXISTS idx_owner_gifts_at
            ON owner_gifts(at);

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

        -- THE TWO THE KINGDOM BOARD READS, AND THEY ARE PARTIAL ON PURPOSE.
        --
        -- The board only ever asks about gold that was DESTROYED, so every one
        -- of its queries carries `WHERE delta < 0`. The two indexes above are
        -- ordered by `at`, which none of those queries want, so each one was
        -- reading the whole table.
        --
        -- `WHERE delta < 0` on the index itself keeps the positive rows out of
        -- it entirely - and the positive rows are the majority, because most
        -- ledger events are loot being minted. `(user_id, delta)` and
        -- `(reason, delta)` then COVER the queries: SQLite answers them from
        -- the index pages without touching the table at all.
        --
        -- MEASURED, NOT ASSUMED. On 500 players and two million rows, the
        -- board's remaining two queries went from 229ms and 484ms to 70ms and
        -- 66ms. The indexes cost about 29 MB at that size and roughly a second
        -- to build once.
        CREATE INDEX IF NOT EXISTS idx_gold_ledger_spent_user
            ON gold_ledger(user_id, delta) WHERE delta < 0;
        CREATE INDEX IF NOT EXISTS idx_gold_ledger_spent_reason
            ON gold_ledger(reason, delta) WHERE delta < 0;

        -- SERVER-WIDE SETTINGS THAT OUTLIVE A RESTART.
        --
        -- WHY A TABLE AND NOT A CONSTANT: the maintenance switch is thrown
        -- from the owner's HUD while the server is running. A constant would
        -- need a file edit and a redeploy, which is precisely what you cannot
        -- do when the reason you are closing the server is that you are about
        -- to redeploy it. It also has to survive the restart it announces -
        -- a switch that forgets itself on boot reopens the server in the
        -- middle of the update it was closed for.
        --
        -- KEY-VALUE rather than a column per setting, because the alternative
        -- is a migration every time the owner needs one more switch.
        CREATE TABLE IF NOT EXISTS server_settings (
            key        TEXT    PRIMARY KEY,
            value      TEXT    NOT NULL,
            updated_by TEXT    NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL DEFAULT 0
        );

        -- WHAT THE SERVER SAYS TO EVERYONE.
        --
        -- Not a chat table. There is deliberately no player-to-player message
        -- here yet: this is the SERVER talking, so there is no moderation
        -- surface, no mute, no flood to police. Player chat can grow out of it
        -- later by adding an author and a gate; nothing here forecloses that.
        --
        -- WHY AN AUTOINCREMENT id AND NOT A TIMESTAMP CURSOR. Clients poll with
        -- "give me everything after N". Two broadcasts posted in the same second
        -- share a created_at, so a timestamp cursor either repeats one or drops
        -- one. A monotonic id cannot do either.
        CREATE TABLE IF NOT EXISTS broadcasts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT    NOT NULL DEFAULT 'system',
            body       TEXT    NOT NULL,
            created_by TEXT    NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_broadcasts_id ON broadcasts(id);

        -- A MOVE WAITING TO BE COLLECTED.
        --
        -- ONE PER PLAYER, enforced by the primary key: issuing a second
        -- teleport replaces the first rather than queueing behind it. Two
        -- pending destinations for one player is a question with no good
        -- answer.
        --
        -- WHY IT IS A QUEUE AND NOT A PUSH. The server has no socket to the
        -- game; the client asks. So a teleport is recorded here and collected
        -- on the next poll, which also means it survives the player being
        -- offline at the moment it was issued - they arrive where they were
        -- sent, whenever they next turn up.
        --
        -- slot_index and group_size are how the spacing is agreed. Every
        -- client computes the same offset from the same pair, so a group
        -- teleport lands as a ring and not a pile.
        CREATE TABLE IF NOT EXISTS pending_teleports (
            id         INTEGER NOT NULL,
            user_id    INTEGER PRIMARY KEY,
            area       TEXT    NOT NULL,
            x          REAL    NOT NULL DEFAULT 0,
            y          REAL    NOT NULL DEFAULT 0,
            slot_index INTEGER NOT NULL DEFAULT 0,
            group_size INTEGER NOT NULL DEFAULT 1,
            issued_by  TEXT    NOT NULL DEFAULT '',
            issued_at  INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- SHORT-LIVED CODES EMAILED TO A PLAYER.
        --
        -- ONE LIVE CODE PER (ACCOUNT, PURPOSE), enforced by the primary key:
        -- asking for a new code REPLACES the old one rather than adding a
        -- second. Two valid codes at once doubles the guessing surface and
        -- means revoking one leaves the other working.
        --
        -- code_hash, NOT code. A six digit code is a password with a fifteen
        -- minute life, and it is stored the same way one is - scrypt, via
        -- generate_password_hash. A database that leaks must not hand over
        -- working reset codes along with everything else.
        --
        -- attempts is what makes six digits safe. A million possibilities is
        -- nothing to a script, so the code burns after RESET_MAX_ATTEMPTS
        -- wrong guesses; the ceiling, not the length, is the defence.
        CREATE TABLE IF NOT EXISTS auth_codes (
            user_id    INTEGER NOT NULL,
            purpose    TEXT    NOT NULL,
            code_hash  TEXT    NOT NULL,
            target     TEXT    NOT NULL DEFAULT '',
            expires_at INTEGER NOT NULL,
            attempts   INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, purpose),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
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

    # THE PACKAGES' TABLES, IN ONE ORDER, AND ALL OF THEM HERE - after the
    # core's and before the first migration below. What the order has to hold:
    #
    #   * EVERY CREATE BEFORE EVERY MIGRATION. Each create() is an
    #     executescript, and executescript commits whatever is pending first.
    #     Here nothing is; below, the migrations share one transaction that
    #     the db.commit() at the end closes, and _migrate_hash_session_tokens
    #     counts on that.
    #   * A TABLE BEFORE ANY INDEX ON IT. Each schema.py writes its tables
    #     above their indexes, and no package indexes another's table.
    #   * A TABLE BEFORE ANYTHING THAT READS IT AT BOOT: kill/'s before
    #     _migrate_seed_kill_tally, bank/'s before _migrate_bank_to_account,
    #     chat/'s and trade/'s before their own upgrade() further down.
    #
    # NOT FOREIGN KEYS. SQLite does not need a REFERENCES target to exist when
    # the table naming it is created - it checks the reference when a row is
    # written - so the core going first is a reading order, not a requirement.
    # Among the packages nothing depends on the order; it is where each one's
    # first table stood in the single block they came out of.
    kill_schema.create(db)
    bank_schema.create(db)
    loot_schema.create(db)
    players_schema.create(db)
    trade_schema.create(db)
    saves_schema.create(db)
    world_schema.create(db)
    chat_schema.create(db)
    moderation_schema.create(db)
    friends_schema.create(db)
    guilds_schema.create(db)
    parties_schema.create(db)

    _migrate_bank_to_account(db)

    _migrate_add_column(db, "saves", "active_pet_id", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(db, "saves", "equipment", "TEXT NOT NULL DEFAULT '{}'")
    _migrate_add_column(db, "saves", "explored", "TEXT NOT NULL DEFAULT '{}'")

    # Millisecond timestamp of the last kill credited to this character, for the
    # rate limit in /api/combat/kill. 0 means "never", which is correctly in the
    # past for every comparison.
    _migrate_add_column(db, "saves", "last_kill_at", "INTEGER NOT NULL DEFAULT 0")

    # Kill-rate tokens. Defaults to the full bucket so an existing character is
    # not penalised for having played before this column existed.
    _migrate_add_column(db, "saves", "kill_tokens", "REAL NOT NULL DEFAULT 20.0")

    # When this character last reported skill-training activity to
    # /api/skill/train. 0 means never, which the endpoint reads as "clamp the
    # first report to the elapsed budget" rather than handing it the whole 1970.
    _migrate_add_column(db, "saves", "last_train_at", "INTEGER NOT NULL DEFAULT 0")

    # WHEN HP, MANA AND STAMINA WERE LAST WRITTEN - the clock the healing
    # reconciler measures regeneration against. It used updated_at, and every
    # kill, loot take, skill tick and equip moves updated_at, so a status write
    # two seconds after a kill was allowed two seconds of regeneration for
    # fifteen seconds of standing still, and honest regen was clamped. 0 on an
    # existing row means "not recorded yet", and the reconciler falls back to
    # updated_at for it. See _reconcile_heals().
    _migrate_add_column(db, "saves", "pools_at", "INTEGER NOT NULL DEFAULT 0")

    # THE RECOVERY ADDRESS. Added by migration, not only in CREATE TABLE,
    # because elusion.db already holds real accounts - a column that appears
    # only for new databases is how the two halves of this project drift apart.
    #
    # Empty string rather than NULL so every comparison in this file can stay a
    # plain `=` and no caller has to remember which of the two "no email" is.
    _migrate_add_column(db, "users", "email", "TEXT NOT NULL DEFAULT ''")

    # AN UNVERIFIED ADDRESS IS NOT A RECOVERY ADDRESS. Someone who typos their
    # email at signup would otherwise have handed their account to whoever owns
    # the address they actually typed. Only a 1 here can receive a reset code.
    _migrate_add_column(db, "users", "email_verified", "INTEGER NOT NULL DEFAULT 0")

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
    # TOKEN LIFECYCLE (game 0.44.0): which copy of the game the login is bound
    # to, when it began, whether it is hardened, when its password was last
    # typed, and wrong guesses at it since. Existing sessions are 0/'' - not
    # hardened - and keep working exactly as before until they end.
    _migrate_add_column(db, "sessions", "install_hash", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(db, "sessions", "created_at", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(db, "sessions", "hardened", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(db, "sessions", "confirmed_at", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(db, "sessions", "confirm_misses", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(db, "ended_sessions", "why", "TEXT NOT NULL DEFAULT 'elsewhere'")
    _migrate_add_column(db, "ended_sessions", "install_hash", "TEXT NOT NULL DEFAULT ''")

    # "WHO IS ONLINE" IS A QUESTION ABOUT RECENT ROWS. Sessions last thirty
    # days, so the table holds every login of the last month and the ones
    # inside ONLINE_WINDOW_SECONDS are a sliver of it. Every presence read - the
    # staff list's online filter, /api/players/online, the maintenance count -
    # asks for last_seen_at past a cutoff, and without this each of them read
    # the whole month to find the sliver. The price is one index update per
    # heartbeat, on a write that is already happening.
    #
    # HERE AND NOT IN THE SCHEMA BLOCK: an index on a column has to come after
    # the column. The schema block runs first, and on a database from before
    # presence existed it would fail on "no such column" and take the boot with
    # it - the MIGRATION section starts from exactly that shape.
    db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_seen ON sessions(last_seen_at)")

    # THE CHAT FLOOD BUCKET, on the account rather than on a character.
    #
    # Deliberately NOT sharing the kill or cast buckets: those are per-character
    # and per-activity for the reason written above them, and a player who has
    # been fighting hard should not arrive in chat already throttled. Starting
    # full means a returning account can say hello immediately; the bucket only
    # ever matters to someone sending faster than CHAT_TOKENS_PER_SECOND.
    _migrate_add_column(db, "users", "last_chat_at", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(db, "users", "chat_tokens", "REAL NOT NULL DEFAULT 8.0")

    # SEPARATE FROM last_image_at, which every upload stamps whatever channel
    # it ends up in. This one only moves when a picture actually reaches world
    # chat, so whispering a picture to a friend does not spend the half hour.
    _migrate_add_column(db, "users", "last_world_image_at", "INTEGER NOT NULL DEFAULT 0")

    # HOW MANY TIMES THIS ACCOUNT HAS DIED.
    #
    # A COUNTER, NOT A TABLE OF EVENTS, and that is a deliberate departure from
    # how this project records everything else. Gold has a ledger because gold
    # must be conservable - you have to be able to re-derive every balance from
    # the rows and catch a mint that went around the rules. A death conserves
    # nothing: there is no invariant it could violate, nobody can forge one
    # into existence for profit, and the only question anybody asks is "how
    # many". A row per death would be a table growing forever to answer a
    # question one integer answers.
    #
    # ON users, NOT ON saves. It is an account's lifetime figure, so deleting
    # a character cannot reset it - which it could if the count lived with the
    # character that earned it.
    _migrate_add_column(db, "users", "deaths", "INTEGER NOT NULL DEFAULT 0")

    # THE NAME COLOUR A PLAYER CHOSE, as a hue on the wheel, 0-359. NULL means
    # they never chose one, and the client draws its default - which keeps the
    # default in ONE place, settings.gd, rather than a copy here to drift.
    #
    # WHY IT IS ON THE SERVER AT ALL. It used to be a local setting, so the
    # colour somebody picked was drawn over their own head and nowhere else:
    # chat, the friends list, the players menu and the guild roster all painted
    # names by rank. Stored here, it travels with every name the server sends.
    #
    # AND ON EACH CHAT LINE, as a snapshot: chat/schema.py's upgrade() adds
    # that one, beside the line's `guild`.
    _migrate_add_column(db, "users", "name_hue", "INTEGER")

    # A TRADE'S FOURTH COLUMN, on the character (trade/schema.py's upgrade()
    # adds the other three, on the trade).
    #
    # The trade whose result this character's client has not been shown yet.
    # NULL means in step. While it is set, a whole-bag write from that client
    # is refused - it was built before the trade moved things.
    _migrate_add_column(db, "saves", "resync_trade", "TEXT")
    # Which character this session is playing, as the client last said on its
    # poll. NULL until it has said - see _playing_slot().
    _migrate_add_column(db, "sessions", "playing_slot", "INTEGER")
    # After every other sessions migration: it renames the key column, and the
    # ones above add columns beside it.
    _migrate_hash_session_tokens(db)

    # RELAYING A PICTURE IS NOT SENDING A MESSAGE. It costs the server an
    # outbound fetch of up to IMAGE_MAX_BYTES and a decode, so it gets a
    # cooldown of its own rather than riding the chat bucket - eight messages
    # in a burst is a conversation, eight four-megabyte fetches is not.
    _migrate_add_column(db, "users", "last_image_at", "INTEGER NOT NULL DEFAULT 0")

    # LOGIN THROTTLE state. failed_logins counts consecutive misses; lockout_until
    # is a unix time before which login is refused. Both default 0 so every
    # existing account starts clean. See login() and SECURITY_NOTES.md (E-5).
    _migrate_add_column(db, "users", "failed_logins", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(db, "users", "lockout_until", "INTEGER NOT NULL DEFAULT 0")

    # A CHAT MUTE: a unix time before which this account may not speak, and
    # who said so and why. 0 is "not muted", so every existing account starts
    # able to talk. See IGNORE, REPORT, MUTE.
    _migrate_add_column(db, "users", "chat_muted_until", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(db, "users", "chat_mute_reason", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(db, "users", "chat_muted_by", "TEXT NOT NULL DEFAULT ''")

    # THE PACKAGES' OWN COLUMNS, for a database made before them - each after
    # its create() above, inside this one transaction (db.execute only), and
    # handed the helper every column here goes through.
    chat_schema.upgrade(db, add_column=_migrate_add_column)
    trade_schema.upgrade(db, add_column=_migrate_add_column)

    _migrate_seed_gold_ledger(db)
    _migrate_seed_lusion_ledger(db)
    _migrate_xp_to_next_from_curve(db)
    _migrate_seed_kill_tally(db)

    db.commit()
    db.close()


# Defined up here, not with the routes: init_db() runs at import, before
# anything further down the file exists (CLAUDE.md, "init_db() runs at import
# time"), and the seed below reads it.
KILL_TALLY_SINCE_KEY = "kill_tally_since"


def _migrate_seed_kill_tally(db):
    """
    THE KILL RECORD'S OPENING COUNT, once: every paid kill kill_reports still
    holds (the last KILL_LOG_RETENTION_SECONDS of them), counted into
    kill_tally, and the date counting began written to server_settings as
    kill_tally_since - which is also what says this has run. A counter can only
    describe the future (CLAUDE.md, "A counter added by migration starts at
    zero"); this one starts with the two weeks the log remembers and says so.

    A DELETED CHARACTER'S KILLS ARE NOT THE NEXT ONE'S. kill_reports keeps a
    slot's rows when its character is deleted, so only rows after the slot's
    newest deletion are counted.
    """
    done = db.execute("SELECT value FROM server_settings WHERE key = ?",
                      (KILL_TALLY_SINCE_KEY,)).fetchone()
    if done is not None:
        return
    now = int(time.time())
    db.execute(
        "INSERT OR IGNORE INTO kill_tally (user_id, slot, enemy_id, kills, first_at, last_at)"
        " SELECT k.user_id, k.slot, k.enemy_id, COUNT(*), MIN(k.at), MAX(k.at)"
        "   FROM kill_reports k"
        "  WHERE k.at > COALESCE((SELECT MAX(d.deleted_at) FROM character_deletions d"
        "                          WHERE d.user_id = k.user_id AND d.slot = k.slot), 0)"
        "  GROUP BY k.user_id, k.slot, k.enemy_id"
    )
    first = db.execute("SELECT MIN(first_at) FROM kill_tally").fetchone()
    since = int(first[0]) if first is not None and first[0] is not None else now
    db.execute(
        "INSERT INTO server_settings (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)",
        (KILL_TALLY_SINCE_KEY, str(since), "migration", now),
    )
    app.logger.info("[KILLS] kill record seeded from kill_reports; counting since %d", since)


def _migrate_xp_to_next_from_curve(db):
    """
    Rewrite every character's xp_to_next from the curve in gamedata.json.

    The column is a display copy of a pure function of level, and the curve
    moved (100 x 1.15 became 1,250 x 1.27). apply_xp() already derives the
    real requirement, so nothing is mis-paid without this - but status reads
    return the stored figure, and a level-10 character would be shown 351 when
    the next level costs 10,749.

    IDEMPOTENT, and cheap: one UPDATE per distinct level, only rows that
    differ. Skipped when gamedata.json did not load, so a broken catalogue can
    never rewrite saves with the fallback curve. xp is never touched - every
    old requirement is below the new one, so nobody is pushed over a level.
    """
    if "xp_base" not in gamedata.CONSTANTS:
        return
    levels = [int(r[0]) for r in db.execute("SELECT DISTINCT level FROM saves")]
    changed = 0
    for level in levels:
        cur = db.execute(
            "UPDATE saves SET xp_to_next = ? WHERE level = ? AND xp_to_next != ?",
            (gamedata.xp_needed_for_level(level), level, gamedata.xp_needed_for_level(level)),
        )
        changed += cur.rowcount or 0
    if changed:
        app.logger.info("[XP] xp_to_next rewritten from the curve on %d character(s)", changed)


def _migrate_bank_to_account(db):
    """
    Move a per-character bank onto the account, and re-key it by position.

    WHY THIS IS NOT _migrate_add_column's JOB: that helper adds a COLUMN.
    bank_items needed its PRIMARY KEY changed - (user_id, slot, item_id) became
    (user_id, position) - and SQLite cannot alter a primary key. The table has
    to be rebuilt.

    WHY IT IS NEEDED AT ALL: CREATE TABLE IF NOT EXISTS does nothing to a table
    that already exists, so bank/'s corrected schema reaches a fresh database
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
    #
    # AND THEN THE COLUMN GOES (UPGRADES.md item 7, 19 Oct). A fresh database
    # never has it - init_db()'s CREATE TABLE saves no longer names it - and an
    # old one loses it here, once its gold is on the account. DROP COLUMN
    # needs SQLite 3.35; an older build keeps the column, zeroed, which costs
    # nothing because no query names it. The zeroing stays for that build: it
    # is what keeps the next boot from pooling the same gold twice.
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
        try:
            db.execute("ALTER TABLE saves DROP COLUMN bank_gold")
        except sqlite3.OperationalError as exc:
            print(f"[migrate] saves.bank_gold left in place, zeroed ({exc}); nothing reads it")


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


# =============================================================================
# SESSION TOKENS ARE STORED AS HASHES
# =============================================================================
# The game holds its bearer token; the server holds only the token's SHA-256.
# Every lookup hashes the token it was sent and searches for that.
#
# WHY. Passwords were always scrypt hashes, recovery codes too, trusted devices
# and ended sessions SHA-256 - and the one secret that logs a player straight in
# sat in `sessions.token` as it was issued. Found on day 2, auditing the server
# for the owner's security course: a copy of elusion.db - a backup, a leaked
# file - handed over a working login for every player signed in, for up to
# thirty days.
#
# SHA-256, NOT SCRYPT, on purpose. A password is short and guessable, so its
# hash has to be slow. A token is 32 random bytes from secrets.token_urlsafe:
# there is nothing to guess, and it is checked on every request, so a fast hash
# loses nothing and costs nothing.
#
# DEFINED HERE, ABOVE init_db(), because the migration below runs at import
# time and a helper defined further down the file does not exist yet then.

def _token_hash(token):
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _migrate_hash_session_tokens(db):
    """
    sessions.token (the token itself) -> sessions.token_hash (its SHA-256).

    Every live session survives: each stored token is replaced by its own hash,
    which is exactly what a lookup of that token computes, so nobody is signed
    out. Idempotent - a table that already has token_hash is left alone.

    THE HASHING BEFORE THE RENAME, in one transaction. The UPDATEs open it and
    the ALTER joins it (Python's sqlite3 no longer commits before DDL), and
    init_db() commits at its end - so a crash part-way leaves the old table as
    it was, never a column called token_hash holding raw tokens.
    """
    existing = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
    if "token_hash" in existing or "token" not in existing:
        return
    for (token,) in db.execute("SELECT token FROM sessions").fetchall():
        db.execute("UPDATE sessions SET token = ? WHERE token = ?", (_token_hash(token), token))
    db.execute("ALTER TABLE sessions RENAME COLUMN token TO token_hash")


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

def issue_token(user_id, expires_at=None, install_hash="", hardened=False, staff=False,
                confirmed=False):
    """Mint a session token and store it. Returns (token, expires_at).

    `expires_at` keeps an existing login's end date - see resume_session().
    A HARDENED session (TOKEN LIFECYCLE) is bound to `install_hash`, and a
    staff one ends STAFF_TOKEN_TTL after it began at the latest. `confirmed`
    is a login whose password was just typed."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    if expires_at is None:
        expires_at = now + TOKEN_TTL
    if hardened and staff:
        expires_at = min(int(expires_at), now + STAFF_TOKEN_TTL)

    db = get_db()
    # Seen NOW: whoever just logged in is, by definition, at the keyboard.
    # Only the hash is stored - see SESSION TOKENS ARE STORED AS HASHES. The
    # token itself leaves in the answer and is never written down here.
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, last_seen_at, install_hash, created_at,"
        " hardened, confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_token_hash(token), user_id, expires_at, now, install_hash if hardened else "", now,
         1 if hardened else 0, now if confirmed else 0),
    )
    db.commit()

    return token, expires_at


# =============================================================================
# ONE LOGIN AT A TIME
# =============================================================================
# A new login - or a game reopening a remembered one - ends every other session
# the account holds. The game it left behind is refused on its next request and
# goes back to the login screen saying why.
#
# WHY. Two games on one account was lost items, reproduced: both games hold a
# picture of the same backpack, and the bag write replaces the whole bag. One
# game unequips a sword into the bag; the other, still showing the bag from
# before, saves its own bag a moment later - and the sword is gone from both
# the bag and the gear. The trade version of this was fixed with a resync; this
# is the same bug with nothing to resync against, because the other writer is
# the same player. Every online game answers it the same way, and so do we.
#
# THE OLD SESSION IS DELETED, like a kick deletes it, so the other game cannot
# write anything from that moment on - not after its next heartbeat, now.
# ended_sessions keeps a hash of it for a day so the 401 can say "somewhere
# else" instead of "signed out by the server". _token_hash() is defined above
# init_db(); sessions are stored by the same hash.

def _end_other_sessions(db, user_id, keep_token, rotated_token=None):
    """Ends every session `user_id` holds except `keep_token`. Caller commits.

    `rotated_token` is the session a /resume replaced: its row says so
    ("rotated"), with the copy of the game it was bound to, so the token
    coming back from anywhere else is caught (TOKEN LIFECYCLE)."""
    now = int(time.time())
    keep = _token_hash(keep_token)
    rotated = _token_hash(rotated_token) if rotated_token else ""
    for row in db.execute("SELECT token_hash, install_hash FROM sessions WHERE user_id = ? AND token_hash != ?",
                          (user_id, keep)).fetchall():
        why = "rotated" if row["token_hash"] == rotated else "elsewhere"
        db.execute("INSERT OR REPLACE INTO ended_sessions (token_hash, user_id, ended_at, why, install_hash)"
                   " VALUES (?, ?, ?, ?, ?)", (row["token_hash"], user_id, now, why, row["install_hash"] or ""))
    db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep))


def _ended_elsewhere(token):
    """True when `token` was ended because its account signed in again."""
    if not token:
        return False
    return get_db().execute("SELECT 1 FROM ended_sessions WHERE token_hash = ?",
                            (_token_hash(token),)).fetchone() is not None


SIGNED_IN_ELSEWHERE_MESSAGE = ("This account signed in somewhere else, so this game "
                               "was signed out.")


# =============================================================================
# TOKEN LIFECYCLE (game 0.44.0)
# =============================================================================
# The students' review of the hashed tokens, 6 October: "Your ticket closes
# the database-leak vulnerability, but it doesn't cover token theft from
# anywhere else" - a raw token copied out of browser storage, a log, a crash
# dump, an extension or a clipboard still signs its holder in, with every
# power the account has. "Your missing defense is Token-lifecycle hardening
# through rotation, short lifetime, binding, revocation, and secure storage."
# What each of those is here:
#
#   BINDING       A HARDENED session belongs to one copy of the game: the
#                 install id the game sends with every request
#                 (X-Elusion-Install), hashed, stored on the session. A
#                 request on that session without it, or with another, is
#                 THEFT - the token travelled without the game it came with.
#   ROTATION      Unchanged: a new token at every login and every game start
#                 (/resume), every other session ended. NEW: the token a
#                 /resume replaced is remembered as "rotated", with its copy
#                 of the game. That token coming back FROM ANOTHER COPY is
#                 THEFT - somebody kept it. From the same copy (a second tab,
#                 a request in flight) it is "signed in somewhere else", as
#                 before.
#   THEFT         Ends every session and trusted computer the account has,
#                 tells every game why, and says so in the server log. The
#                 password still works; the thief never had it.
#   SHORT LIFE    A hardened STAFF session ends STAFF_TOKEN_TTL (12 hours)
#                 after its login at the latest, and after STAFF_IDLE_SECONDS
#                 (30 minutes) with no game running it (the heartbeat). A
#                 player's still lasts TOKEN_TTL.
#   STEP-UP       The most powerful routes (require_fresh_password: ranks,
#                 bans, gold and items, rollback, the server's switches) need
#                 the password typed within STEP_UP_SECONDS (15 minutes) -
#                 POST /api/auth/confirm - so a token alone, staff or not,
#                 cannot do them. CONFIRM_MAX_MISSES wrong guesses end every
#                 session, as theft does.
#   REVOCATION    Unchanged: logout, log out everywhere, a password change,
#                 a ban, a kick, a reset.
#   STORAGE       The server keeps only hashes (SESSION TOKENS ARE STORED AS
#                 HASHES); HSTS keeps a browser from ever sending the token
#                 over plain HTTP; the game refuses an http:// server that is
#                 not this computer (api.gd).
#
# HARDENED means the login came from a game that sends the install id with
# every request - client build HARDENED_BUILD (6) or later, saying so. Older
# games keep the old sessions until the owner sets the minimum build to 6,
# which is the switch that turns all of this on for everyone: an older game
# is then refused, updates, and signs in again hardened.
#
# AND EVERY OLD LOGIN ENDS AT THAT SWITCH, not when it runs out. An unbound
# token copied last week would otherwise keep working for up to TOKEN_TTL
# with a made-up build number - and a /resume with it would even bind a fresh
# token to the THIEF's copy of the game. So once the minimum build is
# HARDENED_BUILD or more, an unhardened session is over the next time it is
# shown, and the game says why ("old_login"). Every player types the password
# once more; that is the price, paid once.
HARDENED_BUILD = 6
INSTALL_HEADER = "X-Elusion-Install"
STAFF_TOKEN_TTL = 12 * 3600
STAFF_IDLE_SECONDS = 30 * 60
STEP_UP_SECONDS = 15 * 60
CONFIRM_MAX_MISSES = 5
TOKEN_STOLEN_MESSAGE = ("This login was used from somewhere it should not have been, so every "
                        "session on the account was ended. Sign in again - and if that was not "
                        "you, change your password.")
STAFF_SESSION_ENDED_MESSAGE = "A staff login lasts 12 hours, and half an hour away. Sign in again."
STEP_UP_MESSAGE = "Enter your password to do this."
OLD_LOGIN_MESSAGE = "The game was updated, and logins from before the update have ended. Sign in again."


def request_install_hash():
    """The install id this request came with, hashed - "" for none."""
    if not has_request_context():
        return ""
    return _install_hash(request.headers.get(INSTALL_HEADER))


def login_hardening():
    """(hardened, install hash) for a login or resume happening now: hardened
    when the game is build HARDENED_BUILD or later and sends its install id."""
    install = request_install_hash()
    return bool(install) and client_build() >= HARDENED_BUILD, install


def _end_for_theft(db, user_id, why_text):
    """THEFT: every session and trusted computer of `user_id` ended, each
    game told why, and the server log told. Caller need not commit."""
    now = int(time.time())
    rows = db.execute("SELECT token_hash, install_hash FROM sessions WHERE user_id = ?", (user_id,)).fetchall()
    for row in rows:
        db.execute("INSERT OR REPLACE INTO ended_sessions (token_hash, user_id, ended_at, why, install_hash)"
                   " VALUES (?, ?, ?, 'stolen', ?)", (row["token_hash"], user_id, now, row["install_hash"] or ""))
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM trusted_devices WHERE user_id = ?", (user_id,))
    db.commit()
    who = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    app.logger.warning("[SECURITY] token theft suspected  user=%r  ip=%s  %s - %d session(s) ended",
                       who["username"] if who else user_id, client_ip() if has_request_context() else "-",
                       why_text, len(rows))


def _hardened_session_holds(db, row, token):
    """A hardened session's own rules, on every request: its copy of the game,
    and for staff its short life. False (and the session gone) when broken."""
    if has_request_context() and (row["session_install"] or "") != request_install_hash():
        _end_for_theft(db, int(row["id"]), "a bound token came without its copy of the game")
        return False
    if role_at_least(row, "mod"):
        now = int(time.time())
        began = int(row["session_created"] or 0)
        seen = int(row["session_seen"] or 0)
        if (began and now - began > STAFF_TOKEN_TTL) or (seen and now - seen > STAFF_IDLE_SECONDS):
            db.execute("INSERT OR REPLACE INTO ended_sessions (token_hash, user_id, ended_at, why, install_hash)"
                       " VALUES (?, ?, ?, 'staff', ?)", (_token_hash(token), int(row["id"]), now,
                                                          row["session_install"] or ""))
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
            db.commit()
            return False
    return True


def _ended_refusal(token):
    """The 401 for a token that was ended rather than never valid, saying
    why - or None. A REPLACED token back from another copy of the game is
    theft, and ends everything the account still has."""
    if not token:
        return None
    db = get_db()
    row = db.execute("SELECT user_id, why, install_hash FROM ended_sessions WHERE token_hash = ?",
                     (_token_hash(token),)).fetchone()
    if row is None:
        return None
    why = str(row["why"] or "elsewhere")
    if why == "rotated" and row["install_hash"] and row["install_hash"] != request_install_hash():
        _end_for_theft(db, int(row["user_id"]), "a replaced token came back from another copy of the game")
        why = "stolen"
    if why == "stolen":
        return {"error": "Unauthorized", "message": TOKEN_STOLEN_MESSAGE, "token_stolen": True}, 401
    if why == "staff":
        return {"error": "Unauthorized", "message": STAFF_SESSION_ENDED_MESSAGE, "staff_session_ended": True}, 401
    if why == "old":
        return {"error": "Unauthorized", "message": OLD_LOGIN_MESSAGE, "old_login": True}, 401
    return {"error": "Unauthorized", "message": SIGNED_IN_ELSEWHERE_MESSAGE, "signed_in_elsewhere": True}, 401


def _end_old_login(db, row, token):
    """An unhardened session, shown after the minimum build reached
    HARDENED_BUILD: ended, remembered as 'old' so the game can say why."""
    db.execute("INSERT OR REPLACE INTO ended_sessions (token_hash, user_id, ended_at, why, install_hash)"
               " VALUES (?, ?, ?, 'old', '')", (_token_hash(token), int(row["id"]), int(time.time())))
    db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    db.commit()


def user_for_token(token):
    """Resolve a bearer token to a user row, or None if missing/expired."""
    if not token:
        return None

    db = get_db()
    row = db.execute(
        """
        SELECT u.id, u.username, u.role,
               u.is_banned, u.ban_expires_at, u.ban_reason, u.banned_by, u.banned_at,
               u.email_verified, u.name_hue,
               u.chat_muted_until, u.chat_mute_reason,
               s.expires_at, s.install_hash AS session_install, s.hardened, s.created_at AS session_created,
               s.last_seen_at AS session_seen, s.confirmed_at
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ?
        """,
        (_token_hash(token),),
    ).fetchone()

    if row is None:
        return None

    if row["expires_at"] < int(time.time()):
        # expired - clean it up rather than leaving dead rows around
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
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

    if int(row["hardened"] or 0):
        if not _hardened_session_holds(db, row, token):
            return None
    elif min_client_build() >= HARDENED_BUILD:
        _end_old_login(db, row, token)
        return None

    return row


def bearer_token():
    """Pull the token out of an 'Authorization: Bearer <token>' header."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header[7:].strip()


def stamp_presence(db, slot=None):
    """Mark the calling session as alive, right now. Caller commits.

    THIS IS WHAT "ONLINE" MEANS EVERYWHERE IN THE SERVER - the friends list,
    the guild roster, /api/players/online, /api/players/nearby and the staff
    panel all read `sessions.last_seen_at` against ONLINE_WINDOW_SECONDS. A
    session lasts thirty days, so "holds a session" was never the same question.

    IT USED TO LIVE ONLY IN GET /api/auth/session, AND NOTHING CALLED THAT ON A
    TIMER. api.gd declares HEARTBEAT_SECONDS = 15 and characterhud.gd's comment
    says the client beats on it; the only caller of Api.heartbeat() in the whole
    project is the 401 handler. So the stamp happened at login and then never
    again, and forty-five seconds later every presence surface in the game said
    the player was offline - including, visibly, a guild panel reporting
    "0 online of 1" to the one person in the guild, who was looking at it.

    ONE UPDATE BY PRIMARY KEY. The token's hash is the row, so this touches exactly the
    session asking and nothing else. expires_at is NOT extended: a beat proves
    the client is running, not that the login is fresher.

    `slot` IS WHICH CHARACTER THE CLIENT IS PLAYING, when it says. Before this
    the server guessed it from whichever save was written last, and a save is
    only written when something changes - so for the first minute after
    picking a character, the server believed you were still the one you
    played yesterday, and a trade opened by typing your name went to her.
    None leaves the column as it was: a caller that does not know is not
    saying "nobody".
    """
    if slot is None:
        db.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
            (int(time.time()), _token_hash(bearer_token())),
        )
        return
    db.execute(
        "UPDATE sessions SET last_seen_at = ?, playing_slot = ? WHERE token_hash = ?",
        (int(time.time()), int(slot), _token_hash(bearer_token())),
    )


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


# =============================================================================
# INSTALL IDS
# =============================================================================
# The address check below stops the lazy evader - banned, new account, same
# connection - and a VPN walks straight past it, which SECURITY.md used to list
# as an honest limit. An install id is the other half: a random id the game
# makes the first time it runs and keeps in its own folder (user://install.cfg),
# sent with every login, registration and resume. A VPN changes the address;
# it does not change that file.
#
# THE SAME RULES AS THE ADDRESS, ON PURPOSE:
#   - Registration only. A family computer is a household: the sibling who
#     shares it keeps logging in to their own account.
#   - It expires with the ban, through ban_state().
#   - A crowded computer is not a person. Above EVASION_BLOCK_MAX_ACCOUNTS
#     (a library, an internet cafe) it blocks nothing, so getting yourself
#     banned on a public machine cannot lock out everyone who uses it.
#   - Staff see the link (_linked_accounts), and a human decides.
#
# WHAT IT IS NOT. The id is whatever the client sends, so it is a speed bump
# and not a lock: deleting the file, a second browser for the web build, or a
# modified game sending a fresh id each time gets past it. It stops the evader
# who tries a VPN and nothing else, and the staff link view still shows the
# rest. Stored as SHA-256 and never returned by any route, so nobody can read
# somebody else's id out of the server and send it to frame them.

def _install_hash(raw):
    """SHA-256 of a well-formed install id, or "" for anything else - absent,
    an older game that does not send one, or junk. Ignored, never refused:
    a login does not fail over a field that only exists for moderation."""
    clean = str(raw or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32,128}", clean):
        return ""
    return hashlib.sha256(clean.encode()).hexdigest()


def _record_account_install(db, user_id, install_hash, now):
    """Remember that this account has signed in from this copy of the game.
    The same upsert as _record_account_ip: one row per (account, install),
    first_seen kept, last_seen moving."""
    if not install_hash or not user_id:
        return
    db.execute(
        """
        INSERT INTO account_installs (user_id, install_hash, first_seen, last_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, install_hash) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (user_id, install_hash, now, now),
    )
    db.commit()


def _install_evasion_state(db, install_hash):
    """Has a currently-banned account signed in from this copy of the game?
    The banned username, or None. _ban_evasion_state() below, for the
    computer instead of the connection - read its notes; the crowd rule and
    the u.* are there for the same reasons."""
    if not install_hash:
        return None
    crowd = db.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM account_installs WHERE install_hash = ?",
        (install_hash,),
    ).fetchone()
    if int(crowd["n"] or 0) > EVASION_BLOCK_MAX_ACCOUNTS:
        return None
    for candidate in db.execute(
        """
        SELECT u.*
        FROM account_installs a
        JOIN users u ON u.id = a.user_id
        WHERE a.install_hash = ? AND u.is_banned = 1
        ORDER BY a.last_seen DESC
        """,
        (install_hash,),
    ).fetchall():
        if ban_state(candidate) is not None:
            return candidate["username"]
    return None


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
            "shared_computers": 0,
            "quietest_computer_accounts": 0,
        })

    # THE SAME COMPUTER, which a VPN does not change - see INSTALL IDS. Merged
    # into the list above rather than shown as a second one: "same address and
    # same computer" is one sibling, not two findings. A computer link is
    # STRONG unless the computer itself is crowded (a library machine links
    # strangers exactly the way a carrier address does).
    by_name = {entry["username"]: entry for entry in linked}
    for row in db.execute(
        """
        SELECT u.id, u.username, u.role,
               u.is_banned, u.ban_expires_at, u.ban_reason,
               u.banned_by, u.banned_at,
               COUNT(DISTINCT mine.install_hash) AS computers,
               MIN(crowd.n)            AS quietest,
               MIN(theirs.first_seen)  AS first_linked,
               MAX(theirs.last_seen)   AS last_linked
        FROM account_installs mine
        JOIN account_installs theirs
             ON theirs.install_hash = mine.install_hash AND theirs.user_id != mine.user_id
        JOIN users u ON u.id = theirs.user_id
        JOIN (
            SELECT install_hash, COUNT(DISTINCT user_id) AS n
            FROM account_installs
            WHERE install_hash IN (SELECT install_hash FROM account_installs WHERE user_id = ?)
            GROUP BY install_hash
        ) crowd ON crowd.install_hash = mine.install_hash
        WHERE mine.user_id = ?
        GROUP BY u.id
        ORDER BY quietest ASC, last_linked DESC
        LIMIT ?
        """,
        (user_id, user_id, LINKED_ACCOUNT_LIMIT + 1),
    ).fetchall():
        quiet_computer = int(row["quietest"] or 0)
        entry = by_name.get(row["username"])
        if entry is None:
            entry = {
                "username": row["username"],
                "role": role_for(row),
                "ban": ban_state(row),
                "shared_addresses": 0,
                "quietest_address_accounts": 0,
                "busiest_address_accounts": 0,
                "strength": "weak",
                "first_linked": int(row["first_linked"] or 0),
                "last_linked": int(row["last_linked"] or 0),
            }
            by_name[row["username"]] = entry
            linked.append(entry)
        entry["shared_computers"] = int(row["computers"] or 0)
        entry["quietest_computer_accounts"] = quiet_computer
        if quiet_computer < SHARED_ADDRESS_ACCOUNTS:
            entry["strength"] = "strong"
        entry["first_linked"] = min(entry["first_linked"] or int(row["first_linked"] or 0),
                                    int(row["first_linked"] or 0))
        entry["last_linked"] = max(entry["last_linked"], int(row["last_linked"] or 0))

    # BANNED FIRST, then the strong links, then the rest - the order the
    # address list already had, kept now that two sources feed it.
    linked.sort(key=lambda e: (e["ban"] is None, e["strength"] != "strong",
                               -e["shared_computers"], e["quietest_address_accounts"],
                               -e["last_linked"]))
    if len(linked) > LINKED_ACCOUNT_LIMIT:
        truncated = True
        linked = linked[:LINKED_ACCOUNT_LIMIT]

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
    # A day is long past any game still running on an ended session: it is
    # refused on its next request, within a heartbeat.
    db.execute("DELETE FROM ended_sessions WHERE ended_at < ?", (now - 86400,))
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
    username that does not exist has nothing to count against.

    Returns True when this miss is the one that locked it."""
    count = _row_int(row, "failed_logins") + 1
    locked = count >= LOGIN_MAX_ATTEMPTS
    if locked:
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
    return locked


def _wait_words(seconds):
    """"15 minutes", "1 minute", "40 seconds" - for a person, not a stopwatch.
    Rounded UP: telling somebody "14 minutes" when there are 14 and a half is
    sending them back to a door that is still shut."""
    seconds = max(1, int(seconds))
    if seconds < 60:
        return "%d second%s" % (seconds, "" if seconds == 1 else "s")
    minutes = (seconds + 59) // 60
    return "%d minute%s" % (minutes, "" if minutes == 1 else "s")


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


def co_owners_on():
    """True while the owner's co-owner switch is on (CO_OWNERS_KEY). Asked once
    a request - role_for() runs for every name on a chat page - and OFF when it
    cannot be read, outside a request included: a corrupt row must not hand
    out the owner's powers."""
    if not CO_OWNER_USERNAMES:
        return False
    try:
        cached = g.get("_co_owners_on")
    except RuntimeError:
        cached = None
        outside = True
    else:
        outside = False
    if cached is not None:
        return cached
    try:
        raw = get_server_setting(CO_OWNERS_KEY)
        on = bool(json.loads(raw).get("on")) if raw else False
    except Exception:
        on = False
    if not outside:
        g._co_owners_on = on
    return on


def _co_owners_changed():
    """Forget this request's answer, so the next co_owners_on() reads the row
    again - the co-owners' switch, once it has moved it (switches/)."""
    g.pop("_co_owners_on", None)


def is_co_owner(username):
    """True for an account ELUSION_CO_OWNERS names, while the owner's switch is
    on. Case-insensitive, like is_owner(). Never the owner."""
    if not username or not CO_OWNER_USERNAMES or is_owner(username):
        return False
    folded = str(username).casefold()
    if not any(folded == name.casefold() for name in CO_OWNER_USERNAMES):
        return False
    return co_owners_on()


def has_owner_powers(username):
    """The owner, or a co-owner while the switch is on: what require_owner
    lets in, and what the login answer's is_owner says."""
    return is_owner(username) or is_co_owner(username)


# The ranks, in order. Index is the comparison - "mod or above" is one
# integer test rather than an expression repeated in every endpoint, which is
# what a pile of booleans turns into the moment there is more than one of them.
#
# 'owner' is last and is NOT storable. users.role has a CHECK that refuses it,
# and role_for() supplies it from the environment instead. That is what makes
# the top rank ungrantable: there is no write that produces it.
#
# 'coowner' (0.21.0) is the same: not storable, not grantable, supplied by
# role_for() from ELUSION_CO_OWNERS and the owner's switch. Just below the
# owner, so can_act_on() keeps the owner above every co-owner by itself.
ROLES = ("player", "mod", "dev", "coowner", "owner")

# The ranks that come from the environment, never from a row.
ENVIRONMENT_ROLES = ("coowner", "owner")

# What a fresh account is, and what an unrecognised value is read as. A row
# holding something this server has never heard of - written by an older build,
# or by hand - must read as the LEAST privilege, never the most.
DEFAULT_ROLE = "player"

# The ranks a moderation endpoint may assign. 'owner' is absent and always will
# be - it comes from ELUSION_OWNER, so no request can grant it. Mirrors
# SETTABLE_ROLES in set_role.py.
SETTABLE_ROLES = tuple(r for r in ROLES if r not in ENVIRONMENT_ROLES)


def role_for(user):
    """
    The effective rank of a user row.

    The owner is decided before the column is consulted, so revoking their row
    cannot lock them out of their own server, and a column set to 'owner' by
    hand cannot grant it.
    """
    if is_owner(user["username"]):
        return "owner"
    if is_co_owner(user["username"]):
        return "coowner"

    stored = user["role"] if "role" in user.keys() else DEFAULT_ROLE
    return stored if stored in ROLES and stored not in ENVIRONMENT_ROLES else DEFAULT_ROLE


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


# THE NAME COLOUR IS A HUE, 0-359 - a position on the wheel, nothing more.
# Saturation and brightness are fixed by the client (Settings.NAME_SATURATION
# and NAME_VALUE) so that every choice is readable; storing a whole colour
# would let a patched client store an unreadable one.
NAME_HUE_MAX = 359


def name_hue_of(row):
    """The hue a user row chose, or None when they never chose one.

    None rather than a default on purpose: the client owns the default, and a
    second copy of it here is a second number to keep in step.
    """
    if row is None or "name_hue" not in row.keys():
        return None
    hue = row["name_hue"]
    if hue is None:
        return None
    hue = int(hue)
    return hue if 0 <= hue <= NAME_HUE_MAX else None


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


# =============================================================================
# MAINTENANCE - THE OWNER'S KILL SWITCH
# =============================================================================
#
# Closing the server is TWO events, not one, and the gap between them is the
# whole design:
#
#   1. THE DOOR SHUTS. New logins are refused immediately with a message the
#      player can read. Nobody new arrives.
#   2. THE ROOM EMPTIES. Players already in the world keep their session for a
#      grace window, then are signed out on their next request.
#
# WHY THE GAP EXISTS: the client is still the thing that knows what just
# happened to a character. characterdata.gd debounces its writes, so at any
# instant there is up to a couple of seconds of real progress - gold picked up,
# xp earned - that the server has not been told about yet. Deleting sessions
# the moment the switch is thrown throws that away, and the player experiences
# it as "the update ate my loot".
#
# So the grace window is not politeness, it is correctness: it is the time the
# client uses to flush. GET /api/auth/session is the heartbeat, it runs more
# often than ONLINE_WINDOW_SECONDS, and it now carries the maintenance notice -
# so every client that is actually playing learns the server is closing and
# saves, well before its session is taken away.
#
# THE OWNER IS NEVER LOCKED OUT BY THEIR OWN SWITCH. Someone has to be able to
# get in and turn it off; if that someone could be shut out, the only remaining
# fix would be a shell on the server.

MAINTENANCE_KEY = "maintenance"

# PVP. One row in server_settings, like the maintenance switch beside it, and on
# the server for the same reason: it is a fact about the WORLD rather than about
# one client, so every client has to be able to read the same answer.
#
# WHAT IT DOES TODAY, stated plainly so nobody has to guess from the name: it
# says PvP is on, it is announced in chat when it changes, and it appears on
# /api/status so every client sees it - including one sitting on the login
# screen that has never logged in. IT DOES NOT MAKE ANYBODY DAMAGEABLE, because
# nothing in this game can damage another player yet: there are no positions on
# the server and no remote bodies in the client, which /api/players/nearby says
# in its own comment. See CLAUDE.md, "Decided, not built: PvP and the world
# boss", for the four steps between here and a world boss.
#
# IT IS STILL WORTH HAVING NOW RATHER THAN LATER, and the reason is where it
# lives. When damage does arrive, the question "is PvP on" must be answered by
# the SERVER - a flag in a client is a flag an attacker sets. Putting the switch
# in the right place first means the damage path has an authority to ask, rather
# than growing one in a hurry next to the thing that needed it.
PVP_KEY = "pvp"

# TRADE. Fourth row, the same shape: the owner can stop players trading with
# each other in one request. E-3 is open - a kill is asserted, not watched - so
# a cheated drop is real loot, and trading is the one way loot or gold moves
# from one account to another. Off, a cheat stays in the account that made it.
# E3_SCOPE.md, option B.
#
# OFF STOPS NEW TRADES, NOT OPEN ONES. The owner, 6 Oct: "allow trade to
# finish". A trade already open may still be changed, accepted or cancelled,
# and it dies on its own after TRADE_EXPIRY_SECONDS of nobody touching it; only
# POST /api/trade/offer is refused. Nothing is half-done by the switch.
#
# NO ROW READS ON, because trading was on before the switch existed; a row that
# cannot be read reads OFF, because a corrupt row must not open the border.
TRADE_KEY = "trade"

# THE CO-OWNERS' SWITCH (0.21.0), the same shape: {"on": true}. Who it lets in
# is ELUSION_CO_OWNERS, in the environment; this only says whether they are in
# now. NO ROW READS OFF - a server starts with its co-owners out - and a row
# that cannot be read reads OFF. Thrown by THE owner only (require_the_owner).
CO_OWNERS_KEY = "coowners"

# THE CLIENT BUILD GATE. Third row in server_settings, same shape as the two
# above, and here for a reason neither of them has: this one cannot be added
# later without leaving a permanent hole.
#
# WHAT THE HOLE IS. Until now nothing in a request said which build sent it.
# The server could not refuse a client with a known bug, could not tell a
# player to update, and - worst of the three - a breaking change to the wire did
# not FAIL on an old build, it half-worked. Silently, on exactly the copies
# nobody can reach.
#
# WHY IT IS A DOOR RATHER THAN A TASK. The header can be added any time, and an
# absent header can be read as "pre-versioning". That works. What does not come
# back is the first cohort: every build shipped before the header exists is
# permanently in the unknown bucket, and the unknown bucket is the oldest code,
# which is the code you will most want to reason about. So it ships with the
# first build or it never covers it.
#
# /api/status ALREADY VERSIONS THE DATA CONTRACT with gamedata_schema. This is
# the same idea one layer up: that one says "we disagree about the roster", this
# one says "we disagree about the protocol".
MIN_BUILD_KEY = "min_client_build"

# The header a client stamps its build on. X- prefix because it is not a
# registered header name; nothing else in this API invents one.
CLIENT_BUILD_HEADER = "X-Elusion-Build"

# What the client shipped alongside this server reports. Served on /api/status
# so an OLD client can tell it is old without the server having to refuse it -
# which is the whole point of shipping the gate disarmed.
#
# MONOTONIC INTEGER, NOT A SEMVER STRING. The only question the gate asks is
# "older than", and integers answer that without a parser. A display version
# lives beside it in the client for humans; this is the one the server compares,
# and it never needs to know how the humans are numbering things.
#
# 2: the backpack and bank became server-owned (ONE CELL AT A TIME). A build-1
# game still plays, but its drags and its bin are never saved - its whole-bag
# writes are ignored - so this is the first build worth setting as the minimum.
#
# 3: dropped gear carries its quality roll in its id ("ironsword~d107", QUALITY
# ROLLS in gamedata.py). A build-2 game has never seen one: its ItemRegistry
# answers the error item for a rolled piece, so it cannot show, wear or sell
# one. Set the minimum to 3 once the new game is out.
#
# 4: armour carries an element resistance at the end of its roll
# ("jadechest~a104h96r605", ELEMENT RESISTANCE in gamedata.py). A build-3 game
# reads that whole id as a malformed roll - the error item - exactly as build 2
# read a quality roll. Set the minimum to 4 once 0.11.0 is out.
#
# 5 (game 0.41.0): a game's state carries its aim and health (presence.py) and
# every other game draws them; an attack with no cursor on the game aims where
# the character faces. A build-4 game - anything from 0.11 to 0.40 - plays on,
# but draws nobody's aim or health, and a browser left on an old upload swings
# every keyboard attack at the screen's corner. Set the minimum to 5 once
# 0.41.0 is on the site, and everyone in an area sees the same.
#
# 6 (game 0.44.0): every request says which copy of the game sent it
# (X-Elusion-Install), and a login from it is bound to that copy (TOKEN
# LIFECYCLE). A build-5 game plays on with the old, unbound logins. Set the
# minimum to 6 once 0.44.0 is on the site: that is the switch that binds every
# login, ends every old one, and asks staff for the password again.
CURRENT_CLIENT_BUILD = 6

MAINTENANCE_DEFAULT_MESSAGE = "Update in progress - please come back later."

# The grace window's default and its longest (MAINTENANCE_DEFAULT_GRACE_SECONDS,
# MAINTENANCE_MAX_GRACE_SECONDS) are the close route's, in switches/services.py.

MAINTENANCE_OFF = {
    "on": False, "message": "", "back_at": None, "kick_at": 0, "by": "", "at": 0,
}


def get_server_setting(key, default=None):
    """Read one setting, tolerating a database that predates the table."""
    try:
        row = get_db().execute(
            "SELECT value FROM server_settings WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.OperationalError:
        # A server that cannot read the switch must behave as though it is OFF.
        # FAILING OPEN IS DELIBERATE HERE and it is the opposite of the usual
        # rule: failing closed would mean a missing table locks every player
        # out of a perfectly healthy server, which is a self-inflicted outage.
        return default
    return default if row is None else row["value"]


def set_server_setting(key, value, by=""):
    """Write one setting. Caller commits - this runs inside their transaction."""
    get_db().execute(
        """
        INSERT INTO server_settings (key, value, updated_by, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value      = excluded.value,
            updated_by = excluded.updated_by,
            updated_at = excluded.updated_at
        """,
        (key, value, by, int(time.time())),
    )


def pvp_enabled():
    """True when the owner has PvP switched on. Anything unreadable reads OFF.

    Fails toward off on purpose, the same way every unrecognised rank in this
    file reads as `player`: a corrupt row must not be able to turn combat on.
    """
    raw = get_server_setting(PVP_KEY)
    if not raw:
        return False
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return bool(isinstance(stored, dict) and stored.get("on"))


def trades_enabled():
    """True unless the owner has switched trading off. See TRADE_KEY."""
    raw = get_server_setting(TRADE_KEY)
    if raw is None:
        return True
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return bool(isinstance(stored, dict) and stored.get("on"))


def client_build():
    """Which build sent this request. 0 when it did not say.

    ZERO MEANS "FROM BEFORE ANY OF THIS EXISTED", and it is a real answer
    rather than a missing one. Every build shipped before the header did will
    never send it, so the absent case has to mean something specific and has to
    keep meaning it. It sorts below every real build, which is exactly right:
    the oldest code is the code most likely to need refusing.

    ANYTHING UNREADABLE IS ALSO ZERO. A header of "banana", "1.0" or an empty
    string is a client that does not know the protocol, which is the same
    situation as one that never heard of it. No 400: refusing the request would
    make a malformed header a way to learn that the gate exists, and the gate
    is not a secret but it is also not worth a separate refusal path.
    """
    raw = (request.headers.get(CLIENT_BUILD_HEADER) or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def min_client_build():
    """The oldest build allowed in. 0 disarms the gate entirely.

    FAILS TOWARD DISARMED, the opposite direction from the maintenance switch
    and deliberately so. A corrupt row in `server_settings` must not be able to
    lock every player out of the game - the failure it could cause is the exact
    failure this gate exists to make survivable, and a defence whose broken
    state is an outage is not one. The same argument pvp_enabled() makes for
    failing toward off: a row nobody can read must not be able to decide
    something this large.

    IT SHIPS AT 0 ON PURPOSE. The mechanism is the thing that cannot be added
    later; the enforcement can be switched on at any moment from one owner
    request. So the first cohort cannot be locked out by a number typed wrong
    months before anyone met it.
    """
    raw = get_server_setting(MIN_BUILD_KEY)
    if not raw:
        return 0
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return 0
    if not isinstance(stored, dict):
        return 0
    try:
        return max(0, int(stored.get("build", 0)))
    except (TypeError, ValueError):
        return 0


def pvp_since():
    """Unix seconds when the PvP switch was last written. 0 if never.

    NO NEW COLUMN AND NO NEW WRITE. `server_settings` has recorded
    `updated_at` on every key since the table was created, and
    set_server_setting() has always stamped it - so the moment PvP was
    switched has been stored all along and nothing was reading it.

    THAT IS WHY THIS IS A READ AND NOT A FEATURE. The alternative was putting
    an "at" inside the JSON blob in `value`, which would have been a SECOND
    place the same fact lives, written by one path and not the other, and the
    first disagreement between them would be unresolvable - exactly the
    argument the kingdom total already makes for deriving instead of counting.

    0 RATHER THAN None, because it crosses the wire to a Godot client that
    parses every JSON number as a float and has no null to compare against.
    The client reads 0 as "no time known" and shows the state without one.
    """
    try:
        row = get_db().execute(
            "SELECT updated_at FROM server_settings WHERE key = ?", (PVP_KEY,)
        ).fetchone()
    except sqlite3.OperationalError:
        # Same posture as get_server_setting(): a table this build predates
        # must not raise, and an unknown time is simply unknown.
        return 0
    return int(row["updated_at"] or 0) if row is not None else 0


def maintenance_state():
    """
    The switch, normalised. Always returns the full shape, so no caller has to
    guess whether a key is missing - anything unreadable reads as OFF.
    """
    raw = get_server_setting(MAINTENANCE_KEY)
    if not raw:
        return dict(MAINTENANCE_OFF)
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return dict(MAINTENANCE_OFF)
    if not isinstance(stored, dict) or not stored.get("on"):
        return dict(MAINTENANCE_OFF)
    state = dict(MAINTENANCE_OFF)
    state.update({
        "on": True,
        "message": str(stored.get("message") or MAINTENANCE_DEFAULT_MESSAGE),
        "back_at": stored.get("back_at"),
        "kick_at": int(stored.get("kick_at") or 0),
        "by": str(stored.get("by") or ""),
        "at": int(stored.get("at") or 0),
    })
    return state


def maintenance_seconds_left(state=None):
    """Seconds until sessions start ending. 0 once the grace window is spent."""
    if state is None:
        state = maintenance_state()
    if not state["on"]:
        return 0
    return max(0, int(state["kick_at"]) - int(time.time()))


def maintenance_public(state=None):
    """What a client is told. None when the server is open."""
    if state is None:
        state = maintenance_state()
    if not state["on"]:
        return None
    return {
        "on": True,
        "message": state["message"],
        "back_at": state["back_at"],
        "seconds_left": maintenance_seconds_left(state),
    }


def maintenance_refusal(username):
    """
    The front door. Returns a 503 response tuple while the server is closed, or
    None to let this login through.

    503 AND NOT 403: the credentials were never the problem, and a client that
    reads this as "wrong password" would send the player to reset one.
    """
    state = maintenance_state()
    if not state["on"] or has_owner_powers(username):
        return None
    return {
        "error": "Service Unavailable",
        "message": state["message"],
        "maintenance": True,
        "back_at": state["back_at"],
    }, 503


def maintenance_disconnect(user):
    """
    The room emptying. Returns a 401 response tuple once this session's grace
    window is spent, or None to let the request through.

    INSIDE THE WINDOW EVERY REQUEST PASSES, and that is the point - the save
    the client is racing to flush goes through this same decorator. A gate that
    refused writes during the grace window would guarantee the data loss the
    window exists to prevent.

    401, so it lands in the client's existing kicked-or-banned path: the
    heartbeat reads it as a revoked session and returns the player to the login
    screen, where /api/status explains why.
    """
    state = maintenance_state()
    if not state["on"] or has_owner_powers(user["username"]):
        return None
    if int(time.time()) < int(state["kick_at"]):
        return None

    db = get_db()
    db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(bearer_token()),))
    db.commit()
    return {
        "error": "Unauthorized",
        "message": state["message"],
        "maintenance": True,
    }, 401


def _end_all_player_sessions(db):
    """
    Sign out everyone but the owner (and a co-owner while the switch is on,
    who is exempt from the switch like the owner). Only used when the switch is
    thrown with no grace window at all, which means the caller has accepted the
    loss.
    """
    kept = [-1]
    for name in ((OWNER_USERNAME,) if OWNER_USERNAME else ()) + (
            CO_OWNER_USERNAMES if co_owners_on() else ()):
        row = _user_by_name(name)
        if row is not None:
            kept.append(int(row["id"]))
    cursor = db.execute("DELETE FROM sessions WHERE user_id NOT IN (%s)" % ",".join("?" * len(kept)),
                        tuple(kept))
    return max(0, int(cursor.rowcount or 0))


# =============================================================================
# BROADCASTS - THE SERVER'S VOICE
# =============================================================================
#
# One-way announcements from the server to everyone playing. This exists now,
# ahead of any player chat, because the kill switch needed it: a server that
# closes in sixty seconds has to be able to SAY SO to the people in the world,
# and until this there was no channel that could reach them at all.
#
# NOT THE CHAT SYSTEM. That is the section below, and it ended up a separate
# table rather than an author column here, for the reasons written over
# chat_messages in init_db(). What this still is, and what chat must never
# become, is the one channel that cannot be flooded, muted or argued with:
# nobody but the owner can post to it, so a restart notice always lands.

# Long enough for a real notice, short enough that it cannot be used to push
# anything else off a player's screen.
MAX_BROADCAST_LENGTH = 200

# Announcements are ephemeral by nature - a restart notice from last Tuesday
# helps nobody. Pruned on write so the table cannot grow without bound and no
# separate cleanup job has to exist.
BROADCAST_RETENTION_SECONDS = 7 * 24 * 60 * 60

# The most a single poll will return, so a client that has been away for a week
# cannot ask for the entire table in one response.
BROADCAST_PAGE_LIMIT = 50


def post_broadcast(body, kind="system", by=""):
    """
    Record one announcement. Caller commits - this runs inside their
    transaction, so the kill switch's notice cannot land without the switch
    itself also landing.
    """
    text = str(body or "").strip()[:MAX_BROADCAST_LENGTH]
    if text == "":
        return None

    db = get_db()
    now = int(time.time())
    cursor = db.execute(
        "INSERT INTO broadcasts (kind, body, created_by, created_at) VALUES (?, ?, ?, ?)",
        (str(kind or "system")[:24], text, str(by or "")[:64], now),
    )
    db.execute(
        "DELETE FROM broadcasts WHERE created_at < ?",
        (now - BROADCAST_RETENTION_SECONDS,),
    )
    return cursor.lastrowid


# =============================================================================
# WORLD CHAT - WHAT THE PLAYERS SAY - the chat/ package (since 18 Oct)
# =============================================================================
# World chat and its other channels, pictures in chat, taking a line down and
# the ignore list are in chat/; its __init__.py tells what each is. The routes
# register at CHAT ROUTES below, and hand back what other areas ask of chat -
# post_chat(), a report's "could they read it", and who ignores whom - which
# stay callable here under the names this file has always used.
import chat  # noqa: E402

# Named here as well, for the suites and anything else that reads them off this
# module; chat/services.py is where they are set.
MAX_CHAT_LENGTH = chat.MAX_CHAT_LENGTH
CHAT_RETENTION_SECONDS = chat.CHAT_RETENTION_SECONDS
CHAT_PAGE_LIMIT = chat.CHAT_PAGE_LIMIT
CHAT_DELETION_WINDOW_SECONDS = chat.CHAT_DELETION_WINDOW_SECONDS
CHAT_BUCKET_CAPACITY = chat.CHAT_BUCKET_CAPACITY
CHAT_TOKENS_PER_SECOND = chat.CHAT_TOKENS_PER_SECOND
CHAT_DEFAULT_CHANNEL = chat.CHAT_DEFAULT_CHANNEL

# PILLOW IS OPTIONAL AND THE SERVER STARTS WITHOUT IT. It is needed only to
# relay pictures into chat (chat/services.py imports it); everything else
# works untouched. An install that has not run `pip install Pillow` gets a
# clear refusal on the picture routes rather than a server that will not boot.
PILLOW_AVAILABLE = chat.PILLOW_AVAILABLE


# ONE LINE, AS TYPED. Everything a player types that other players will read
# goes through clean_player_text() before it is stored. Each rule below was a
# message that did real damage on a real screen:
#
#   - a newline, tab or other control character is a space. One message of
#     150 newlines was 150 blank lines: the whole chat window, wiped, by one
#     line the flood limit counted as one. A newline also let a player write
#     "12:00 [SERVER] Server restarting - log out now" on a line of its own.
#   - an invisible format character is dropped: bidi overrides printed a
#     message backwards, and a message of nothing but zero-width spaces was
#     an empty line under somebody's name. ZWJ (U+200D) stays - emoji like the
#     family are built with it.
#   - at most MAX_MARKS_PER_LETTER accents stack on one letter, so a pile of
#     combining marks cannot draw over the lines above and below it.
#   - runs of spaces become one.
MAX_MARKS_PER_LETTER = 3


def clean_player_text(raw, limit):
    out = []
    marks = 0
    for ch in str(raw or ""):
        kind = unicodedata.category(ch)
        if kind in ("Cc", "Zl", "Zp") or ch.isspace():
            ch = " "
        elif kind == "Cf" and ch != "\u200d":
            continue
        elif kind == "Cs":
            continue
        if kind in ("Mn", "Me"):
            marks += 1
            if marks > MAX_MARKS_PER_LETTER:
                continue
        else:
            marks = 0
        if ch == " " and (not out or out[-1] == " "):
            continue
        out.append(ch)
    return "".join(out).strip()[:limit].strip()


def post_chat(user_row, body, channel="world", target_id=0, image_id=""):
    """
    Record one player message. Caller commits, like post_broadcast(). The
    chat/ package's own (see CHAT ROUTES).
    """
    return _CHAT.post_chat(user_row, body, channel, target_id, image_id)


# =============================================================================
# CHANNELS
# =============================================================================
#
# FIVE NAMES, ONE TABLE. What separates them is who may write to a channel and
# who may read it back, and those two questions are answered in exactly two
# places - the write check and the SELECT in read_chat(), both chat/routes.py.
# Nothing else in the chat code knows there is more than one channel.

# =============================================================================
# GUILDS - the guilds/ package (since 18 Oct)
# =============================================================================
# The ten routes, the rules (the name, the size, the founding cost, the rank
# ladder, the history kept) and every write to the four guild tables are in
# guilds/; its __init__.py tells what a guild is and is not. The routes
# register at GUILD ROUTES below.
#
# WHAT OTHER AREAS ASK STAYS ASKABLE HERE: who is in which guild
# (guild_membership() - guild chat, the poll and the players list ask it) and
# how a guild's name is drawn (guild_tag() - every chat line, nameplate and the
# kingdom board). Both are the package's own; these names are what this file
# and the suites have always called them.
import guilds  # noqa: E402

guild_tag = guilds.guild_tag
MAX_GUILD_NAME = guilds.MAX_GUILD_NAME

# Named here as well, for the suites and anything else that reads them off this
# module; guilds/services.py is where they are set.
GUILD_FOUND_COST = guilds.GUILD_FOUND_COST
GUILD_EVENTS_KEPT = guilds.GUILD_EVENTS_KEPT
GUILD_ACTIVITY_SHOWN = guilds.GUILD_ACTIVITY_SHOWN
GUILD_EVENT_KINDS = guilds.GUILD_EVENT_KINDS
log_guild_event = guilds.log_guild_event


def guild_membership(user_id):
    """The caller's guild_members row, or None. Never raises on a stranger."""
    return guilds.membership(get_db(), user_id)


# =============================================================================
# PARTIES (game 0.42.0) - the parties/ package (since 18 Oct)
# =============================================================================
# The seven routes, voice's included, the rules (how big, how long an
# invitation waits) and every write to the three party tables are in parties/;
# its __init__.py tells what a party is. The routes register at PARTY ROUTES
# below.
#
# WHAT OTHER AREAS ASK STAYS ASKABLE HERE, under the names this file has always
# called it by: who is in which party (party_membership() - party chat asks),
# who is in a party (party_member_rows()), the invitations waiting on somebody
# (_party_invites_to() - the poll asks). All three are the package's own.
import parties  # noqa: E402

# WHAT PARTY CHAT SAYS TO SOMEBODY WITH NO PARTY, read or written - one
# sentence, as GUILD_CHAT_NO_GUILD is, saying where to go. Chat's, not the
# package's: it is a chat refusal.
PARTY_CHAT_NO_PARTY = "You are not in a party. Open Party to invite somebody, or answer an invite."


def party_membership(db, user_id):
    """The caller's party_members row, or None."""
    return parties.membership(db, user_id)


def party_member_rows(db, party_id):
    """A party's members, the leader first, then by when they joined."""
    return parties.member_rows(db, party_id)


def _party_invites_to(db, user_id, now=None):
    """Invitations waiting on this player's answer, newest first - the ones run
    out skipped. A read, on the poll every ten seconds."""
    return parties.invites_to(db, user_id, int(time.time()) if now is None else now)


def _own_party(db, user_id):
    """{"id", "leader", "members": [usernames]} for the broadcast poll - id 0
    and nobody for a player in no party. The game colours its party's names
    and draws them on the map from this, within one poll of any change."""
    seat = party_membership(db, user_id)
    if seat is None:
        return {"id": 0, "leader": "", "members": []}
    rows = party_member_rows(db, int(seat["party_id"]))
    return {"id": int(seat["party_id"]),
            "leader": next((r["username"] for r in rows if int(r["is_leader"] or 0)), ""),
            "members": [r["username"] for r in rows]}


# THE TAIL KEPT PER CHANNEL, on top of the 24 hours. A world channel on a busy
# night and a private conversation nobody has opened in a week are very
# different amounts of rows, and a single global cap would let the first one
# push the second out of the table. A TUNABLE OF THIS FILE'S, asked by chat/ on
# every line: test_chatrooms.py turns it down.
CHAT_KEEP_PER_CHANNEL = 400


# =============================================================================
# IGNORE, REPORT, MUTE
# =============================================================================
# Three tools for a busy world chat, one for each of the people in it.
#
#   IGNORE is the player's own: somebody's lines stop reaching you, in every
#   channel, and their whispers, friend requests and trades to you are refused.
#   Filtered in the read's own WHERE clause, like everything else in chat, so
#   there is no second pass to forget.
#
#   REPORT is how a player hands a line to staff. The line is copied into the
#   report, so it survives being deleted - which is usually what a report leads
#   to. Staff see open reports in their panel and a count on the poll.
#
#   MUTE is staff's, and smaller than a ban: the account keeps playing and
#   cannot speak until the time runs out. Every channel, pictures included.
#
# STAFF CANNOT BE IGNORED. A mod telling you to stop, and the owner announcing
# a restart in chat, have to arrive. A member of staff who is the problem is a
# report, like anybody else.

# THE TUNABLES a suite turns down, asked for on every use: the ignore list's
# length by chat/, the other three by moderation/. What a report may be about,
# how long a closed one is kept and the longest mute are moderation/services.py's.
IGNORE_LIMIT = 200
# Per reporter. Far above anybody reporting what they see; below somebody
# using the button to flood the staff panel.
REPORTS_PER_HOUR = 20
# The newest few of a reported player's lines on their Reports card.
REPORT_LINES_PER_PLAYER = 10
# A mod's mute is at most a day - longer is a ban's job, and a ban has a rank
# ladder of its own. Dev and the owner can go to thirty days
# (moderation/'s MUTE_MAX_MINUTES).
MUTE_MAX_MINUTES_MOD = 24 * 60

# APPENDED TO A CHAT READ'S WHERE, with the reader's id as its one parameter -
# the poll's news uses it too. chat/data.py is where it is written.
IGNORED_AUTHORS_CLAUSE = chat.IGNORED_AUTHORS_CLAUSE


def ignores(db, user_id, other_id):
    """Does `user_id` ignore `other_id`? The ignore list is chat/'s; trades,
    friends, parties and the players menu ask it here."""
    return chat.ignores(db, user_id, other_id)


def chat_mute_state(row, now=None):
    """None, or {"until", "seconds_left", "reason"} while `row` is muted."""
    if row is None:
        return None
    now = int(time.time()) if now is None else now
    try:
        until = _row_int(row, "chat_muted_until")
        reason = str(row["chat_mute_reason"] or "")
    except (KeyError, IndexError):
        return None
    if until <= now:
        return None
    return {"until": until, "seconds_left": until - now, "reason": reason}


def describe_mute(state):
    """The sentence a muted player reads."""
    line = "You are muted in chat for another %s." % _wait_words(state["seconds_left"])
    if state.get("reason"):
        line += " Reason: %s" % state["reason"]
    return line


def _can_read_chat_row(db, user_id, row):
    """Could `user_id` have read this line? The read's own rules, one line at a
    time - a report is only for a line you were shown. chat/'s own."""
    return _CHAT.can_read_row(db, user_id, row)


def _friend_ids(db, user_id):
    """The accounts that have accepted this one, as a set of ids."""
    rows = db.execute(
        "SELECT requester_id, addressee_id FROM friends"
        " WHERE state = 'accepted' AND (requester_id = ? OR addressee_id = ?)",
        (user_id, user_id),
    ).fetchall()
    out = set()
    for row in rows:
        other = int(row["addressee_id"]) if int(row["requester_id"]) == user_id \
            else int(row["requester_id"])
        out.add(other)
    return out


# =============================================================================
# PICTURES IN CHAT - THE RELAY'S ONE CALL OUT
# =============================================================================
# A player pastes a link; the SERVER fetches it, decides whether it is really
# a picture, and stores the bytes. Everyone else is shown the server's copy.
# Deciding and storing are chat/'s (services.decode_relayed_image(), the
# picture caps, who may see one); THE FETCH IS THIS FILE'S, handed in: it is
# the server reaching out to the internet, and every hop of it is checked
# against the server's own network here (_address_is_public(), which
# test_chatrooms.py replaces to fetch from a local test server).

# The input cap, chat/'s - see IMAGE_MAX_BYTES there - and the most a stored
# still's side may be. Named here for the fetch and for the suites.
IMAGE_MAX_BYTES = chat.IMAGE_MAX_BYTES
IMAGE_MAX_SIDE = chat.IMAGE_MAX_SIDE

# How long the fetch is allowed to take, and how many hops it may be moved
# through. Redirects are followed by hand so that EVERY hop is checked - an
# allowed host that 302s to 127.0.0.1 is the oldest trick there is.
IMAGE_FETCH_TIMEOUT = 6.0
IMAGE_MAX_REDIRECTS = 3


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Hands redirects back to the caller instead of following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _address_is_public(host):
    """
    False for anything that resolves to a machine on this network.

    THE POINT OF THE WHOLE FUNCTION is that the server is inside a network the
    internet is not. Left unchecked, "post a picture" is an invitation to read
    http://127.0.0.1:5000/api/..., a router's admin page, or a cloud
    provider's metadata service, with the server's own credentials and from
    the server's own address.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False

    # EVERY ADDRESS, not the first. A name can resolve to a public address and
    # a private one, and picking one at random is picking wrong half the time.
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved
                or address.is_unspecified):
            return False
    return True


def _image_url_ok(url):
    """(parsed, None) if this is a URL worth fetching, or (None, reason)."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None, "that is not a link"

    if parsed.scheme not in ("http", "https"):
        # file:// and ftp:// both read things this server should not read.
        return None, "only http and https links can be posted"
    if not parsed.hostname:
        return None, "that link has no host"
    if not _address_is_public(parsed.hostname):
        return None, "that link points inside the server's own network"
    return parsed, None


def _fetch_image_bytes(url):
    """
    (raw, None) or (None, reason). Follows redirects by hand, checking each.
    """
    seen = url
    for _hop in range(IMAGE_MAX_REDIRECTS + 1):
        parsed, why = _image_url_ok(seen)
        if parsed is None:
            return None, why

        request_obj = urllib.request.Request(seen, headers={
            "User-Agent": "ElusionRPG/1.0 (+chat image relay)",
            "Accept": "image/*",
        })
        opener = urllib.request.build_opener(_NoRedirects)
        try:
            with opener.open(request_obj, timeout=IMAGE_FETCH_TIMEOUT) as answer:
                kind = str(answer.headers.get("Content-Type", "")).split(";")[0].strip()
                if not kind.lower().startswith("image/"):
                    return None, "that link is not a picture"

                declared = answer.headers.get("Content-Length")
                if declared and int(declared) > IMAGE_MAX_BYTES:
                    return None, "that picture is bigger than %d MB" % (
                        IMAGE_MAX_BYTES // (1024 * 1024))

                # ONE BYTE PAST THE LIMIT, so a server that lies about (or
                # omits) Content-Length is still stopped by the read itself.
                raw = answer.read(IMAGE_MAX_BYTES + 1)
                if len(raw) > IMAGE_MAX_BYTES:
                    return None, "that picture is bigger than %d MB" % (
                        IMAGE_MAX_BYTES // (1024 * 1024))
                if not raw:
                    return None, "there was nothing at that link"
                return raw, None
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                target = error.headers.get("Location")
                if not target:
                    return None, "that link redirects to nowhere"
                seen = urllib.parse.urljoin(seen, target)
                continue
            return None, "that link answered %d" % error.code
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            return None, "that link could not be reached"
        except ValueError:
            return None, "that is not a link"

    return None, "that link redirects too many times"


# =============================================================================
# FRIENDS
# =============================================================================
#
# A request has to be accepted before either side appears on the other's list.
# The alternative - adding someone the moment you type their name - means
# anyone who knows your name can watch when you are online, and there is no
# point in a friends list that you cannot refuse entry to.
#
# ONE ROW PER PAIR, stored in the direction it was asked. See the comment over
# the friends table in init_db() for why it is one table and not two.
#
# THE ROUTES AND THEIR SQL ARE THE friends/ PACKAGE'S (16 Oct; FRIENDS - THE
# ROUTES, below). What stays here is what other areas share: the limits (this
# file's tunables), who is online, and how a person is shown.


# A list, not an address book. The cap is here so that neither the presence
# query nor the client's panel has to cope with an unbounded list, and 100 is
# far past what anyone will reach honestly.
MAX_FRIENDS = 100

# THE ANTI-NUISANCE NUMBER. Declining deletes the row, which means a request
# can always be sent again - so the thing that has to be bounded is how many
# can be outstanding at once. Twenty-five pending requests is someone asking
# everyone they can see, and they cannot ask anybody else until some are
# answered or withdrawn.
MAX_PENDING_SENT = 25


def _presence_for(db, user_ids):
    """
    {user_id: last_seen_at} for a set of accounts, in ONE query.

    Same shape as the staff account list, and for the same reason: a friends
    panel refreshing every few seconds must not turn into one query per friend.
    MAX() because one account can hold several live sessions.
    """
    if not user_ids:
        return {}

    now = int(time.time())
    marks = ",".join("?" for _ in user_ids)
    rows = db.execute(
        "SELECT user_id, MAX(last_seen_at) AS last_seen FROM sessions"
        " WHERE expires_at > ? AND user_id IN (%s) GROUP BY user_id" % marks,
        tuple([now] + list(user_ids)),
    ).fetchall()
    return {int(r["user_id"]): int(r["last_seen"] or 0) for r in rows}


def friend_entry(row, presence, now):
    """One account as a friends panel reads it."""
    last_seen = int(presence.get(int(row["id"]), 0))
    return {
        "username": row["username"],
        "role": role_for(row),
        "name_hue": name_hue_of(row),
        "online": last_seen > 0 and now - last_seen <= ONLINE_WINDOW_SECONDS,
        "last_seen_at": last_seen,
    }


# =============================================================================
# TELEPORT
# =============================================================================
#
# Moving one player, or everybody, to a place.
#
# THE SERVER DOES THE SPACING, not the clients. Sending a hundred players to
# one coordinate and hoping collision sorts it out is how you get a pile that
# shoves itself apart over several seconds, flinging people through walls on
# the way. Instead each target is given a SLOT, and the slot decides an offset
# on a hex ring around the destination. Every client computes the same answer
# from the same slot, so the group lands already spread out.
#
# HEX RINGS, NOT A SQUARE GRID. Ring r holds exactly 6r slots, and every slot
# is the same distance from its neighbours - which is the densest arrangement
# where nobody is closer to anyone else than TELEPORT_SPACING. A square grid
# puts diagonal neighbours 1.41x further apart than orthogonal ones, so it
# wastes space to guarantee the same minimum gap.
#
#   ring 0:  1 slot   (the destination itself)
#   ring 1:  6 slots  at 1x spacing
#   ring 2: 12 slots  at 2x spacing        ... 1 + 3r(r+1) slots up to ring r

# Far enough apart that two bodies cannot overlap - the player's collision
# circle is radius 7 - with room to spare so nobody is nudged on arrival.
TELEPORT_SPACING = 48.0


def teleport_ring_for_slot(index):
    """Which hex ring a slot falls on. Slot 0 is the centre, ring 0."""
    index = max(0, int(index))
    if index == 0:
        return 0
    ring = 1
    while 1 + 3 * ring * (ring + 1) <= index:
        ring += 1
    return ring


def teleport_offset(index):
    """
    (dx, dy) for one slot, in pixels from the destination.

    Pure arithmetic on purpose: no database, no state, no randomness. The same
    slot always gives the same offset, which is what lets the server hand out
    slots and every client agree about where everyone stands.
    """
    index = max(0, int(index))
    if index == 0:
        return 0.0, 0.0

    ring = teleport_ring_for_slot(index)
    first_in_ring = 1 + 3 * ring * (ring - 1)
    position_in_ring = index - first_in_ring
    slots_in_ring = 6 * ring

    angle = (2.0 * math.pi * position_in_ring) / slots_in_ring
    radius = ring * TELEPORT_SPACING
    return round(radius * math.cos(angle), 2), round(radius * math.sin(angle), 2)


def queue_teleport(db, user_id, area, x, y, slot_index, group_size, by, now):
    """Record one pending move, replacing anything already waiting for them."""
    db.execute(
        """
        INSERT INTO pending_teleports
               (id, user_id, area, x, y, slot_index, group_size, issued_by, issued_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            id         = excluded.id,
            area       = excluded.area,
            x          = excluded.x,
            y          = excluded.y,
            slot_index = excluded.slot_index,
            group_size = excluded.group_size,
            issued_by  = excluded.issued_by,
            issued_at  = excluded.issued_at
        """,
        (now, user_id, area, float(x), float(y), int(slot_index), int(group_size), by, now),
    )


def pending_teleport_for(user_id):
    """What this player should be told to do, or None. Shaped for the client."""
    try:
        row = get_db().execute(
            "SELECT * FROM pending_teleports WHERE user_id = ?", (user_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None

    dx, dy = teleport_offset(_row_int(row, "slot_index"))
    return {
        "id": _row_int(row, "id"),
        "area": row["area"],
        # WHERE THIS PLAYER LANDS, already spaced. The client does not repeat
        # the arithmetic and cannot disagree with it.
        "x": round(float(row["x"]) + dx, 2),
        "y": round(float(row["y"]) + dy, 2),
        "slot": _row_int(row, "slot_index"),
        "of": _row_int(row, "group_size"),
        "by": row["issued_by"],
    }


# =============================================================================
# WHICH REFUSAL CODE - THE RULE THIS FILE FOLLOWS
# =============================================================================
#
# The textbook is four sentences: authentication is who is asking, authorization
# is what they may do, 401 means authentication failed, 403 means authorization
# failed. This file obeys the first two and DELIBERATELY BREAKS THE THIRD, which
# is why a first read finds fifteen 403s and twelve 404s that are plainly
# authorization refusals and looks like a codebase that cannot make up its mind.
#
# It can. One rule, and all twenty-seven already follow it:
#
#     THE GATE THAT DECIDES WHETHER THE ROUTE IS YOURS ANSWERS 404.
#     THE CHECK ON WHAT YOU ASKED FOR, INSIDE A ROUTE ALREADY YOURS, ANSWERS 403.
#
# Read it as a question about the caller: does their own rank already admit them
# here?
#
#   NO   the refusal would BE the disclosure - it confirms a route, a rank ladder
#        or a guild that they have no standing to learn about. So: 404, the word
#        "Not found.", and no rank named anywhere in it. A 404 that explains
#        itself un-hides exactly what the status code hid.
#
#   YES  they are already inside. The refusal tells them nothing they did not
#        know, so it may as well say why. "A mod may ban for at most 30 days" is
#        useful and leaks nothing, BECAUSE ONLY A MOD CAN READ IT.
#
# So /api/staff/ban answers 404 to a player and 403 to a mod who asked for a
# permanent one. Same route, same gate, different question. Every 403 in this
# file is a check on an ARGUMENT - permanent vs temporary, which rank, everyone
# vs one player, level, class, bait, a rod.
#
# The one 403 that is not about an argument is the ban at /api/auth/login, and it
# is deliberate twice: it runs AFTER the password check so the route cannot be
# used to find out who is banned, and it includes the reason, because a banned
# player has every right to know and silence reads as the game being broken.
#
# THE 401s HAVE NO EXCEPTIONS. All eight are authentication - three are a token
# that resolved against a row since deleted (identity can no longer be
# established: late, but still authentication), the rest are a password that did
# not match. Which produces the fact the CLIENT depends on:
#
#     A 401 IS NOT A VERDICT ON THE SESSION.
#
# /api/auth/password and /api/account/email both require the CURRENT password and
# answer 401 when it is wrong, while the token stays perfectly live - a valid
# token is not proof of identity when the token may be the stolen thing. A client
# that signs people out on any 401 signs them out for typos. The Godot client
# asks /api/auth/session instead of deciding.
#
# test_refusals.py enforces every paragraph above, and F-1 reads the route list
# off the _elusion_min_role tags below rather than having one typed into it - so
# an /api/staff/ route added without a rank decorator fails the suite instead of
# quietly working for everybody.


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

        # TAGGED SO THE RULE CAN BE READ BACK. /api/staff/powers reports what
        # each rank may do, and the only way that list can be trusted is if it
        # is the DECORATORS talking rather than a copy someone maintains by
        # hand. functools.wraps copies __dict__ outward, so this survives being
        # wrapped again by require_auth above it.
        wrapped._elusion_min_role = minimum
        return wrapped
    return decorator


def require_fresh_password(view):
    """STEP-UP (TOKEN LIFECYCLE): the route needs the password typed within
    STEP_UP_SECONDS on a hardened session - POST /api/auth/confirm - or it
    answers 403 with confirm_password, and the game asks for it and tries
    again. Below @require_auth and the rank's own decorator. An unhardened
    session (an older game) is not asked: the minimum build is the switch."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = g.user
        if int(user["hardened"] or 0) and int(time.time()) - int(user["confirmed_at"] or 0) > STEP_UP_SECONDS:
            return {"error": "Forbidden", "message": STEP_UP_MESSAGE, "confirm_password": True}, 403
        return view(*args, **kwargs)

    wrapped._elusion_fresh_password = True
    return wrapped


def require_owner(view):
    """
    Decorator for the things only the server's owner may do - and, since
    0.21.0, a co-owner while the owner's switch is on (see CO_OWNER_USERNAMES).

    Deliberately NOT a check against a database column. See OWNER_USERNAME.

    TAGGED "coowner", the lowest rank it lets in, so /api/staff/powers lists
    these under the co-owner and the ladder reads true: the owner is above, and
    has them too.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not has_owner_powers(g.user["username"]):
            # 404, not 403. A 403 confirms the route exists and that you are
            # not allowed to use it, which tells an attacker where to aim.
            return {"error": "Not Found", "message": "Not found."}, 404
        return view(*args, **kwargs)

    wrapped._elusion_min_role = "coowner"
    return wrapped


def require_the_owner(view):
    """
    Decorator for what is THE owner's alone, a co-owner included out: the
    co-owners' own switch, so a co-owner can never keep it on.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_owner(g.user["username"]):
            return {"error": "Not Found", "message": "Not found."}, 404
        return view(*args, **kwargs)

    wrapped._elusion_min_role = "owner"
    return wrapped


# What a co-owner is told when the target is the owner. 403, not the usual
# 404: a co-owner knows who the owner is, and there is nothing to hide.
OWNER_UNTOUCHABLE = ({"error": "Forbidden",
                      "message": "That is the owner's account - a co-owner cannot change it."}, 403)


def owner_may_touch(actor, target):
    """
    Whether the owner's powers may be turned on `target` by `actor` - a give,
    a level, a rollback. The owner may touch anyone; a co-owner anyone but the
    owner, which is the one thing that keeps the owner above. Moderation has
    can_act_on() for this; these routes never asked, because only the owner
    reached them.
    """
    if target is None:
        return True
    return is_owner(actor["username"]) or not is_owner(target["username"])


def require_auth(view):
    """
    Decorator for any route that needs a logged-in user.
    Puts the user row on g.user so the view can read it.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = user_for_token(bearer_token())
        if user is None:
            # Only a refusal pays for this lookup, never a request that works.
            ended = _ended_refusal(bearer_token())
            if ended is not None:
                return ended
            return {
                "error": "Unauthorized",
                "message": "Missing, invalid or expired token.",
            }, 401
        g.user = user

        # THE KILL SWITCH, second half. One choke point for every
        # authenticated route, so a closed server cannot be outlasted by
        # whichever endpoint somebody forgot to gate. Passes straight
        # through while the server is open, and for the owner always.
        closing = maintenance_disconnect(user)
        if closing is not None:
            return closing

        return view(*args, **kwargs)

    wrapped._elusion_needs_auth = True
    return wrapped


# =============================================================================
# OPS
# =============================================================================

@app.get("/api/metrics")
@require_auth
@require_owner
def metrics():
    """
    Live per-endpoint request latency (owner only)
    ---
    tags:
      - Ops
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200:
        description: Per-endpoint count, error rate, p50/p95/p99/max latency, plus uptime
      404:
        description: Not the owner (same 404 the other owner routes give)
      401:
        description: Missing, invalid or expired token
    """
    return _metrics_summary(), 200


# =============================================================================
# AUTH - the auth/ package (since 19 Oct)
# =============================================================================
# The ten /api/auth routes - register, login, the heartbeat, resume, confirm,
# logout, logout-all, password, recover and reset - are auth/'s, registered
# below the code helpers and the mail they are handed (AUTH - THE ROUTES).
# What every request passes through stays in this file: the token check and
# the sessions it reads, issue_token() and the token lifecycle, the throttles
# and the login log, bans, the evasion checks and the install binding, the
# staff login code's switch (below), and the mail.


# =============================================================================
# STAFF LOGIN CODES
# =============================================================================
# A staff name is public: the owner's crown, the MOD and DEV badges, the
# players list. So anyone knows which account to aim a password guess at, and
# the lockout (LOGIN_MAX_ATTEMPTS) only slows that down.
#
# For a staff account with a confirmed recovery address, a correct password is
# not enough. The login answers 202 and emails a six-digit code; only the same
# login sent again WITH that code gets a token. A stolen or guessed password
# alone opens nothing.
#
# - Staff means mod and up: the ranks whose actions reach other players.
# - Only with a CONFIRMED address, and only while this server can send mail.
#   Without either there is nowhere to send a code, and refusing would lock the
#   owner out of their own server. Such a login goes through as before, and
#   says so: "staff_unprotected" in the answer, which the HUD says once, and
#   the boot log.
# - The code is the password-reset machinery: hashed, fifteen minutes, five
#   wrong tries burns it (_store_code and _consume_code, purpose "staff-login").
# - A wrong code is a failed login for the account lockout, so guessing codes
#   costs what guessing passwords costs. The streak only clears on a login
#   that gets its token - a correct password followed by a wrong code is still
#   a miss.
# - At most one new code a minute, so a leaked password cannot flood the inbox.
# - ELUSION_STAFF_LOGIN_CODES=off turns the step off. It is the way back in if
#   mail breaks after launch; see DEPLOY.md.
#
# THE STEP ITSELF IS auth/'s (since 19 Oct): the code's purpose, the resend
# minute, the trusted computers and the login's second step. What stays here
# is the switch and whether an account's logins take a code at all
# (staff_login_protected(), below) - the staff powers page asks both, and the
# suites turn the switch.
STAFF_LOGIN_CODES = os.environ.get("ELUSION_STAFF_LOGIN_CODES", "on").strip().lower() \
    not in ("off", "0", "false", "no")


# TRUSTED DEVICES - the code once per computer, not once per login - are
# auth/'s (services.py says how), and TRUSTED_DEVICE_DAYS and
# TRUSTED_DEVICES_KEPT are named here again where the package registers.


def staff_login_protected(row):
    """True when this account's logins take a code: staff, with a confirmed
    address, on a server that has the step on and can send mail."""
    return (STAFF_LOGIN_CODES and role_at_least(row, "mod")
            and not needs_recovery_email(row) and mail_can_send())


# =============================================================================
# ACCOUNT RECOVERY
# =============================================================================
#
# "I forgot my password" is the single most attacked endpoint in any game
# backend, because it is the one door that opens WITHOUT the password. Every
# rule below exists to stop it being easier than the front door.
#
# THE SHAPE: the player asks with their email, the server emails a six digit
# code, the player types it in and chooses a new password. The code is the only
# thing that unlocks the change.
#
# WHY A CODE AND NOT A LINK. A link needs a web page to land on, that page needs
# to reach the API from a browser, and the flow leaves the game to finish
# somewhere else. A code is typed straight into the client, so the whole thing
# happens on the login screen and there is no second surface to secure.
#
# THE FIVE THINGS THAT MAKE IT SAFE
#   1. It never says whether an address is on an account. Same answer, always.
#   2. The code is stored hashed, like a password, and expires in fifteen
#      minutes.
#   3. Five wrong guesses burns it. Six digits is only a million, so the
#      attempt ceiling is the real lock, not the length.
#   4. Only a VERIFIED address can receive one, so a typo at signup cannot
#      hand the account to a stranger.
#   5. A completed reset destroys every session on the account. If someone is
#      already inside, recovering the password throws them out - which is the
#      main reason a player reaches for this in the first place.

# Fifteen minutes: long enough to go and find the email, short enough that a
# code sitting in an inbox is not a standing key to the account.
RESET_CODE_TTL_SECONDS = 15 * 60

# Six digits is 1,000,000 possibilities. THIS is what makes that enough.
RESET_MAX_ATTEMPTS = 5

# One code per account per minute, so this endpoint cannot be used to bomb
# somebody's inbox, and per address so it cannot be used as a timing oracle.
RESET_REQUEST_COOLDOWN_SECONDS = 60

# And a ceiling per address for the same reason at a different scale: one
# machine working down a list of leaked emails.
RESET_MAX_REQUESTS_PER_IP = 10
RESET_IP_WINDOW_SECONDS = 15 * 60

# Deliberately loose. Address validity is proved by a code arriving at it, not
# by a regex - every strict email pattern ever written rejects somebody's real
# address, and this one only has to catch a typo like a missing @.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 254

def needs_recovery_email(row):
    """
    True when this account has no CONFIRMED recovery address.

    This is the one question the login screen asks before letting somebody into
    the world, and it is answered on every login, register and heartbeat so the
    client never has to go and ask separately.

    UNVERIFIED COUNTS AS NONE. A typed address nobody has proved they own is
    not a way back in, so it does not satisfy the prompt either - otherwise a
    typo would quietly leave the account unrecoverable, which is the exact
    situation the prompt exists to prevent.
    """
    # NOTHING TO ASK FOR WHEN NOTHING CAN BE SENT. The login screen will not
    # let a player past the prompt until a code arrives, so a server with no
    # mail set up - or one whose mail was never configured on the day - turned
    # every new player away at the door with "check that inbox". The same rule
    # the staff login code follows: it stands aside when there is nowhere to
    # send one, and the boot log says so.
    if not mail_can_send():
        return False
    if row is None:
        return True
    try:
        return not bool(_row_int(row, "email_verified"))
    except (KeyError, IndexError):
        # A row selected without the column: say yes rather than guess. Being
        # asked for an email you already gave is a nuisance; being skipped when
        # you have none is an unrecoverable account.
        return True


# The one answer /api/auth/recover ever gives. See the header: a different
# reply for "no such address" turns this into an account enumerator.
RECOVER_GENERIC_MESSAGE = (
    "If that address is on an account, a code is on its way. "
    "It expires in %d minutes." % (RESET_CODE_TTL_SECONDS // 60)
)


# =============================================================================
# SENDING MAIL
# =============================================================================
# Configured entirely from the environment so no provider is baked in and no
# credential is in the repo. Set these beside ELUSION_OWNER in .env:
#
#   ELUSION_SMTP_HOST      smtp.gmail.com, smtp.sendgrid.net, ...
#   ELUSION_SMTP_PORT      587 for STARTTLS (the default), 465 for SSL
#   ELUSION_SMTP_USER      the login for that server
#   ELUSION_SMTP_PASSWORD  its password or API key
#   ELUSION_MAIL_FROM      the address players will see it come from
#
# WITH NOTHING SET, MAIL DOES NOT SEND. That is the correct default: a server
# that silently swallows reset codes looks identical to one that works until a
# player needs it. ELUSION_MAIL_CONSOLE=1 prints the code to the server log
# instead, which is how you exercise the whole flow with no provider signed up.
#
# THAT SETTING IS FOR YOUR MACHINE ONLY. A printed code is a working key to an
# account sitting in a log file, so it is opt-in by name rather than tied to a
# debug flag somebody could leave on.
SMTP_HOST = os.environ.get("ELUSION_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("ELUSION_SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("ELUSION_SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("ELUSION_SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("ELUSION_MAIL_FROM", "").strip()
MAIL_CONSOLE = os.environ.get("ELUSION_MAIL_CONSOLE", "").strip() == "1"


def mail_can_send():
    """True when send_mail() has somewhere to send: a provider, or the console
    fallback. Read at call time, so a test can set the values it needs."""
    return MAIL_CONSOLE or bool(SMTP_HOST and MAIL_FROM)


# SAY SO AT BOOT, like the owner line. A staff login with no second step looks
# exactly like one with it, until somebody else has the password.
if not STAFF_LOGIN_CODES:
    print("[BOOT] staff login codes: OFF (ELUSION_STAFF_LOGIN_CODES). "
          "A staff password alone logs in.")
elif not mail_can_send():
    print("[BOOT] staff login codes: no mail set up, so there is nowhere to send "
          "one. A staff password alone logs in until ELUSION_SMTP_HOST and "
          "ELUSION_MAIL_FROM are set.")
else:
    print("[BOOT] staff login codes: on")


def send_mail(to_address, subject, body):
    """
    Deliver one message. Returns True if it left the building.

    NEVER RAISES. A mail server that is down must not turn into a 500 on an
    endpoint whose whole contract is to answer identically every time - the
    error goes to the log, and the caller still tells the player the same
    sentence it always does.
    """
    if MAIL_CONSOLE:
        app.logger.warning("[MAIL - console fallback] to=%s | %s | %s",
                           to_address, subject, body)
        return True

    if not SMTP_HOST or not MAIL_FROM:
        app.logger.error(
            "mail NOT sent to %s: ELUSION_SMTP_HOST/ELUSION_MAIL_FROM are unset. "
            "Set them, or ELUSION_MAIL_CONSOLE=1 for local testing.", to_address)
        return False

    message = EmailMessage()
    message["From"] = MAIL_FROM
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
            server.starttls()
        with server:
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(message)
        return True
    except Exception as error:
        # Deliberately broad: smtplib raises a dozen different things and every
        # one of them means the same thing here.
        app.logger.error("mail to %s failed: %s", to_address, error)
        return False


def send_mail_async(to_address, subject, body):
    """
    Hand the message to a background thread and return at once.

    THIS IS A SECURITY FIX, NOT A SPEED ONE. A real SMTP round trip - connect,
    STARTTLS, authenticate, send, quit - takes seconds. Done inside the request,
    /api/auth/recover answers in about four seconds when the address HAS an
    account and about a tenth of a second when it does not, because the second
    path has no mail to send. That gap is readable with a stopwatch, and it
    turns the endpoint into precisely the account enumerator that its identical
    wording, and the dummy hash beside it, exist to prevent. Same answer, same
    body, and now the same TIME.
    (Measured on a live server: 4505 ms with an account, ~100 ms without.)

    Fire and forget is correct here. The caller cannot act on a failure anyway -
    every one of these endpoints must answer identically whether or not mail
    went out - so a failure belongs in the log, which is where send_mail puts
    it. Nothing in the thread touches the database or the request context, so
    there is no app context to push and nothing to race.
    """
    threading.Thread(
        target=send_mail, args=(to_address, subject, body),
        name="elusion-mail", daemon=True,
    ).start()
    return True


# =============================================================================
# CODE HELPERS
# =============================================================================

def _new_code():
    """Six digits from the cryptographic generator, never `random`."""
    return "%06d" % secrets.randbelow(1000000)


def _store_code(db, user_id, purpose, code, target, now):
    """One live code per (account, purpose) - this replaces any previous one."""
    db.execute(
        """
        INSERT INTO auth_codes (user_id, purpose, code_hash, target,
                                expires_at, attempts, created_at)
        VALUES (?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT(user_id, purpose) DO UPDATE SET
            code_hash  = excluded.code_hash,
            target     = excluded.target,
            expires_at = excluded.expires_at,
            attempts   = 0,
            created_at = excluded.created_at
        """,
        (user_id, purpose, generate_password_hash(code), target,
         now + RESET_CODE_TTL_SECONDS, now),
    )


def _consume_code(db, user_id, purpose, code, now):
    """
    Check a code and spend it. Returns (ok, reason).

    Every failure reads the same to the caller on purpose - "wrong" and
    "expired" and "never asked" are one answer, because telling them apart
    tells an attacker which accounts have a code outstanding.
    """
    row = db.execute(
        "SELECT * FROM auth_codes WHERE user_id = ? AND purpose = ?",
        (user_id, purpose),
    ).fetchone()
    if row is None:
        return False, "no-code"

    if _row_int(row, "expires_at") <= now:
        db.execute("DELETE FROM auth_codes WHERE user_id = ? AND purpose = ?",
                   (user_id, purpose))
        return False, "expired"

    if _row_int(row, "attempts") >= RESET_MAX_ATTEMPTS:
        db.execute("DELETE FROM auth_codes WHERE user_id = ? AND purpose = ?",
                   (user_id, purpose))
        return False, "burned"

    if not check_password_hash(row["code_hash"], str(code)):
        db.execute(
            "UPDATE auth_codes SET attempts = attempts + 1 "
            "WHERE user_id = ? AND purpose = ?",
            (user_id, purpose),
        )
        # The LAST allowed miss burns it rather than leaving one more try.
        if _row_int(row, "attempts") + 1 >= RESET_MAX_ATTEMPTS:
            db.execute("DELETE FROM auth_codes WHERE user_id = ? AND purpose = ?",
                       (user_id, purpose))
        return False, "wrong"

    # SPENT ON SUCCESS. A code that still works after it has been used is a
    # second key left in the door.
    db.execute("DELETE FROM auth_codes WHERE user_id = ? AND purpose = ?",
               (user_id, purpose))
    return True, "ok"


def _clean_email(raw):
    """Normalised, or "" if it is not an address. Stored lowercase so lookups
    are a plain = against however the player capitalised it today."""
    address = str(raw or "").strip().lower()
    if len(address) > MAX_EMAIL_LENGTH or EMAIL_PATTERN.match(address) is None:
        return ""
    return address


def _mask_email(address):
    """t****n@example.com - enough for a player to recognise their own address,
    not enough for anyone else to learn it from a screenshot."""
    address = str(address or "")
    if "@" not in address:
        return ""
    name, domain = address.split("@", 1)
    if len(name) <= 2:
        return "%s@%s" % ("*" * len(name), domain)
    return "%s%s%s@%s" % (name[0], "*" * (len(name) - 2), name[-1], domain)


# THE RECOVERY ADDRESS'S THREE ROUTES - GET and POST /api/account/email and
# POST /api/account/email/verify - are the accountsettings/ package's since 19
# Oct, registered with the name colour at THE ACCOUNT'S SETTINGS, further down.
# The codes and the address's cleaning and masking are not theirs alone - the
# recovery routes and the staff login use them too - so they are handed in.


# =============================================================================
# AUTH - THE ROUTES - the auth/ package (since 19 Oct)
# =============================================================================
# The ten /api/auth routes, as they were here: endpoint names, docstrings,
# which take a token, which keep the write lock off, and every message
# (routes.lock). auth/__init__.py tells what each is for. Registered here,
# after the code helpers and the mail they are handed; what every request
# passes through - see AUTH, above - is this file's, handed in by name.
import auth as auth_package  # noqa: E402

auth_package.register(
    app,
    require_auth=require_auth,
    no_write_lock=no_write_lock,
    get_db=get_db,
    # Defined with the saves, just below.
    bad_request=lambda message: bad_request(message),
    client_ip=client_ip,
    validate_credentials=validate_credentials,
    hash_password=generate_password_hash,
    check_password=check_password_hash,
    dummy_hash=_TIMING_DUMMY_HASH,
    min_password_length=MIN_PASSWORD_LENGTH,
    maintenance_refusal=maintenance_refusal,
    maintenance_public=maintenance_public,
    prune_login_attempts=_prune_login_attempts,
    prune_sessions=_prune_sessions,
    ip_throttle_state=_ip_throttle_state,
    register_throttle_state=_register_throttle_state,
    record_login_attempt=_record_login_attempt,
    register_failed_login=_register_failed_login,
    wait_words=_wait_words,
    # ASKED ON EVERY CALL: test_throttle.py turns the window down.
    ip_window=lambda: IP_WINDOW_SECONDS,
    lockout_seconds=lambda: LOGIN_LOCKOUT_SECONDS,
    ban_state=ban_state,
    ban_evasion_state=_ban_evasion_state,
    install_evasion_state=_install_evasion_state,
    hash_install=_install_hash,
    record_account_ip=_record_account_ip,
    record_account_install=_record_account_install,
    issue_token=issue_token,
    login_hardening=login_hardening,
    end_other_sessions=_end_other_sessions,
    end_for_theft=_end_for_theft,
    hash_token=_token_hash,
    bearer_token=bearer_token,
    stamp_presence=stamp_presence,
    step_up_seconds=STEP_UP_SECONDS,
    confirm_max_misses=CONFIRM_MAX_MISSES,
    token_stolen_message=TOKEN_STOLEN_MESSAGE,
    role_for=role_for,
    default_role=DEFAULT_ROLE,
    has_owner_powers=has_owner_powers,
    role_at_least=role_at_least,
    name_hue_of=name_hue_of,
    row_int=_row_int,
    needs_recovery_email=needs_recovery_email,
    staff_login_protected=staff_login_protected,
    mask_email=_mask_email,
    make_code=_new_code,
    store_code=_store_code,
    consume_code=_consume_code,
    clean_email=_clean_email,
    # ASKED ON EVERY CALL: the suites replace it to read the codes it sends.
    send_mail_async=lambda to_address, subject, body: send_mail_async(to_address, subject, body),
    reset_code_ttl=RESET_CODE_TTL_SECONDS,
    recover_message=RECOVER_GENERIC_MESSAGE,
    reset_cooldown=RESET_REQUEST_COOLDOWN_SECONDS,
    reset_max_per_ip=RESET_MAX_REQUESTS_PER_IP,
    reset_ip_window=RESET_IP_WINDOW_SECONDS,
)

# Named here as well, for the staff powers page and the suites that read them
# off this module; auth/services.py is where they are set.
STAFF_LOGIN_CODE_PURPOSE = auth_package.STAFF_LOGIN_CODE_PURPOSE
STAFF_LOGIN_CODE_RESEND_SECONDS = auth_package.STAFF_LOGIN_CODE_RESEND_SECONDS
TRUSTED_DEVICE_DAYS = auth_package.TRUSTED_DEVICE_DAYS
TRUSTED_DEVICES_KEPT = auth_package.TRUSTED_DEVICES_KEPT


# =============================================================================
# SAVES  -  GET/PUT /api/save
# =============================================================================

MAX_SLOT = 3


def parse_slot(raw):
    """
    Validate a slot number coming from a query string or a JSON body.

    Note the bool check: in Python `True == 1` and `isinstance(True, int)` is
    True, so without it `{"slot": true}` would quietly be accepted as slot 1.

    A WHOLE NUMBER, FLOAT OR NOT. Godot sends every number as a float, so 1.0
    is slot 1 - but int() truncates, and 1.5 used to be slot 1 as well. Found
    by the character delete route's tests, where "delete slot 1.5" deleted
    slot 1.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float) and not raw.is_integer():
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


def parse_cell(payload, capacity):
    """
    The optional `position` on a request that takes an item out of a grid:
    which cell the player actually used. Returns (cell_or_None, error_or_None).

    Absent means "no preference", and the take falls back to its usual order.
    Present means a real cell of that grid or a 400 - a malformed position is a
    client bug worth hearing about, not a hint worth guessing at. Same bool
    trap as parse_slot().
    """
    if "position" not in payload or payload.get("position") is None:
        return None, None
    raw = payload.get("position")
    if isinstance(raw, bool):
        return None, bad_request("position must be an integer 0-%d" % (capacity - 1))
    try:
        cell = int(raw)
    except (TypeError, ValueError):
        return None, bad_request("position must be an integer 0-%d" % (capacity - 1))
    if cell < 0 or cell >= capacity:
        return None, bad_request("position must be an integer 0-%d" % (capacity - 1))
    return cell, None


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

# parse_equipment() WAS HERE: it validated a whole equipment map a save sent.
# Nothing has sent one it stores since equipment became server-owned - the
# save ignores the field - and the equip routes ask gamedata.equip_check() for
# one item at a time. Removed on 18 Oct (UPGRADES.md) rather than left looking
# like a guard that guards nothing.


def _stored_json(raw, fallback):
    """Read one of the JSON columns back, forgiving anything unreadable.

    A row written by a future version, or corrupted by hand, must not make the
    save endpoint 500 - the character is still playable without its explored
    map."""
    try:
        value = json.loads(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _derived_stats(row, class_id=None, level=None):
    """
    gamedata.max_stats_for() for this saves row, WITH WHAT IT IS WEARING.

    The one way app.py asks for a character's maxima. A Vitality amulet adds to
    max_hp, and the status route clamps hp to whatever this returns - so a call
    that left the equipment out would give a character a lower ceiling than
    their own client shows and take the difference off them on the next save.

    row["equipment"] IS READ, NOT .get()'d. A SELECT that forgot the column
    raises IndexError here and fails loudly in the tests, rather than quietly
    deriving a bare maximum. class_id and level override the row's own when the
    caller knows better (the save route's class, the kill route's new level).
    """
    return gamedata.max_stats_for(
        row["class_id"] if class_id is None else class_id,
        int(row["level"] if level is None else level),
        _stored_json(row["equipment"], {}),
    )


# GET AND PUT /api/save ARE saves/'s since 18 Oct, with GET /api/character and
# deleting a character: registered at THE CHARACTER - the saves/ package,
# further down, after everything it is handed. What stays here is what every
# route shares: the slot, the 400, a cell, the JSON-column reader and
# _derived_stats().


# =============================================================================
# PLAYER STATUS - the status/ package (since 18 Oct)
# =============================================================================
# GET and PUT /api/player/status, and the check that every rise in a pool was
# earned (_reconcile_heals()), are status/'s - registered below, after the
# tunables it is handed. What stays here: the stat ceiling and its parser,
# what a coin is worth, the healing tunables and the grant ids every refill
# writes, and the boot check that says which protections are not armed.

# THE CEILING ON ANY STAT A REQUEST NAMES (parse_stat()). Nothing in this game
# legitimately exceeds it; a number past it means a corrupted save, an
# overflow, or someone editing packets - all three are worth refusing rather
# than storing.
STAT_CEILING = 1_000_000_000

# The two item ids that resolve into carried gold rather than a backpack cell.
# Read from gamedata so the authored source stays baseenemy.gd's constants.
CONSTANTS_GOLD_SMALL = gamedata.CONSTANTS.get("gold_small_id", "smallamountofgold")
CONSTANTS_GOLD_LARGE = gamedata.CONSTANTS.get("gold_large_id", "largeamountofgold")


# WHAT ONE LUSION IS WORTH IN GOLD.
#
# Needed the moment two currencies are added into one number - the score - and
# stated once here rather than inlined at the place that adds them, because
# the day it is tuned it must move everywhere at once.
#
# WHERE 1000 COMES FROM. It is one gold coin on the denomination ladder, which
# makes the premium currency legible against the thing players actually count:
# a lusion is a gold coin. It also lands the two revive paths near each other
# in cost - the lusion revive is 20 lusions, so 20,000 gold, and the gold
# revive takes 80% of what you hold, which is the same bill for somebody
# carrying about 25,000. A player choosing between them is making a real
# choice rather than an obvious one.
#
# IT IS NOT AN EXCHANGE RATE PLAYERS CAN TRADE AT, and nothing here lets them.
# It is the weight used when one number has to describe both.
LUSION_GOLD_VALUE = int(gamedata.CONSTANTS.get("lusion_gold_value", 1000))


def gold_item_value(item_id):
    """What one of this coin is worth, or 0 if it is not a gold coin at all.

    THE ONE PLACE THAT ANSWERS "IS THIS GOLD, AND HOW MUCH". Loot used to
    credit the QUANTITY as gold, which was right only while every gold item
    was worth exactly 1. With a real ladder - a platinum coin is a hundred
    thousand - crediting the count would pay out a hundred thousandth of the
    drop. The client already multiplied by value; this is the server catching
    up to it, and the two now agree by doing the same arithmetic.
    """
    premium = gamedata.CONSTANTS.get("lusions_item_id", LUSIONS_ITEM_ID)
    if item_id == premium:
        return 0
    record = gamedata.item_row(item_id) or {}
    if str(record.get("type_name", "")) != "CURRENCY":
        # The two legacy ids are still honoured by name even if a data file
        # ever stops labelling them, because old bags and old saves hold them.
        if item_id in (CONSTANTS_GOLD_SMALL, CONSTANTS_GOLD_LARGE):
            return 1
        return 0
    try:
        return max(0, int(record.get("value", 0) or 0))
    except (TypeError, ValueError):
        return 0


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

# And the same for the OTHER way out of the death screen - the one that pays
# nothing and loses everything carried. It fills the same three pools, so it
# explains a rise for exactly the same reason; it gets its own id only so the
# log can say which of the two happened.
RESPAWN_GRANT_ID = "__respawn__"

# And the same for a level-up. player.gd::level_up() calls
# _fill_all_resources(), so every level legitimately fills all three pools
# at once - the one remaining client-side refill that is supposed to happen.
# The server is the thing that decides a level-up (it owns level and grants
# the XP at the kill), so it is also the thing that can say so.
#
# WITHOUT THIS the check would flag every level a player ever gains, which is
# the surest way to make a log nobody reads.
LEVELUP_GRANT_ID = "__levelup__"


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
        "the catalogue cannot say which slot an item goes in, so every equip "
        "is refused as not equipment, and the slot names fall back to "
        "EQUIP_SLOTS_FALLBACK",
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


# THE STATUS ROUTES - the status/ package (see PLAYER STATUS). Handed the
# healing tunables above as lookups, so the switch stays here, where its
# history is, and a flip of it is heard on the next check.
import status as status_package  # noqa: E402

_STATUS = status_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    parse_stat=parse_stat,
    stat_ceiling=STAT_CEILING,
    derived_stats=_derived_stats,
    catalogue=gamedata,
    heal_margin=lambda: HEAL_ALLOWANCE_MARGIN,
    clamp_margin=lambda: HEAL_CLAMP_MARGIN,
    clamp_enforced=lambda: HEAL_CLAMP_ENFORCED,
    minimum_elapsed=lambda: HEAL_MINIMUM_ELAPSED_SECONDS,
    explain_window=lambda: HEAL_EXPLAIN_WINDOW_SECONDS,
    refill_grants=(REVIVE_GRANT_ID, RESPAWN_GRANT_ID, LEVELUP_GRANT_ID),
)
status_payload = _STATUS.status_of
_reconcile_heals = _STATUS.reconcile_heals
# THE ONE WRITER OF A REFILL'S NOTE - a potion's, a revive's, a respawn's, a
# level-up's - which the healing check reads (status/data.py, note_refill()).
# Every area that authorises a pool to rise is handed it, below: consuming/,
# death/, kill/ and ownertools/, each registered after this.
_note_refill = _STATUS.note_refill

# Named here as well, for anything that reads them off this module;
# status/services.py is where they are set.
STATUS_FIELDS = status_package.STATUS_FIELDS
SERVER_OWNED_STATS = status_package.SERVER_OWNED_STATS
DERIVED_STATS = status_package.DERIVED_STATS


# =============================================================================
# THE BANK AND THE ACCOUNT
# =============================================================================
#
# THE ROUTES ARE bank/'s since 18 Oct, registered further down with the carry's
# sizes they are handed (THE BANK AND THE ACCOUNT - the bank/ package). What is
# here: the ledgers, the account row, and how a request names a cell. What the
# backpack and the bank share - taking from cells, a move, a discard - is
# carry/'s since 18 Oct. The history below is the bank's, and stays with the
# grids it explains.
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


# =============================================================================
# ONE CELL AT A TIME - moving and discarding inside a grid
# =============================================================================
# THE BACKPACK AND THE BANK ARE SERVER-OWNED. They used to be whatever the game
# last sent as a whole array, trimmed to what the server had granted
# (_reconcile_inventory, E-1): a gain was caught, but the arrangement and every
# loss were the client's word. Now each thing a player does to a grid is one
# request the server carries out on its own copy - move, discard, use a pile -
# and answers with the grid as it stands. The whole-array writes are kept for
# staff tooling only (see write_inventory()).
#
# THE CLIENT NAMES WHAT IT SAW. A move or a discard says which item it expects
# in the source cell. When the server's cell holds something else, the
# client's picture is out of date - a trade finished, a loot take landed - and
# acting on the cell would move or destroy the wrong thing. That is a 409 that
# carries the grid, so the game adopts it in the same breath, and nothing is
# changed.

# THE MOVE AND THE DISCARD ARE carry/'s since 18 Oct (carry/routes.py, the
# SQL in carry/data.py), with taking from cells: one copy for both grids,
# named by table, handed back at THE CARRY AND THE SKILLS and to the bank
# under the names they had here - _grid_move(), _grid_discard(),
# _take_from_cells(), _apply_cell_changes(). What stays is how a request
# names a cell and an item.


def parse_grid_cell(payload, key, capacity):
    """A required cell index named `key`: (cell, None) or (None, a 400). The
    same rules as parse_cell(), but the cell is not optional here."""
    raw = payload.get(key)
    if raw is None or isinstance(raw, bool):
        return None, bad_request("%s must be an integer 0-%d" % (key, capacity - 1))
    try:
        cell = int(raw)
    except (TypeError, ValueError):
        return None, bad_request("%s must be an integer 0-%d" % (key, capacity - 1))
    if cell < 0 or cell >= capacity:
        return None, bad_request("%s must be an integer 0-%d" % (key, capacity - 1))
    return cell, None


def parse_item_id(payload):
    """The item a request names: (item_id, None) or (None, a 400)."""
    item_id = str(payload.get("item_id", "")).strip()
    if not item_id or len(item_id) > 64:
        return None, bad_request("item_id must be 1-64 characters")
    return item_id, None


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


# =============================================================================
# THE ACCOUNT'S SETTINGS - the accountsettings/ package (since 19 Oct)
# =============================================================================
# The recovery address (GET and POST /api/account/email, POST .../verify) and
# the name colour (PUT /api/account/name-colour), as they were here: endpoint
# names, docstrings, the write-lock marks and every message (routes.lock).
# accountsettings/__init__.py says why the address needs the password and a
# code. Handed what other areas share - the mail, the codes, the address's
# cleaning and masking, the login log - each asked for when a request lands:
# test_accounts.py stands in for the mail on this module, and the codes and
# the address helpers belong with the recovery routes and the staff login.
import accountsettings as accountsettings_package  # noqa: E402

accountsettings_package.register(
    app,
    require_auth=require_auth,
    no_write_lock=no_write_lock,
    get_db=get_db,
    bad_request=bad_request,
    parse_stat=parse_stat,
    hue_max=NAME_HUE_MAX,
    clean_email=lambda raw: _clean_email(raw),
    mask_email=lambda address: _mask_email(address),
    row_int=lambda row, key: _row_int(row, key),
    record_login_attempt=lambda db, username, ip, ok, reason="": _record_login_attempt(db, username, ip, ok, reason),
    client_ip=lambda: client_ip(),
    mail_can_send=lambda: mail_can_send(),
    send_mail_async=lambda to_address, subject, body: send_mail_async(to_address, subject, body),
    new_code=lambda: _new_code(),
    store_code=lambda db, user_id, purpose, code, target, now: _store_code(db, user_id, purpose, code, target, now),
    consume_code=lambda db, user_id, purpose, code, now: _consume_code(db, user_id, purpose, code, now),
    code_ttl=lambda: RESET_CODE_TTL_SECONDS,
)


# =============================================================================
# COMBAT - KILL REWARDS - the kill/ package (since 18 Oct)
# =============================================================================
# POST /api/combat/kill, as it was here (routes.lock): the client says which
# monster died and the server decides what that paid. kill/__init__.py tells
# the story, kill/services.py holds the token bucket and the spawn ceiling with
# their arithmetic, and kill/data.py writes the kill's two records. Handed what
# other areas share: the maxima, the class specialty, the broadcast line - and
# the skills' grant, the stirring lands and the loot bag's store, all defined
# further down, so asked for when a kill lands.
import kill as kill_package  # noqa: E402

kill_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    catalogue=gamedata,
    stir_for=lambda db, enemy_id: world_stir_for(db, enemy_id),
    derived_stats=_derived_stats,
    levelup_grant_id=LEVELUP_GRANT_ID,
    proficient_amount=proficient_amount,
    grant_skill_xp=lambda user_id, slot, skill_id, gained: _grant_skill_xp(user_id, slot, skill_id, gained),
    post_broadcast=post_broadcast,
    create_loot_bag=lambda user_id, slot, enemy_id, contents: _create_loot_bag(user_id, slot, enemy_id, contents),
    note_refill=_note_refill,
)

# Named here as well, for the suites and the notes that read them off this
# module; kill/services.py is where they are set.
KILL_BUCKET_CAPACITY = kill_package.KILL_BUCKET_CAPACITY
KILL_TOKENS_PER_SECOND = kill_package.KILL_TOKENS_PER_SECOND
KILL_WINDOW_SECONDS = kill_package.KILL_WINDOW_SECONDS
KILL_RESPAWN_FLOOR_SECONDS = kill_package.KILL_RESPAWN_FLOOR_SECONDS
KILL_LOG_RETENTION_SECONDS = kill_package.KILL_LOG_RETENTION_SECONDS
MYTHIC_BROADCAST_KIND = kill_package.MYTHIC_BROADCAST_KIND


def mythic_find_text(username, item_id, enemy_id):
    """What the world is told when a mythic drops, named from this server's
    own catalogue - kill/services.py's mythic_find_text()."""
    return kill_package.mythic_find_text(gamedata, username, item_id, enemy_id)


# =============================================================================
# MODERATION
# =============================================================================
# The sanctions (ban, kick, unban, a rank), the reports and the mutes: the
# moderation/ package since 18 Oct, all ten routes and every write to
# chat_reports. Every one is require_role("mod") at the door and within the
# caller's reach inside (can_act_on(), through _moderation_target()), and the
# refusal is the same 404 for nobody and for somebody above you. The rank
# ladder, the moderation log's writer and the mute's own reading
# (chat_mute_state()) stay above, shared by every staff area and by chat.
import moderation as moderation_package  # noqa: E402


# WHO COUNTS AS ONLINE: a session whose client called GET /api/auth/session
# within this many seconds. The game sends that heartbeat every 15 seconds, so
# 45 is three missed beats - one slow request does not flicker someone
# offline, and a closed game drops off the list within the minute.
#
# NOT "has a live session". Sessions last thirty days and survive the game
# being closed, so that count answered "who has logged in this month", and a
# kick list sorted by it put last week's visitors at the top.
ONLINE_WINDOW_SECONDS = 45


_MODERATION = moderation_package.register(
    app,
    require_auth=require_auth,
    require_role=require_role,
    require_fresh_password=require_fresh_password,
    get_db=get_db,
    bad_request=bad_request,
    parse_stat=parse_stat,
    moderation_target=_moderation_target,
    find_user=_user_by_name,
    can_act_on=can_act_on,
    role_for=role_for,
    role_at_least=role_at_least,
    roles=ROLES,
    settable_roles=SETTABLE_ROLES,
    log_staff_action=log_staff_action,
    can_read_row=_can_read_chat_row,
    wait_words=_wait_words,
    default_channel=CHAT_DEFAULT_CHANNEL,
    # THE TUNABLES A SUITE TURNS DOWN, asked for on every use (IGNORE, REPORT,
    # MUTE, above).
    reports_per_hour=lambda: REPORTS_PER_HOUR,
    report_lines_per_player=lambda: REPORT_LINES_PER_PLAYER,
    mute_max_minutes_mod=lambda: MUTE_MAX_MINUTES_MOD,
)
# The staff numbers on the broadcast poll, and the limits the powers page
# quotes, by the names this file always used.
_open_report_count = _MODERATION.open_lines
_open_report_players = _MODERATION.open_players
MAX_MOD_BAN_DAYS = moderation_package.MAX_MOD_BAN_DAYS
MUTE_MAX_MINUTES = moderation_package.MUTE_MAX_MINUTES
REPORT_KEEP_SECONDS = moderation_package.REPORT_KEEP_SECONDS


# =============================================================================
# THE OWNER'S TOOLS - the ownertools/ package (since 19 Oct)
# =============================================================================
# Gold, a level, a skill and an item for testing or for a player, the gifts
# report, the save history and the rollback, and moving players: nine routes,
# registered further down (after the trade routes), once everything they are
# handed exists. What the poll, the save route and the boot backfill share
# with them stays here: the gift's flag, the gifts ledger's writers, taking a
# snapshot, the teleport queue.


# A GIFT RIDES THE TRADE FLAG. saves.resync_trade is "this character's game is
# holding an old bag; hand it the new one" - set by a trade, read and cleared by
# _take_resync() on whichever of three polls reaches the game first. A staff
# gift needs exactly that, plus a line saying what arrived, so the column holds
# GIFT_MARK and a short JSON list instead of a trade id. A trade id is a hex
# token and never starts with the mark.
#
# A TRADE'S RESULT WINS. If one is still waiting to be shown, the gift leaves the
# flag alone: the bag the poll hands over is read at delivery, so it holds the
# gift anyway, and the trade's line is the one a player needs to read. A trade
# that finishes after a gift overwrites the gift's line the same way.
GIFT_MARK = "gift:"
GIFTS_KEPT = 5


def _gifts_in(marker):
    """The gifts a resync_trade value carries; [] for a trade id or nothing."""
    if not marker or not str(marker).startswith(GIFT_MARK):
        return []
    try:
        value = json.loads(str(marker)[len(GIFT_MARK):])
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _mark_gift(db, user_id, slot, by, item_id, quantity):
    """Flag this character's bag as changed by a gift. NO COMMIT - the grant's."""
    row = db.execute("SELECT resync_trade FROM saves WHERE user_id = ? AND slot = ?",
                     (user_id, slot)).fetchone()
    if row is None:
        return
    held = row["resync_trade"]
    if held is not None and not str(held).startswith(GIFT_MARK):
        return
    gifts = _gifts_in(held)
    gifts.append({"by": str(by), "item_id": str(item_id), "quantity": int(quantity)})
    db.execute("UPDATE saves SET resync_trade = ? WHERE user_id = ? AND slot = ?",
               (GIFT_MARK + json.dumps(gifts[-GIFTS_KEPT:]), user_id, slot))


# =============================================================================
# THE GIFTS LEDGER
# =============================================================================
#
# The owner, 10 Oct, after eleven clicks of Give item put 10,990 Piles of Gold
# - 274,750,000 gold - in the bag of the first other player to try the game:
# "do not roll back but make a ledger for anthing i give to players so its
# accounted for if i ever ask how much did i inflate my server".
#
# EVERY GIFT IS A ROW in owner_gifts, written in the same transaction as the
# gift: a give to a player, a grant to the owner himself (staff_grant) and the
# owner's own gold (staff_gold, bank or purse; taking some back is a negative
# row) - the routes are ownertools/'s, the writers below. Since 0.21.0 a
# co-owner's gifts too, with his name as the giver (`actor_name`): "to
# yourself" is a giver to his own account, and the report says who gave what
# (`by_giver`). What it was worth is worked out then (gift_worth()) and kept,
# so a price changed later does not rewrite history:
#
#     kind "gold"     coins and piles, at what using them pays (gold_item_value),
#                     and gold given straight; "gold" is that amount
#     kind "lusions"  a pile of lusions; "lusions" is what it cashes into
#     kind "item"     anything else: "value" is the catalogue's price for it,
#                     "sells_for" what the general store would pay - the gold
#                     it can still turn into
#
# A RECORD, NOT MONEY. The gold supply is still gold_ledger's, and a pile of
# gold given and never used is not in it yet - so the ledger counts a gift at
# what it CAN become, on the day it is given, and the report sets that beside
# the gold actually in purses and banks now.
#
# THE PAST IS IN THE LOGS, so it is read back, once (_backfill_owner_gifts(),
# at the end of this file): every "give" and "grant" in staff_actions and every
# staff_gold row in gold_ledger, marked source "log". A tally that started
# today would answer "how much have I given" with "nothing yet" (CLAUDE.md, "A
# counter added by migration starts at zero").
#
# READ by GET /api/staff/gifts (ownertools/'s gift_report(), owner only) and by
# giftwatch.py on the server. test_gifts.py.

OWNER_GIFTS_SINCE_KEY = "owner_gifts_since"
# Whose prices "sells_for" is: the one shop there is.
GIFT_SHOP_ID = "generalstore"


def money_item_worth(item_id):
    """("gold", each), ("lusions", each) or (None, 0): what a carried pile of
    this cashes into, one at a time. THE ONE ANSWER for the cash route and the
    gifts ledger."""
    premium = gamedata.CONSTANTS.get("lusions_item_id", LUSIONS_ITEM_ID)
    if item_id == premium:
        return "lusions", max(1, int((gamedata.item_row(item_id) or {}).get("value", 1) or 1))
    per = gold_item_value(item_id)
    if per > 0:
        return "gold", per
    return None, 0


def gift_worth(item_id, quantity):
    """{kind, gold, lusions, value, sells_for} for `quantity` of `item_id`."""
    quantity = int(quantity)
    currency, per = money_item_worth(item_id)
    if currency == "gold":
        return {"kind": "gold", "gold": per * quantity, "lusions": 0, "value": 0, "sells_for": 0}
    if currency == "lusions":
        return {"kind": "lusions", "gold": 0, "lusions": per * quantity, "value": 0, "sells_for": 0}
    row = gamedata.item_row(item_id) or {}
    try:
        each = max(0, int(row.get("value", 0) or 0))
    except (TypeError, ValueError):
        each = 0
    sells = gamedata.shop_sell_price(GIFT_SHOP_ID, item_id) or 0
    return {"kind": "item", "gold": 0, "lusions": 0, "value": each * quantity,
            "sells_for": int(sells) * quantity}


def _record_gift(db, actor, receiver_id, receiver_name, slot, item_id, quantity, source, at=None):
    """A gift of an item, written to owner_gifts. NO COMMIT - the gift's own.
    Returns what it was worth."""
    worth = gift_worth(item_id, quantity)
    db.execute(
        "INSERT INTO owner_gifts (at, actor_id, actor_name, user_id, username, slot, kind, item_id,"
        " quantity, gold, lusions, value, sells_for, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(at if at is not None else time.time()), actor["id"], str(actor["username"]),
         receiver_id, str(receiver_name), slot, worth["kind"], str(item_id), int(quantity),
         worth["gold"], worth["lusions"], worth["value"], worth["sells_for"], source),
    )
    return worth


def _record_gold_gift(db, actor, receiver_id, receiver_name, slot, amount, source="staff_gold", at=None):
    """Gold given straight - a purse (slot) or the bank (slot None). NO COMMIT."""
    db.execute(
        "INSERT INTO owner_gifts (at, actor_id, actor_name, user_id, username, slot, kind, item_id,"
        " quantity, gold, source) VALUES (?,?,?,?,?,?,'gold','',?,?,?)",
        (int(at if at is not None else time.time()), actor["id"], str(actor["username"]),
         receiver_id, str(receiver_name), slot, int(amount), int(amount), source),
    )


# =============================================================================
# SAVE SNAPSHOTS AND ROLLBACK
# =============================================================================
#
# The owner, 6 Oct: "roll back ... might be useful" - a player who lost a
# stack to a bug, a dupe to undo, a gift given to the wrong name. The server
# keeps each character as it was, a few times over, and one owner-only route
# puts one back. The two routes - the history and the rollback - are
# ownertools/'s since 19 Oct; taking a snapshot, and what one holds, stay here,
# because the save route takes them too.
#
# WHAT A SNAPSHOT HOLDS: everything about one character a mistake can change -
# level, xp, the purse, what is worn, the pet that is out, every carried cell
# (bag and hotbar) and every skill. NOT the bank or lusions: they are the
# account's, shared by all four characters, and putting one character back must
# not reach into the other three. NOT the area or the explored map: where
# somebody stands and what they have seen are not what a rollback is for. NOT
# hp, mana or stamina: a rollback keeps today's pools (brought down to the old
# maxima if those are lower), so it never doubles as a free heal.
#
# WHEN ONE IS TAKEN:
#   save             on PUT /api/save, at most every SNAPSHOT_EVERY_SECONDS
#   before-give      before the owner puts an item in somebody's bag
#   before-rollback  before a rollback - so a rollback can itself be undone
#   before-level     before the owner sets somebody else's level (0.21.0)
# and never when nothing has changed since the newest one (`fingerprint`), so an
# afternoon standing in town does not push the useful history out. The newest
# SNAPSHOTS_KEPT per character are kept, pruned on write, and a character's go
# with it when it is deleted (character_deletions keeps its own copy).
#
# OWNER ONLY, and the reason is the honest limit: a rollback can only put back
# what this character holds. An item it has since traded, banked, sold or
# dropped is somewhere else now and stays there - so a snapshot from before a
# trade gives the item back AND leaves it with the other player. That is a
# duplicate the owner made on purpose, a judgement about one player's history,
# and the same kind of power as creating an item. SECURITY.md says so.
#
# THE GOLD GOES THROUGH THE LEDGER (`staff_rollback`), up or down, so the
# supply invariant holds - and, like every burn, gold a rollback takes away is
# counted on the Kingdom board as given.
#
# THEIR GAME RELOADS. Every session of the account is ended, as a kick ends
# them: the game sees the 401 on its next poll, goes to the login screen, and
# signing in loads the character from here. The game holds its own copy of
# level, gold and gear, and nothing short of a reload replaces all of it. The
# owner rolling back their own character is signed out the same way.
SNAPSHOT_EVERY_SECONDS = 600
SNAPSHOTS_KEPT = 20


def _character_state(db, user_id, slot):
    """What a snapshot keeps of one character, or None when there is none."""
    row = db.execute(
        "SELECT class_id, name, level, xp, xp_to_next, gold, active_pet_id, equipment"
        " FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)).fetchone()
    if row is None:
        return None
    carry = [[int(r["position"]), r["item_id"], int(r["quantity"])] for r in db.execute(
        "SELECT position, item_id, quantity FROM carry_items"
        " WHERE user_id = ? AND slot = ? ORDER BY position", (user_id, slot))]
    skills = [[r["skill_id"], int(r["level"]), int(r["xp"])] for r in db.execute(
        "SELECT skill_id, level, xp FROM skills"
        " WHERE user_id = ? AND slot = ? ORDER BY skill_id", (user_id, slot))]
    return {
        "class_id": row["class_id"],
        "name": row["name"],
        "level": int(row["level"] or 1),
        "xp": int(row["xp"] or 0),
        "xp_to_next": int(row["xp_to_next"] or 0),
        "gold": int(row["gold"] or 0),
        "active_pet_id": row["active_pet_id"] or "",
        "equipment": _stored_json(row["equipment"], {}),
        "carry": carry,
        "skills": skills,
    }


def _state_fingerprint(state):
    return hashlib.sha1(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def take_snapshot(db, user_id, slot, reason):
    """
    Keep this character as it is now.

    Returns the id of the snapshot that holds the character as it is - a new
    one, or the newest one when nothing has changed since it was taken - or
    None when there is no character, or (`save` only) the newest is younger
    than SNAPSHOT_EVERY_SECONDS.

    NO COMMIT. It joins the caller's transaction, so a rollback and the
    snapshot taken before it land together or not at all.
    """
    now = int(time.time())
    newest = db.execute(
        "SELECT id, taken_at, fingerprint FROM save_snapshots"
        " WHERE user_id = ? AND slot = ? ORDER BY taken_at DESC, id DESC LIMIT 1",
        (user_id, slot)).fetchone()
    # THE CHEAP QUESTION FIRST. Every save asks, so a save inside the window
    # costs one indexed read and nothing else.
    if reason == "save" and newest is not None \
            and now - int(newest["taken_at"]) < SNAPSHOT_EVERY_SECONDS:
        return None
    state = _character_state(db, user_id, slot)
    if state is None:
        return None
    fingerprint = _state_fingerprint(state)
    if newest is not None and newest["fingerprint"] == fingerprint:
        return int(newest["id"])
    cursor = db.execute(
        "INSERT INTO save_snapshots"
        " (user_id, slot, taken_at, reason, level, gold, items, fingerprint, snapshot)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, slot, now, reason, state["level"], state["gold"], len(state["carry"]),
         fingerprint, json.dumps(state, separators=(",", ":"))),
    )
    db.execute(
        "DELETE FROM save_snapshots WHERE user_id = ? AND slot = ? AND id NOT IN ("
        " SELECT id FROM save_snapshots WHERE user_id = ? AND slot = ?"
        " ORDER BY taken_at DESC, id DESC LIMIT ?)",
        (user_id, slot, user_id, slot, SNAPSHOTS_KEPT),
    )
    return int(cursor.lastrowid)


def _days_words(minutes):
    """"a day", "30 days", "90 minutes" - a limit as a person says it."""
    minutes = int(minutes)
    if minutes % (24 * 60) == 0:
        days = minutes // (24 * 60)
        return "a day" if days == 1 else "%d days" % days
    if minutes % 60 == 0:
        hours = minutes // 60
        return "an hour" if hours == 1 else "%d hours" % hours
    return "%d minutes" % minutes


def _staff_login_note():
    """What signing in is like for staff on THIS server, read from the switch
    and the mail settings - staff_login_protected() is the same three facts
    plus the account's own confirmed address."""
    if not STAFF_LOGIN_CODES:
        return ("Staff logins take no emailed code on this server:"
                " ELUSION_STAFF_LOGIN_CODES is off.")
    if not mail_can_send():
        return ("Staff logins would take a code from their email, but this"
                " server cannot send mail, so for now they do not.")
    return ("Signs in with a code from their email, once per computer every"
            " %d days - needs a confirmed recovery address." % TRUSTED_DEVICE_DAYS)


@app.get("/api/staff/powers")
@require_auth
@require_role("mod")
def staff_powers():
    """
    What each rank may actually do, read from the live routes
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
        description: The rank ladder, and every gated route grouped under the rank that unlocks it
      404:
        description: Not staff
      401:
        description: Missing, invalid or expired token
    """
    # DERIVED, NEVER WRITTEN DOWN. Every entry below comes from the decorators
    # on the routes themselves - see the tags set in require_role/require_owner.
    # A hand-maintained list of "what a mod can do" is wrong the first time
    # somebody adds a route and forgets to update it, and wrong in the most
    # expensive direction: the owner believes a rank is narrower than it is.
    #
    # THIS IS ALSO WHY IT IS SAFE TO SHOW. It lists paths and the rank each one
    # needs; it grants nothing, and every one of those routes still checks the
    # caller itself on the way in.
    buckets = {name: [] for name in ROLES}
    for rule in app.url_map.iter_rules():
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            continue

        minimum = getattr(view, "_elusion_min_role", None)
        if minimum is None:
            # Not rank-gated. Logged-in-only routes are the ordinary business
            # of playing the game, so they are not powers and are left out.
            continue
        if minimum not in buckets:
            continue

        methods = sorted(m for m in rule.methods if m in ("GET", "POST", "PUT", "DELETE", "PATCH"))
        buckets[minimum].append({
            "method": methods[0] if methods else "GET",
            "path": str(rule.rule),
            "what": (view.__doc__ or "").strip().splitlines()[0].strip() if view.__doc__ else "",
        })

    for name in buckets:
        buckets[name].sort(key=lambda row: row["path"])

    # THE THINGS THAT ARE NOT ROUTES. A rank is more than the endpoints it
    # opens, and these are the ones that would surprise an owner handing the
    # rank out. Every number is pulled from the constant that enforces it
    # rather than retyped, so the figures cannot drift; the words can, and
    # did - this list said "the in-game item menu" for a day after the menu
    # moved into the GM panel. test_moderation.py M-10 holds what it can.
    extras = {
        "player": [],
        "mod": [
            "Ban for at most %s - a longer or permanent ban is refused."
            % _days_words(MAX_MOD_BAN_DAYS * 24 * 60),
            "Mute for at most %s - longer is refused." % _days_words(MUTE_MAX_MINUTES_MOD),
            "Cannot be ignored in chat - a player's ignore refuses staff, so"
            " what a mod says always arrives.",
            _staff_login_note(),
            # WHAT A MOD NO LONGER HAS, said where the owner handing out the
            # rank will read it. Until 6 Oct 2026 a mod could give themselves
            # any item and write a whole bag or bank; both are the owner's now.
            "Cannot create items: giving an item to anyone, yourself included,"
            " and writing a whole bag or bank at once, are the owner's alone.",
        ],
        "dev": [
            "Ban permanently, and for any number of days.",
            "Mute for up to %s." % _days_words(MUTE_MAX_MINUTES),
        ],
        # THE CO-OWNER HOLDS THE GM PANEL'S NOTES (0.21.0), because the
        # co-owner is the lowest rank require_owner lets in, and that is where
        # its routes are listed; the owner, above, has every one of them.
        "coowner": [
            "Cannot be granted or revoked here - an account the server's"
            " ELUSION_CO_OWNERS names, and only while the owner's co-owner switch"
            " (the GM panel's Server tab) is on. Off, it is the rank its row says.",
            "Every power the owner has, with the GM panel - except over the owner:"
            " a co-owner cannot kick, ban, mute, rank, give to, level, read the"
            " save history of or roll back the owner's account. The owner can do"
            " all of that to a co-owner.",
            "Exempt from the maintenance switch: never locked out or disconnected by it.",
            "The GM panel opens for the owner and a co-owner: its Account, Testing and Server tabs.",
            "Testing tab: gold, any item by id or from the item catalogue, and"
            " the level and skill levels of their own character.",
            # CLIENT-SIDE, AND SAID SO. Everything else on this list is something
            # the SERVER enforces; god mode is a switch in the game client, and a
            # rank the client checks is only ever worth what an honest build is
            # worth. It was a dev's too, through a key, until game 0.7.1 removed
            # the debug keys; the GM panel's switch is the only way in now.
            #
            # It grants nothing: the client returns before the damage AND before
            # the defense XP it would otherwise report to /api/skill/train, so an
            # invincible character earns exactly what a stationary one does. And
            # hp is client-written anyway (E-9), so a modified client could
            # always refuse to die - this changes what an HONEST build can do.
            "Also on the Testing tab (client-side): god mode, which takes no"
            " damage and earns no defense XP while it is on, and the game's"
            " performance readout.",
            "The only accounts that can create items, with the owner: the Testing"
            " tab, and writing a whole bag or bank at once (everyone else moves one"
            " cell at a time). The game has no debug keys. Every gift goes in the"
            " gifts ledger under the name of whoever gave it.",
            "Account tab: Give item puts anything from the item catalogue into"
            " the bag of the character a player is playing, Set level sets that"
            " character's level (they are signed out so their game loads it), and"
            " Save history puts a player's character back to one of its last %d"
            " snapshots (one every %d minutes at most, while it changes). A"
            " rollback signs them out so their game reloads it, and can return an"
            " item they have since traded away." % (SNAPSHOTS_KEPT, SNAPSHOT_EVERY_SECONDS // 60),
        ],
        "owner": [
            "Cannot be granted or revoked - it comes from ELUSION_OWNER in the server's environment.",
            "Everything a co-owner has, and above every co-owner: nobody can do"
            " any of it to the owner.",
            "The only one who lets the co-owners in or takes them out - the"
            " co-owner switch on the GM panel's Server tab.",
        ],
    }

    ladder = []
    for index, name in enumerate(ROLES):
        ladder.append({
            "rank": name,
            "level": index,
            "grantable": name in SETTABLE_ROLES,
            "routes": buckets.get(name, []),
            "notes": extras.get(name, []),
        })

    return {
        "ladder": ladder,
        "you_are": role_for(g.user),
        # Mirrors the rule in PUT /api/staff/role: you may not grant a rank at
        # or above your own.
        "you_may_grant": [r for r in SETTABLE_ROLES if ROLES.index(r) < ROLES.index(role_for(g.user))],
    }, 200


# =============================================================================
# THE STAFF LIST AND THE MODERATION RECORD, FOR A SERVER THAT IS NOT SMALL
# =============================================================================
# The staffdesk/ package since 18 Oct: the account list, the moderation log, a
# note and one account's page - and why the list is a page and a cursor, not
# the whole server (staffdesk/__init__.py). What stays here: the query's
# numbers every staff page reads (_query_int(), trade/'s staff view too), and
# role_for() as SQL (_rank_level_sql()), the ladder's, which reads the owner
# and the co-owners as they are on each call.
import staffdesk as staffdesk_package  # noqa: E402


def _query_int(name, default, low, high):
    """An integer query parameter, or None when it is not one in [low, high]."""
    raw = request.args.get(name, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if low <= value <= high else None


def _rank_level_sql(alias):
    """
    role_for() as SQL: the ladder position of users row `alias`, as (sql, params).

    Built from ROLES and OWNER_USERNAME rather than typed out, so a rank added
    to the ladder is a rank this understands. The owner is decided by name
    before the column, exactly like role_for(), so a column reading 'owner'
    counts as a player here too. NULL for a row that is not there - a LEFT JOIN
    onto an account since deleted - which callers read as a player.
    """
    whens = []
    params = []
    if OWNER_USERNAME:
        # users.username is COLLATE NOCASE, so this is is_owner()'s
        # case-insensitive match, not a second rule beside it.
        whens.append("WHEN %s.username = ? THEN %d" % (alias, ROLES.index("owner")))
        params.append(OWNER_USERNAME)
    # The co-owners, while the switch is on - role_for()'s second line.
    if co_owners_on():
        for name in CO_OWNER_USERNAMES:
            whens.append("WHEN %s.username = ? THEN %d" % (alias, ROLES.index("coowner")))
            params.append(name)
    for name in SETTABLE_ROLES:
        if name != DEFAULT_ROLE:
            whens.append("WHEN %s.role = '%s' THEN %d" % (alias, name, ROLES.index(name)))
    sql = "(CASE WHEN %s.id IS NULL THEN NULL %s ELSE %d END)" % (
        alias, " ".join(whens), ROLES.index(DEFAULT_ROLE))
    return sql, params


_STAFFDESK = staffdesk_package.register(
    app,
    require_auth=require_auth,
    require_role=require_role,
    get_db=get_db,
    bad_request=bad_request,
    query_int=_query_int,
    moderation_target=_moderation_target,
    can_act_on=can_act_on,
    role_for=role_for,
    rank_sql=_rank_level_sql,
    roles=ROLES,
    default_role=DEFAULT_ROLE,
    ban_state=ban_state,
    mute_state=chat_mute_state,
    log_staff_action=log_staff_action,
    online_window=ONLINE_WINDOW_SECONDS,
    # Defined with trade, further down, so asked for when an account is read.
    playing_slot=lambda db, user_id: _playing_slot(db, user_id),
    linked_accounts=_linked_accounts,
    crowded_at=SHARED_ADDRESS_ACCOUNTS,
    row_int=_row_int,
)
# THE TWO QUERY BUILDERS, by their old names: test_moderation.py asks SQLite
# for the plan of exactly what the routes run. And the staff page's sizes and
# the log's vocabulary, which trade/'s staff view and the suites read here.
_staff_users_query = _STAFFDESK.users_query
_staff_log_query = _STAFFDESK.log_query
STAFF_PAGE_DEFAULT = staffdesk_package.STAFF_PAGE_DEFAULT
STAFF_PAGE_MAX = staffdesk_package.STAFF_PAGE_MAX
STAFF_ACTION_KINDS = staffdesk_package.STAFF_ACTION_KINDS
STAFF_ACTION_GROUPS = staffdesk_package.STAFF_ACTION_GROUPS
STAFF_PRIVATE_KINDS = staffdesk_package.STAFF_PRIVATE_KINDS
STAFF_NOTE_MAX = staffdesk_package.STAFF_NOTE_MAX
STAFF_ONLINE_COUNT_SQL = staffdesk_package.STAFF_ONLINE_COUNT_SQL


# =============================================================================
# THE SERVER'S SWITCHES - the switches/ package (since 18 Oct)
# =============================================================================
# GET /api/status, the owner's five switches (maintenance, the minimum build,
# PvP, trading, the co-owners - read and set) and the owner's announcement, as
# they were here: endpoint names, docstrings, ranks, the password asked again
# and every message (routes.lock). switches/__init__.py says what each does and
# which are announced. What every switch READS as stays above, with its key -
# the gates that ask run on every request - and is handed in; so are the
# broadcast line, the moderation log and the sign-out for a close with no
# grace. The broadcast poll stays below: it is the heartbeat, and reads every
# area.
import switches as switches_package  # noqa: E402

switches_package.register(
    app,
    require_auth=require_auth,
    require_owner=require_owner,
    require_the_owner=require_the_owner,
    require_fresh_password=require_fresh_password,
    get_db=get_db,
    bad_request=bad_request,
    catalogue=gamedata,
    get_setting=get_server_setting,
    set_setting=set_server_setting,
    maintenance_key=MAINTENANCE_KEY,
    min_build_key=MIN_BUILD_KEY,
    pvp_key=PVP_KEY,
    trade_key=TRADE_KEY,
    co_owners_key=CO_OWNERS_KEY,
    maintenance_state=maintenance_state,
    seconds_left=maintenance_seconds_left,
    default_message=MAINTENANCE_DEFAULT_MESSAGE,
    pvp_enabled=pvp_enabled,
    trades_enabled=trades_enabled,
    min_client_build=min_client_build,
    current_build=CURRENT_CLIENT_BUILD,
    co_owners_on=co_owners_on,
    co_owners_changed=_co_owners_changed,
    # ASKED ON EVERY CALL: test_coowner.py empties it to see the switch refuse.
    co_owner_names=lambda: CO_OWNER_USERNAMES,
    post_broadcast=post_broadcast,
    broadcast_limit=MAX_BROADCAST_LENGTH,
    log_staff_action=log_staff_action,
    online_window=ONLINE_WINDOW_SECONDS,
    end_player_sessions=_end_all_player_sessions,
    # trade/'s count, registered further down, so asked for when it is wanted.
    open_trades=lambda db: trade_package.open_count(db, int(time.time())),
)

# Named here as well, for the suites that read them off this module;
# switches/services.py is where they are set.
MAINTENANCE_DEFAULT_GRACE_SECONDS = switches_package.MAINTENANCE_DEFAULT_GRACE_SECONDS
MAINTENANCE_MAX_GRACE_SECONDS = switches_package.MAINTENANCE_MAX_GRACE_SECONDS


# =============================================================================
# THE STIRRING LANDS (game 0.46.0)
# =============================================================================
# Every few hours one of the seven lands stirs: its monsters hit harder, have
# more health, move faster, drop more and give more XP, as the owner sets it.
# The world/ package since 18 Oct: the owner's three routes, the settings, and
# the stir itself - which land, when the next begins (the whole story is in
# world/__init__.py). What other areas ask of it - the kill route what a kill
# pays, the poll what the game is told, the events their guardrails, the
# insights the next stir - register() hands back, and this file keeps under
# the names it always used.
import world as world_package  # noqa: E402

# THE WORLD'S EVENTS (game 0.48.0): the EventsAPI the events/ package hands
# back, registered near the end of this file. None until then.
world_events = None
# THE LAND INSIGHTS (game 0.50.0, insights/): who is learning where, for the
# world's own pick. None until registered, at the end of the file.
land_insights = None

_WORLD = world_package.register(
    app,
    require_auth=require_auth,
    require_owner=require_owner,
    require_fresh_password=require_fresh_password,
    get_db=get_db,
    bad_request=bad_request,
    get_setting=get_server_setting,
    set_setting=set_server_setting,
    announce=lambda body, by="": post_broadcast(body, "world", by),
    log_staff_action=log_staff_action,
    element_of=lambda enemy_id: int((gamedata.ENEMIES.get(enemy_id) or {}).get("element", 0) or 0),
    # ASKED ON EVERY USE: both are registered at the end of this file, and are
    # handed what this hands back.
    events=lambda: world_events,
    insights=lambda: land_insights,
)
WORLD_KEY = world_package.WORLD_KEY
LANDS = world_package.LANDS
WORLD_DEFAULTS = world_package.WORLD_DEFAULTS
world_settings = _WORLD.settings
land_hunts = _WORLD.land_hunts
pick_land = _WORLD.pick_land
_begin_stir = _WORLD.begin_stir
current_world_tick = _WORLD.current_tick
world_next_stir = _WORLD.next_stir
world_public = _WORLD.public
world_stir_for = _WORLD.stir_for
# The two owner's changes, as the routes are registered: test_world.py reads
# their mark for asking the password again.
set_world = app.view_functions["set_world"]
stir_world = app.view_functions["stir_world"]


@app.get("/api/server/broadcasts")
@require_auth
def read_broadcasts():
    """
    Everything the server has said since a given message id
    ---
    tags:
      - Status
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: query
        name: since
        type: integer
        description: "Return messages with an id greater than this. 0 for the recent tail."
    responses:
      200:
        description: Messages in id order, oldest first, plus the newest id
      401:
        description: Missing, invalid or expired token
    """
    # THIS DOUBLES AS THE HEARTBEAT and that is not an accident. It is the call
    # a client in the world makes on a timer, so it is already the thing that
    # finds out about a kick, a ban, or a closed server - @require_auth answers
    # 401 for all three, which is exactly the signal the client already knows
    # how to read. One poll, two jobs, no second timer to keep in step.
    #
    # AND IT DOES THE OTHER HALF OF THE HEARTBEAT NOW, which it had never done.
    # The sentence above was true about REVOCATION and quietly false about
    # PRESENCE: the stamp lived only in GET /api/auth/session, and nothing in
    # the client called that on a timer. So this route was the heartbeat in the
    # sense that mattered for kicks and not in the sense that mattered for "who
    # is online", and every presence surface in the game read offline forty-five
    # seconds after login. See stamp_presence().
    #
    # HERE RATHER THAN A SECOND TIMER IN THE CLIENT. This request already
    # happens every BROADCAST_POLL_SECONDS and is already authenticated, so the
    # stamp costs one UPDATE on a row already being looked up - against a whole
    # extra round trip per player per fifteen seconds, which at a thousand
    # players is 67 requests a second bought to learn nothing new.
    raw_since = request.args.get("since", "0")
    try:
        since = max(0, int(raw_since))
    except (TypeError, ValueError):
        return bad_request("since must be a whole number")

    # WHICH CHARACTER IS BEING PLAYED, if the client says - see stamp_presence().
    # IGNORED WHEN MALFORMED, not refused: this request is also the heartbeat,
    # and a 400 here reads to the client as "the server cannot be reached",
    # which is a far worse answer to a bad slot than simply not recording it.
    playing = parse_slot(request.args.get("slot")) if request.args.get("slot") is not None else None

    db = get_db()
    # AFTER THE VALIDATION, so a malformed request is answered rather than
    # recorded as a beat - and committed with the read below rather than on its
    # own, so this is one transaction, not two.
    stamp_presence(db, playing)
    # THE STIRRING LANDS, asked before the messages are read: a stir this poll
    # begins is announced in this poll's own answer.
    world = world_public(db)
    # THE WORLD'S EVENTS (game 0.48.0, events/): what is on, what is coming.
    events_now = world_events.public(db) if world_events is not None else {"events": [], "next": []}
    # WHO IS ONLINE, counted for the event report (game 0.49.0) - at most once
    # a minute per process, after this player's own beat above.
    if world_events is not None:
        world_events.sample(db)
    db.commit()

    # A FIRST POLL ASKS FOR THE TAIL, NOT THE WHOLE TABLE. since=0 means "I just
    # got here" - answering that with a week of restart notices would scroll the
    # useful ones off screen before the player read them.
    if since == 0:
        rows = db.execute(
            "SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?",
            (BROADCAST_PAGE_LIMIT,),
        ).fetchall()
        rows = list(reversed(rows))
    else:
        rows = db.execute(
            "SELECT * FROM broadcasts WHERE id > ? ORDER BY id ASC LIMIT ?",
            (since, BROADCAST_PAGE_LIMIT),
        ).fetchall()

    messages = [{
        "id": int(r["id"]),
        "kind": r["kind"],
        "body": r["body"],
        "by": r["created_by"],
        "at": int(r["created_at"]),
    } for r in rows]

    # The newest id IN THE TABLE, not in this page: a client that was handed a
    # truncated page still needs to know there is more, and comparing its cursor
    # to this is how it finds out.
    newest = db.execute("SELECT MAX(id) AS n FROM broadcasts").fetchone()["n"]

    return {
        "messages": messages,
        "latest_id": int(newest or 0),
        "maintenance": maintenance_public(),
        # RIDES THE SAME POLL AS THE MAINTENANCE NOTICE, and for the same
        # reason: it is a fact about the world that the HUD has to keep on
        # screen, and a second request every few seconds to learn one boolean
        # would be a second request every few seconds.
        "pvp": pvp_enabled(),
        # WHEN IT WAS SWITCHED, because "PvP is ON" answers a different
        # question from the one a player who just logged in is asking. See
        # pvp_since(); 0 when it has never been touched.
        "pvp_at": pvp_since(),
        # THE STIRRING LANDS (game 0.46.0): which land stirs, until when, and
        # how hard its monsters hit and how much resistance to it counts - the
        # two the game applies itself. See world_public().
        "world": world,
        "events": events_now,
        # Rides the same poll as everything else. None when there is nowhere
        # to be; the client applies it and then acks it away.
        "teleport": pending_teleport_for(g.user["id"]),
        # YOUR OWN GUILD, ON THE POLL THAT ALREADY KNOWS WHO YOU ARE.
        #
        # The client draws the tag over your head, and a nameplate is a thing
        # that must be TRUE RIGHT NOW rather than true when you last opened a
        # panel. GET /api/guild is the only other route that says this, and
        # nothing calls it on a timer - so without this field the tag would
        # appear when the guild panel happened to be opened and then go on being
        # whatever it was, which is the shape of bug this project keeps finding:
        # a display fed by something nobody schedules.
        #
        # IT ALSO MAKES REMOVAL VISIBLE. Being kicked, or a mod renaming or
        # disbanding the guild, reaches the player within one poll instead of
        # never. That is the same argument that put revocation on this route.
        #
        # ONE INDEXED LOOKUP on a request that has already fetched the session
        # and is already writing a presence stamp. It is the cheapest place in
        # the whole system to answer this, which is why it is answered here
        # rather than by a second timer in the client.
        **_own_guild(db, int(g.user["id"])),
        # THE PARTY YOU ARE IN (game 0.42.0): its id, leader and members, so
        # the game colours their names and draws them on the map, and hears a
        # change within one poll. See _own_party().
        "party": _own_party(db, int(g.user["id"])),
        # THE TRADE YOU ARE IN, if any, in one line - so somebody who has not
        # opened the trade window finds out a trade was opened WITH them. The
        # window polls /api/trade itself, but only while it is open, and before
        # this nothing told the other person to open it.
        "trade": _trade_summary(db, int(g.user["id"])),
        # AND THE RESULT OF ONE THAT FINISHED WITHOUT YOU WATCHING. See
        # _take_resync(): whoever accepted first learns here that it went
        # through, and gets the bag and purse it went through with.
        "trade_resync": _take_resync(db, int(g.user["id"])),
        # WHAT WAS SAID TO YOU WHILE YOU WERE NOT LOOKING. See _chat_news().
        "chat_news": _chat_news(db, int(g.user["id"])),
        # WHAT IS WAITING ON YOUR ANSWER. See _waiting_asks().
        "asks": _waiting_asks(db, int(g.user["id"])),
        # LINES WAITING FOR STAFF, for staff only - a report nobody sees is a
        # report nobody reads. 0 for everyone else, not absent.
        "open_reports": _open_report_count(db) if role_at_least(g.user, "mod") else 0,
        # AND HOW MANY PLAYERS THOSE LINES ARE ABOUT, which is the number on
        # the Staff button: one card each on the Reports tab.
        "open_report_players": _open_report_players(db) if role_at_least(g.user, "mod") else 0,
    }, 200


def _chat_news(db, user_id):
    """The newest whisper TO this player, and the newest line in their guild
    and among their friends - each said by somebody else.

    THE CHAT WINDOW ONLY READS THE TAB THAT IS OPEN, AND ONLY WHILE IT IS
    OPEN. So a whisper reached nobody: with chat closed, or open on World, it
    sat on the server, and the Whisper tab only shows a conversation with a
    name you already typed. A player could be whispered all day and never
    know. This rides the poll that runs anyway, like "trade" above, and the
    HUD says who whispered and lights the tabs.

    Three index reads (idx_chat_to, idx_chat_private), on chat that is pruned
    after a day."""
    # NOBODY YOU IGNORE, in all three - the same clause the read uses.
    whisper = db.execute(
        "SELECT id, username, body, image_id, created_at FROM chat_messages"
        " WHERE channel = 'private' AND target_id = ? AND user_id != ?"
        + IGNORED_AUTHORS_CLAUSE +
        " ORDER BY id DESC LIMIT 1", (user_id, user_id, user_id)).fetchone()
    seat = guild_membership(user_id)
    guild_line = 0
    if seat is not None:
        guild_line = db.execute(
            "SELECT MAX(id) AS n FROM chat_messages"
            " WHERE channel = 'guild' AND target_id = ? AND user_id != ?"
            + IGNORED_AUTHORS_CLAUSE,
            (int(seat["guild_id"]), user_id, user_id)).fetchone()["n"] or 0
    party_seat = party_membership(db, user_id)
    party_line = 0
    if party_seat is not None:
        party_line = db.execute(
            "SELECT MAX(id) AS n FROM chat_messages"
            " WHERE channel = 'party' AND target_id = ? AND user_id != ?"
            + IGNORED_AUTHORS_CLAUSE,
            (int(party_seat["party_id"]), user_id, user_id)).fetchone()["n"] or 0
    friend_line = 0
    friends = sorted(_friend_ids(db, user_id))
    if friends:
        friend_line = db.execute(
            "SELECT MAX(id) AS n FROM chat_messages WHERE channel = 'friends'"
            " AND user_id IN (%s)" % ",".join("?" for _ in friends)
            + IGNORED_AUTHORS_CLAUSE,
            tuple(friends) + (user_id,)).fetchone()["n"] or 0
    return {
        "whisper": None if whisper is None else {
            "id": int(whisper["id"]),
            "from": whisper["username"],
            "body": whisper["body"] or ("(a picture)" if whisper["image_id"] else ""),
            "at": int(whisper["created_at"]),
        },
        "guild": int(guild_line),
        "friends": int(friend_line),
        "party": int(party_line),
    }


def _waiting_asks(db, user_id):
    """Friend requests to this player and guild invitations waiting on their
    answer: how many of each, and the newest one's name and time.

        {"friends": {"count": 1, "newest": "caster", "at": 1790800000},
         "guild":   {"count": 0, "newest": "", "at": 0}}

    WHY IT RIDES THE POLL. Both are answered from a panel, and on day 1 nothing
    told the player to open it: a request to somebody standing next to you sat
    there until they happened to look. The same gap trades had (see "trade"
    above), closed the same way, on a request that already runs every ten
    seconds. The client lights the button while the count is above zero and
    says the newest once.

    Two indexed reads each (idx_friends_addressee, idx_guild_invites_user). An
    invitation to a guild the player is already in cannot exist: joining or
    founding deletes every invitation they hold.
    """
    friends = db.execute(
        "SELECT COUNT(*) AS n FROM friends WHERE addressee_id = ? AND state = 'pending'",
        (user_id,)).fetchone()
    newest_friend = db.execute(
        "SELECT u.username, f.created_at FROM friends f JOIN users u ON u.id = f.requester_id"
        " WHERE f.addressee_id = ? AND f.state = 'pending'"
        " ORDER BY f.created_at DESC, f.rowid DESC LIMIT 1", (user_id,)).fetchone()
    invites = db.execute(
        "SELECT COUNT(*) AS n FROM guild_invites WHERE user_id = ?", (user_id,)).fetchone()
    party_asks = _party_invites_to(db, user_id)
    newest_invite = db.execute(
        "SELECT gl.name, i.created_at FROM guild_invites i JOIN guilds gl ON gl.id = i.guild_id"
        " WHERE i.user_id = ? ORDER BY i.created_at DESC, i.rowid DESC LIMIT 1",
        (user_id,)).fetchone()
    return {
        "friends": {"count": int(friends["n"] or 0),
                    "newest": newest_friend["username"] if newest_friend else "",
                    "at": int(newest_friend["created_at"] or 0) if newest_friend else 0},
        "guild": {"count": int(invites["n"] or 0),
                  "newest": newest_invite["name"] if newest_invite else "",
                  "at": int(newest_invite["created_at"] or 0) if newest_invite else 0},
        # A PARTY INVITATION (game 0.42.0): who asked, the newest first.
        "party": {"count": len(party_asks),
                  "newest": party_asks[0]["username"] if party_asks else "",
                  "at": int(party_asks[0]["created_at"] or 0) if party_asks else 0},
    }


def _own_guild(db, user_id):
    """{"guild": name, "guild_tag": TAG} for a caller, both "" when they have none.

    A DICT RATHER THAN TWO CALLS so the two fields cannot disagree about
    whether somebody is in a guild - one query, one answer, spread into the
    response.
    """
    row = db.execute(
        "SELECT gl.name FROM guild_members m JOIN guilds gl ON gl.id = m.guild_id"
        " WHERE m.user_id = ?", (user_id,)).fetchone()
    name = row["name"] if row else ""
    return {"guild": name, "guild_tag": guild_tag(name) if name else ""}


# =============================================================================
# CHAT ROUTES - the chat/ package (see WORLD CHAT)
# =============================================================================
# /api/chat/send, /api/chat, the three picture routes (four paths), the
# staff's /api/chat/delete and the three /api/ignores routes, as they were
# here - endpoint names, docstrings, ranks, the write lock and every message
# (routes.lock). Handed from here what they share with other areas, and handing
# back what other areas ask of chat (_CHAT): post_chat() and a report's "could
# they read it".
_CHAT = chat.register(
    app,
    require_auth=require_auth,
    require_role=require_role,
    no_write_lock=no_write_lock,
    get_db=get_db,
    bad_request=bad_request,
    find_user=_user_by_name,
    clean_text=clean_player_text,
    role_for=role_for,
    name_hue_of=name_hue_of,
    guild_tag=guild_tag,
    role_at_least=role_at_least,
    can_act_on=can_act_on,
    has_owner_powers=has_owner_powers,
    log_staff_action=log_staff_action,
    mute_state=chat_mute_state,
    describe_mute=describe_mute,
    friend_ids=_friend_ids,
    guild_membership=guild_membership,
    party_membership=party_membership,
    # ASKED ON EVERY USE: both are this file's tunables, and the suites turn
    # them down (test_chatrooms.py, test_chatsafety.py).
    keep_per_channel=lambda: CHAT_KEEP_PER_CHANNEL,
    ignore_limit=lambda: IGNORE_LIMIT,
    # LOOKED UP ON EACH CALL, so a suite's stand-in for the fetch is heard.
    fetch_image=lambda url: _fetch_image_bytes(url),
)


# =============================================================================
# IGNORE, REPORT, MUTE - the routes
# =============================================================================
# Reporting a line or a player, the Reports tab and muting are moderation/'s
# since 18 Oct (registered at MODERATION, above); the ignore list's three
# routes are chat/'s.


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
# WHY THE BAG IS CHANGED ONE CELL AT A TIME, AND WAS NOT ALWAYS
# --------------------------------------------------------------
# This said an inventory was not a discrete act the server could reason about -
# the client held the whole picture, twenty cells were small enough to send in
# full, and a whole-array replace had no partial-failure state. All true, and it
# left the arrangement and every loss on the client's word, with the server
# trimming only gains. Every act on a bag turned out to be discrete after all:
# a drag is a move, the bin is a discard, a pile is cashed. So each is now its
# own request (ONE CELL AT A TIME), and the whole-array writes are staff-only.

# The client's backpack is twenty cells. Kept here rather than inferred from
# whatever arrives so an oversized array is a 400 rather than a slow leak into
# the database.
INVENTORY_CAPACITY = 20

# THE HOTBAR IS TEN MORE CARRIED CELLS, drawn along the bottom of the screen
# instead of in the bag: keys 1-9 then 0 (Hotbar.SLOT_COUNT in the client's
# src/ui/hotbar.gd). They are carry_items rows at positions INVENTORY_CAPACITY
# to CARRY_CAPACITY - 1, so the backpack's rules are the hotbar's rules for
# free: the ledger reconciles them, respawning deletes them, and moving a stack
# between the bag and a key is a reorder of one array.
#
# It used to hold item ids pointing INTO the bag, so the same potion was drawn
# twice - once in its bag cell, once on its key. Dragging to a key now moves the
# item, the way equipping moves one onto the character.
HOTBAR_SIZE = 10

# Every cell a character carries: the bag, then the hotbar.
#
# BAG-ONLY PLACEMENT. _add_to_backpack() - loot, the shop, bank withdrawals,
# trades, catches, cooking, a staff grant - only ever tops up or opens cells
# below INVENTORY_CAPACITY. A key holds what the player put there and nothing
# lands on one by itself. "Your backpack is full" is therefore about the twenty,
# and says twenty.
CARRY_CAPACITY = INVENTORY_CAPACITY + HOTBAR_SIZE

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


# _reconcile_inventory() and _reconcile_bank() WERE HERE - the E-1 ledger that
# trimmed a whole bag from the client to what the server had granted. Nothing
# calls them now: a player's bag and bank are never written whole (see ONE
# CELL AT A TIME), and staff were always exempt. Removed rather than left
# reachable, so the next route cannot quietly lean on a check that only ever
# caught gains.


def _slot_exists(user_id, slot):
    row = get_db().execute(
        "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    return row is not None


# =============================================================================
# THE CARRY AND THE SKILLS - the carry/ and skills/ packages
# =============================================================================
# The backpack's four routes and the skills' two are the packages' since 18
# Oct, and so is everything they share with the rest of this file: the item
# grants and the XP grant, the carry as the game holds it, and the grid steps
# the bank and the shop's sale share. Registered HERE - before the bank, the shop, trades, the
# kill, loot, fishing and cooking, every area they are handed to - and kept
# callable under the names they had in this file. What they are handed from
# further down (the trade-resync flag, the lusion pile's id, the account view)
# is asked for when a route runs.
import carry as carry_package  # noqa: E402
import skills as skills_package  # noqa: E402

_CARRY = carry_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    slot_exists=_slot_exists,
    parse_grid_cell=parse_grid_cell,
    parse_item_id=parse_item_id,
    has_owner_powers=has_owner_powers,
    catalogue=gamedata,
    bag_cells=INVENTORY_CAPACITY,
    carry_cells=CARRY_CAPACITY,
    quantity_ceiling=QUANTITY_CEILING,
    take_resync=lambda db, user_id, slot: _take_resync(db, user_id, slot),
    money_item_worth=money_item_worth,
    lusions_item_id=lambda: LUSIONS_ITEM_ID,
    # LOOKED UP ON EACH CASH: test_concurrency.py holds _ensure_account open,
    # and a function handed in once would be the one it never slowed.
    ensure_account=lambda user_id: _ensure_account(user_id),
    gold_delta=gold_delta,
    lusion_delta=lusion_delta,
    account_of=lambda user_id: account_payload(user_id),
)
_stack_limit = _CARRY.stack_limit
_parse_positional_items = _CARRY.parse_positional_items
bag_fingerprint = _CARRY.fingerprint
inventory_payload = _CARRY.inventory_of
_owns_item = _CARRY.owns_item
_take_from_backpack = _CARRY.take_from_backpack
_add_to_backpack = _CARRY.add_to_backpack
_take_from_cells = _CARRY.take_from_cells
_apply_cell_changes = _CARRY.apply_cell_changes
_grid_cells = _CARRY.grid_cells
_grid_rewrite = _CARRY.grid_rewrite
_grid_move = _CARRY.grid_move
_grid_discard = _CARRY.grid_discard
_carry_request = _CARRY.carry_request
_carry_resync = _CARRY.resync

_SKILLS = skills_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    parse_stat=parse_stat,
    slot_exists=_slot_exists,
    has_owner_powers=has_owner_powers,
    catalogue=gamedata,
    valid_skills=VALID_SKILLS,
    server_owned=SERVER_OWNED_SKILLS,
    trainable=TRAINABLE_SKILLS,
    max_level=MAX_SKILL_LEVEL,
    max_xp=MAX_SKILL_XP,
    max_train_seconds=MAX_TRAIN_ELAPSED_SECONDS,
    max_train_xp_per_sec=MAX_TRAIN_XP_PER_SEC,
    proficient_amount=proficient_amount,
)
skills_payload = _SKILLS.skills_of
_grant_skill_xp = _SKILLS.grant_xp


# =============================================================================
# THE BANK AND THE ACCOUNT - the bank/ package
# =============================================================================
# Registered here, after the carry's sizes and helpers it is handed, rather
# than at THE BANK AND THE ACCOUNT above. The seven routes - the account, the
# bank's whole-array write and the lusion read-back, a transfer of items and
# of gold, a move and a discard - are bank/'s since 18 Oct; the grid steps
# they share with the backpack are carry/'s, handed back just above.
import bank as bank_package  # noqa: E402

_BANK = bank_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    parse_stat=parse_stat,
    parse_cell=parse_cell,
    parse_grid_cell=parse_grid_cell,
    parse_item_id=parse_item_id,
    parse_positional_items=_parse_positional_items,
    slot_exists=_slot_exists,
    # ASKED ON EVERY CALL: test_concurrency slows _ensure_account() to hold two
    # deposits of one purse open at once.
    ensure_account=lambda user_id: _ensure_account(user_id),
    has_owner_powers=has_owner_powers,
    catalogue=gamedata,
    capacity=BANK_CAPACITY,
    carry_capacity=CARRY_CAPACITY,
    backpack_capacity=INVENTORY_CAPACITY,
    quantity_ceiling=QUANTITY_CEILING,
    take_from_cells=_take_from_cells,
    apply_cell_changes=_apply_cell_changes,
    grid_move=_grid_move,
    grid_discard=_grid_discard,
    # Defined with the carry, further down, so asked for when a withdrawal lands.
    add_to_backpack=lambda user_id, slot, item_id, quantity:
        _add_to_backpack(user_id, slot, item_id, quantity),
    inventory_of=inventory_payload,
)
# The account as GET /api/account answers it, by the name this file always
# used: cashing a pile of lusions answers with it.
account_payload = _BANK.account_of


# =============================================================================
# THE SHOP - the shop/ package (since 18 Oct)
# =============================================================================
# POST /api/shop/buy and /sell and GET /api/shop/<shop_id>, as they were here -
# endpoint names, docstrings and every message (routes.lock). shop/__init__.py
# tells why a shop has to be the server's. Handed the carry's grant and its
# one-cell steps (carry/, registered above), the gold ledger, and the trade
# hold - defined with trade, below, so asked for when a purchase lands.
import shop as shop_package  # noqa: E402

shop_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    slot_exists=_slot_exists,
    catalogue=gamedata,
    stack_limit=_stack_limit,
    add_to_backpack=_add_to_backpack,
    bag_cells=INVENTORY_CAPACITY,
    place_trade_hold=lambda db, user_id, item_id, quantity: place_trade_hold(db, user_id, item_id, quantity),
    gold_delta=gold_delta,
    inventory_of=inventory_payload,
    carry_request=_carry_request,
    parse_grid_cell=parse_grid_cell,
    carry_cells=CARRY_CAPACITY,
    grid_cells=_grid_cells,
    grid_rewrite=_grid_rewrite,
    resync=_carry_resync,
)


# =============================================================================
# PLAYER TRADE AND THE KINGDOM TAX
# =============================================================================
# THE ONLY PLACE TWO PLAYERS' PROPERTY CHANGES HANDS, and the second gold sink
# in the game after the vendor: the kingdom takes a cut of every trade, and the
# gold is destroyed. The trade itself is the trade/ package and the board that
# shows what the kingdom took is kingdom/ (both 18 Oct; the rules are written
# out in trade/__init__.py). What stays here is what other areas share with it.
import trade as trade_package  # noqa: E402

# WHAT IS ASKED HERE BY ITS OLD NAME: the limits a rollback, the owner's trade
# switch and the suites read, and the hold a fresh find places - the loot take
# and the shop call place_trade_hold() with the find.
TRADE_EXPIRY_SECONDS = trade_package.TRADE_EXPIRY_SECONDS
TRADE_CANCELLED_KEPT_SECONDS = trade_package.TRADE_CANCELLED_KEPT_SECONDS
TRADE_HOLD_SECONDS = trade_package.TRADE_HOLD_SECONDS
TRADE_HISTORY_SHOWN = trade_package.TRADE_HISTORY_SHOWN
STAFF_TRADE_STATES = trade_package.STAFF_TRADE_STATES


def trade_held_kind(item_id):
    """True for the pieces a fresh find holds: mythic tier, or a Perfect roll.
    trade/'s held_kind(), asked of this server's catalogue."""
    return trade_package.held_kind(gamedata, item_id)


def place_trade_hold(db, user_id, item_id, quantity=1, now=None):
    """A fresh find of a held kind: one hold per piece, from now. trade/'s
    place_hold(); no commit - the find's own transaction makes it."""
    trade_package.place_hold(db, gamedata, user_id, item_id, quantity, now)


# WHICH CHARACTER AN ACCOUNT IS PLAYING, as one SQL expression. {user} is
# whatever names the account in the query it lands in - "?" on its own, or
# "saves.user_id" inside a query that is already walking saves. It takes two
# parameters, in order: the online cutoff, then now. See _playing_slot().
PLAYING_SLOT_SQL = (
    "COALESCE("
    "(SELECT ps.playing_slot FROM sessions ps"
    "   JOIN saves pv ON pv.user_id = ps.user_id AND pv.slot = ps.playing_slot"
    "  WHERE ps.user_id = {user} AND ps.playing_slot IS NOT NULL"
    "    AND ps.last_seen_at > ? AND ps.expires_at > ?"
    "  ORDER BY ps.last_seen_at DESC LIMIT 1),"
    "(SELECT pl.slot FROM saves pl WHERE pl.user_id = {user}"
    "  ORDER BY pl.updated_at DESC, pl.slot ASC LIMIT 1))"
)


def _playing_slot(db, user_id):
    """
    The character this account is playing right now, or None if it has none.

    WHAT THE CLIENT SAID, WHILE IT IS STILL SAYING IT. The broadcast poll
    carries the slot (see stamp_presence), so a session seen inside
    ONLINE_WINDOW_SECONDS whose playing_slot still has a save behind it is the
    answer - freshest session first, because one account can be logged in on
    two machines and the one that spoke last is the one at the keyboard.

    OTHERWISE THE LAST SAVE, which is what every other "which character" in
    this file asks (the online list, the guild roster). It is right most of
    the time and wrong in exactly the moment that mattered here: the minute
    after somebody picks a different character, before anything has changed
    enough to be saved.

    THE RULE IS PLAYING_SLOT_SQL, and this runs it. The nearby list needs the
    same answer for twenty accounts inside one query, and a Python copy of the
    rule beside a SQL copy is two opinions about who is playing whom.
    """
    now = int(time.time())
    row = db.execute(
        "SELECT %s AS slot" % PLAYING_SLOT_SQL.format(user="?"),
        (user_id, now - ONLINE_WINDOW_SECONDS, now, user_id),
    ).fetchone()
    return None if row is None or row["slot"] is None else int(row["slot"])


def _playing_area(db, user_id):
    """The area of the character this account is playing, or "" with none."""
    slot = _playing_slot(db, user_id)
    if slot is None:
        return ""
    row = db.execute("SELECT area FROM saves WHERE user_id = ? AND slot = ?",
                     (user_id, slot)).fetchone()
    return str(row["area"] or "") if row is not None else ""


# THE NAMES OTHER AREAS FIND A TRADE BY, which are trade/'s: the broadcast
# poll's one line about an open trade, the open trade a rollback or a
# character delete refuses on, and the caller's recent ones (test_trades.py
# traces their SQL through these names).
_trade_summary = trade_package.summary
_trade_find_open = trade_package.find_open
_trade_find_recent = trade_package.find_recent
_trade_find_page = trade_package.find_page


def _mark_resync(db, trade):
    """
    Flag both characters in a finished trade as holding a stale picture.

    THE BUG THIS EXISTS FOR, reproduced before it was fixed: whoever accepts
    FIRST is not the one whose request runs the trade. Their client is told
    nothing, goes on showing the bag it had, and the next time they so much as
    drag a potion it saves that bag with PUT /api/character/inventory - which
    is a whole-bag replace. The item they received is not in it, so the replace
    deletes it. The trade worked and the player lost what they traded for.

    BOTH SIDES, not only the one who was not asking. The one who was asking is
    handed the result in the confirm response - unless that response is lost
    to a timeout, and then they are in exactly the same position. Flagging them
    too costs, in the ordinary case, one redundant re-read of a bag that
    already matches.

    NO COMMIT. This is part of the trade's own transaction (trade/routes.py,
    execute()): a trade is never finished without the flag, and the flag never
    outlives a rolled-back trade.
    """
    for prefix in ("a", "b"):
        db.execute(
            "UPDATE saves SET resync_trade = ? WHERE user_id = ? AND slot = ?",
            (trade["trade_id"], int(trade["%s_user" % prefix]), int(trade["%s_slot" % prefix])),
        )


def _take_resync(db, user_id, slot=None):
    """
    What a flagged character now holds, and clear the flag. None if in step.

    {"slot", "gold", "inventory", "trade"} - the server's bag and purse, and a
    line about the trade that changed them. Delivered by whichever of three
    routes reaches the client first: the trade window's poll, the broadcast
    poll, or a refused bag write. See _mark_resync().

    CLEARED ON DELIVERY. What is delivered is read NOW, not at the moment of the
    trade, so it is the latest truth and applying it twice is harmless; there is
    no older copy that could arrive late and roll anything back.

    COMMITS, because every caller is a read that would otherwise throw the
    clearing away when the request's connection closes.
    """
    if slot is None:
        row = db.execute(
            "SELECT slot, gold, resync_trade FROM saves"
            " WHERE user_id = ? AND resync_trade IS NOT NULL"
            " ORDER BY updated_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT slot, gold, resync_trade FROM saves"
            " WHERE user_id = ? AND slot = ? AND resync_trade IS NOT NULL",
            (user_id, int(slot)),
        ).fetchone()
    if row is None:
        return None

    slot = int(row["slot"])
    db.execute("UPDATE saves SET resync_trade = NULL WHERE user_id = ? AND slot = ?",
               (user_id, slot))
    db.commit()
    resync = {
        "slot": slot,
        "gold": int(row["gold"]),
        "inventory": inventory_payload(user_id, slot),
        "trade": None,
    }
    # OR WHAT THE OWNER GAVE, which rides the same flag (see GIFT_MARK): the
    # bag the same way, and `gifts` - who gave what - for the game's line. A
    # game from before gifts reads trade = None and says "Your backpack was
    # updated by the server", which is still true.
    if str(row["resync_trade"]).startswith(GIFT_MARK):
        resync["gifts"] = _gifts_in(row["resync_trade"])
    else:
        resync["trade"] = trade_package.record(db, row["resync_trade"], user_id)
    return resync


# THE TRADE ROUTES ARE trade/ (18 Oct): all seven, the execution and every
# write to trades, trade_items and trade_holds. The backpack grants it pays
# with are the carry's, further down this file, so they are asked for when a
# trade runs rather than handed in before they exist.
trade_package.register(
    app,
    require_auth=require_auth,
    require_role=require_role,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    slot_exists=_slot_exists,
    ignores=ignores,
    presence_for=_presence_for,
    online_window=ONLINE_WINDOW_SECONDS,
    playing_slot=_playing_slot,
    trades_enabled=trades_enabled,
    take_from_backpack=lambda user_id, slot, item_id, quantity, keys_last=False:
        _take_from_backpack(user_id, slot, item_id, quantity, keys_last=keys_last),
    add_to_backpack=lambda user_id, slot, item_id, quantity:
        _add_to_backpack(user_id, slot, item_id, quantity),
    gold_delta=gold_delta,
    inventory_of=inventory_payload,
    mark_resync=_mark_resync,
    take_resync=_take_resync,
    name_hue_of=name_hue_of,
    moderation_target=_moderation_target,
    query_int=_query_int,
    staff_page=(STAFF_PAGE_DEFAULT, STAFF_PAGE_MAX),
    catalogue=gamedata,
)

# =============================================================================
# THE OWNER'S TOOLS, REGISTERED - the ownertools/ package (see above)
# =============================================================================
# Here rather than at their old place because of what they are handed: the
# carry's grants, the skills, which character is played and the open trade
# are all defined above this line and after that one.
import ownertools as ownertools_package  # noqa: E402

ownertools_package.register(
    app,
    require_auth=require_auth,
    require_owner=require_owner,
    require_role=require_role,
    require_fresh_password=require_fresh_password,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    slot_exists=_slot_exists,
    catalogue=gamedata,
    # A LOOKUP: test_concurrency.py holds this module's _ensure_account open.
    ensure_account=lambda user_id: _ensure_account(user_id),
    gold_delta=gold_delta,
    bank_gold_delta=bank_gold_delta,
    insufficient_gold=InsufficientGold,
    record_gift=_record_gift,
    record_gold_gift=_record_gold_gift,
    find_user=_user_by_name,
    owner_may_touch=owner_may_touch,
    owner_untouchable=OWNER_UNTOUCHABLE,
    playing_slot=_playing_slot,
    playing_area=_playing_area,
    take_snapshot=take_snapshot,
    character_state=_character_state,
    stored_json=_stored_json,
    derived_stats=_derived_stats,
    levelup_grant_id=LEVELUP_GRANT_ID,
    log_staff_action=log_staff_action,
    valid_skills=VALID_SKILLS,
    max_skill_level=MAX_SKILL_LEVEL,
    stack_limit=_stack_limit,
    add_to_backpack=_add_to_backpack,
    bag_cells=INVENTORY_CAPACITY,
    inventory_of=inventory_payload,
    mark_gift=_mark_gift,
    online_window=ONLINE_WINDOW_SECONDS,
    gifts_since_key=OWNER_GIFTS_SINCE_KEY,
    snapshots_kept=SNAPSHOTS_KEPT,
    snapshot_every=SNAPSHOT_EVERY_SECONDS,
    find_open_trade=_trade_find_open,
    carry_cells=CARRY_CAPACITY,
    owns_item=_owns_item,
    has_owner_powers=has_owner_powers,
    queue_teleport=queue_teleport,
    teleport_spacing=TELEPORT_SPACING,
    post_broadcast=post_broadcast,
    moderation_target=_moderation_target,
    note_refill=_note_refill,
)

# Named here as well, for the suites that read them off this module;
# ownertools/services.py is where they are set.
DEBUG_GOLD_MAX = ownertools_package.DEBUG_GOLD_MAX
STAFF_LEVEL_MAX = ownertools_package.STAFF_LEVEL_MAX
STAFF_SKILL_ORDER = ownertools_package.STAFF_SKILL_ORDER
ROLLBACK_REASON = ownertools_package.ROLLBACK_REASON


# =============================================================================
# WHO IS AROUND - the players/ package (since 18 Oct)
# =============================================================================
# GET /api/players/online and /nearby, and POST /api/presence/ticket - the
# front door of presence.py, the socket that draws players in each other's
# worlds - as they were here: endpoint names, docstrings and every message
# (routes.lock). players/__init__.py says what each list discloses and why the
# identity on a ticket is the server's. What stays here is what other areas ask
# too: which character an account is playing (PLAYING_SLOT_SQL, _playing_slot()
# and _playing_area(), above), the online window, how a person is shown, the
# caller's guild and the token hashing.
#
# THE PRESENCE SOCKET'S SETTINGS are read here with every other setting:
# ELUSION_PRESENCE_URL names the socket outright; without it the address is
# worked out per request (players/services.py, socket_url()).
PRESENCE_PORT = int(os.environ.get("ELUSION_PRESENCE_PORT", "5001") or 5001)
PRESENCE_URL = os.environ.get("ELUSION_PRESENCE_URL", "").strip()

import players as players_package  # noqa: E402

players_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    catalogue=gamedata,
    playing_area=_playing_area,
    playing_slot_sql=PLAYING_SLOT_SQL,
    online_window=ONLINE_WINDOW_SECONDS,
    role_for=role_for,
    default_role=DEFAULT_ROLE,
    name_hue_of=name_hue_of,
    guild_tag=guild_tag,
    pvp_enabled=pvp_enabled,
    own_guild=_own_guild,
    token_hash=_token_hash,
    bearer_token=bearer_token,
    presence_url=PRESENCE_URL,
    presence_port=PRESENCE_PORT,
    proxy_hops=TRUSTED_PROXY_HOPS,
)

# Named here as well, for anything that reads them off this module;
# players/services.py is where they are set.
NEARBY_LIMIT = players_package.NEARBY_LIMIT
ONLINE_LIST_LIMIT = players_package.ONLINE_LIST_LIMIT
PRESENCE_TICKET_SECONDS = players_package.PRESENCE_TICKET_SECONDS
PRESENCE_PATH = players_package.PRESENCE_PATH


# =============================================================================
# FRIENDS - THE ROUTES - the friends/ package (since 16 Oct)
# =============================================================================
# Ask, answer, list, end: GET /api/friends, POST /api/friends/request,
# /respond and /remove - as they were here, endpoint names and all
# (routes.lock). The rules above (FRIENDS) stay this file's: the limits, who
# is online (_presence_for(), which trade and the poll use too) and how a
# person is shown (friend_entry()).
import friends as friends_package  # noqa: E402

friends_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    find_user=_user_by_name,
    bad_request=bad_request,
    ignores=ignores,
    presence=_presence_for,
    person=friend_entry,
    # Asked each time: MAX_FRIENDS and MAX_PENDING_SENT are this file's
    # tunables, and test_friends.py turns them down to test the limits.
    limits=lambda: (MAX_FRIENDS, MAX_PENDING_SENT),
)


# =============================================================================
# GUILD ROUTES - the guilds/ package (see GUILDS)
# =============================================================================
# /api/guild and its seven actions, /api/staff/guild and /api/guild/list, as
# they were here - endpoint names, docstrings, ranks and every message
# (routes.lock). Handed from here what they share with other areas: the gold
# and bank ledgers founding pays into, the moderation log, the owner's powers,
# how a person is shown, and which character an account is playing.
guilds.register(
    app,
    require_auth=require_auth,
    require_role=require_role,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    find_user=_user_by_name,
    ensure_account=_ensure_account,
    gold_delta=gold_delta,
    bank_gold_delta=bank_gold_delta,
    insufficient_gold=InsufficientGold,
    log_staff_action=log_staff_action,
    has_owner_powers=has_owner_powers,
    role_for=role_for,
    name_hue_of=name_hue_of,
    playing_slot_sql=PLAYING_SLOT_SQL,
    online_window=ONLINE_WINDOW_SECONDS,
)


# =============================================================================
# PARTY ROUTES (game 0.42.0) - the parties/ package (see PARTIES)
# =============================================================================
# /api/party and its five actions, and /api/party/voice, as they were here -
# endpoint names, docstrings and every message (routes.lock). Registered below
# PARTY VOICE, which hands them the relay.


# =============================================================================
# PARTY VOICE (game 0.43.0)
# =============================================================================
# The owner: "I really really want the option for voice chat in game for
# parties". The voice is the browser's own - WebRTC, with its echo
# cancellation and noise suppression - from each game in a party to each
# other, set up through the presence socket (presence.py, PARTY VOICE). It
# never passes through this server.
#
# THROUGH A RELAY, ALWAYS. Two browsers calling each other directly learn each
# other's internet address, and in a game with strangers that is somebody's
# home. So every call goes through a TURN relay (Cloudflare's), and the game
# is told to use nothing else (policy "relay"): each player sees only the
# relay. The relay needs a username and password; this route makes short-lived
# ones from the server's TURN key, which never leaves the server
# (ELUSION_TURN_KEY_ID and ELUSION_TURN_KEY_TOKEN in elusion.env - DEPLOY.md).
# Without a key the route says voice is not set up, and the game says so.
#
# ELUSION_VOICE_DIRECT=on is for a test on one computer: no relay, every
# address allowed. Never on a public server.
#
# THE ROUTE AND ITS RULES ARE parties/'s (GET /api/party/voice, how long a
# player's credentials are reused, how often the relay may be asked). WHAT
# STAYS HERE is the server's side of it: the TURN key, read from the settings
# (voice_settings()), and the one call out to Cloudflare that uses it
# (_turn_fetch()) - handed to the package, which passes the key straight from
# one to the other and keeps nothing - and the credentials handed out, kept
# per process in the two dicts below.
VOICE_TURN_URL = "https://rtc.live.cloudflare.com/v1/turn/keys/%s/credentials/generate-ice-servers"
VOICE_TURN_TIMEOUT = 6
VOICE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
# user_id -> (expires_at, ice_servers); and user_id -> [times it asked the relay].
_voice_issued = {}
_voice_asks = {}
_voice_lock = threading.Lock()


def voice_settings():
    """("direct" | "relay" | "", key id, key token) from the environment."""
    if os.environ.get("ELUSION_VOICE_DIRECT", "").strip().lower() in ("1", "on", "yes", "true"):
        return "direct", "", ""
    key_id = os.environ.get("ELUSION_TURN_KEY_ID", "").strip()
    token = os.environ.get("ELUSION_TURN_KEY_TOKEN", "").strip()
    if key_id and token and VOICE_KEY_PATTERN.match(key_id):
        return "relay", key_id, token
    return "", "", ""


def _turn_fetch(key_id, token, ttl):
    """The relay's answer: its iceServers list, or None. A seam the tests
    replace; the real one asks Cloudflare."""
    body = json.dumps({"ttl": int(ttl)}).encode("utf-8")
    request_obj = urllib.request.Request(VOICE_TURN_URL % key_id, data=body, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": "ElusionRPG/1.0 (+party voice)",
    })
    try:
        with urllib.request.urlopen(request_obj, timeout=VOICE_TURN_TIMEOUT) as answer:
            data = json.loads(answer.read(65536).decode("utf-8"))
    except urllib.error.HTTPError as error:
        # The status only: the request carried the key's token.
        app.logger.warning("[VOICE] the TURN relay answered %d", error.code)
        return None
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as error:
        app.logger.warning("[VOICE] the TURN relay could not be reached: %s", type(error).__name__)
        return None
    return data.get("iceServers") if isinstance(data, dict) else None


# Named here as well, for test_voice.py and anything else that reads them off
# this module; parties/services.py is where they are set.
VOICE_TURN_TTL = parties.VOICE_TURN_TTL
VOICE_TURN_REUSE_SECONDS = parties.VOICE_TURN_REUSE_SECONDS
VOICE_FETCHES_PER_HOUR = parties.VOICE_FETCHES_PER_HOUR
clean_ice_servers = parties.clean_ice_servers

parties.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    find_user=_user_by_name,
    ignores=ignores,
    playing_slot_sql=PLAYING_SLOT_SQL,
    online_window=ONLINE_WINDOW_SECONDS,
    voice_settings=voice_settings,
    # LOOKED UP ON EACH CALL, so test_voice.py's stand-in for the relay is the
    # one asked: it replaces this module's _turn_fetch.
    turn_fetch=lambda key_id, token, ttl: _turn_fetch(key_id, token, ttl),
    voice_issued=_voice_issued,
    voice_asks=_voice_asks,
    voice_lock=_voice_lock,
)


# THE KINGDOM TAX BOARD IS kingdom/ (18 Oct): GET /api/economy/kingdom, the
# board's cache and every read it makes of the two ledgers. register() hands
# back cache_clear(), which the suites call between moving gold and reading the
# board; the size of the board is still asked here by its old name.
import kingdom as kingdom_package  # noqa: E402

_KINGDOM = kingdom_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    guild_tag=guild_tag,
)
_kingdom_cache_clear = _KINGDOM.cache_clear
KINGDOM_BOARD_SIZE = kingdom_package.KINGDOM_BOARD_SIZE
KINGDOM_BOARD_CACHE_SECONDS = kingdom_package.KINGDOM_BOARD_CACHE_SECONDS


@app.get("/api/economy/supply")
@require_auth
@require_role("dev")
def economy_supply():
    """
    Gold supply and the ledger invariant: is every coin accounted for
    ---
    tags:
      - Economy
    description: >
      What the server recorded creating and destroying, against what players
      actually hold. balanced=false means gold moved without going through
      gold_delta() - a bug or a dupe - and drift says by how much.
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
    responses:
      200:
        description: The current supply figures
      404:
        description: >
          Not staff. require_role answers 404 rather than 403 on purpose - a
          403 confirms the route exists to someone who may not know it does.
      401:
        description: Missing, invalid or expired token
    """
    # DEV AND ABOVE, not mod. This is the audit instrument: it says how much
    # currency exists and whether the books are straight, and a moderator needs
    # neither. See role_at_least() for the ladder.
    return gold_supply(get_db())


# THE CARRY'S ROUTES - PUT /api/character/inventory and the three one-cell
# routes (move, discard, cash a pile) - are carry/'s since 18 Oct, and the
# skills write and the training report (PUT /api/character/skills, POST
# /api/skill/train) are skills/'s: both registered at THE CARRY AND THE
# SKILLS, above. GET /api/character, the read that answers with all of them,
# is saves/'s (THE CHARACTER, below).


# =============================================================================
# CONSUMING AN ITEM, AND EQUIPMENT
# =============================================================================
# The consuming/ and equipment/ packages since 18 Oct: using a consumable, and
# putting gear on and taking it off. Both are the same shape - the client
# asks, the server moves the item through the backpack grants, the client
# renders the answer - and why each had to be a server route is in its
# __init__.py. What stays here: the grants (carry/'s, handed back at THE CARRY
# AND THE SKILLS above, still asked for by name when a route runs),
# _derived_stats() - the one way to ask
# a character's maxima, which reads what is worn - and the catalogue, handed
# in whole so a suite's stand-in for one of its functions is heard.
import consuming as consuming_package  # noqa: E402
import equipment as equipment_package  # noqa: E402

consuming_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    parse_cell=parse_cell,
    carry_capacity=CARRY_CAPACITY,
    catalogue=gamedata,
    skills_of=skills_payload,
    valid_skills=VALID_SKILLS,
    take_from_backpack=lambda user_id, slot, item_id, quantity, prefer=None:
        _take_from_backpack(user_id, slot, item_id, quantity, prefer=prefer),
    inventory_of=inventory_payload,
    note_refill=_note_refill,
)

equipment_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    parse_cell=parse_cell,
    carry_capacity=CARRY_CAPACITY,
    equip_slots=EQUIP_SLOTS,
    catalogue=gamedata,
    derived_stats=_derived_stats,
    take_from_backpack=lambda user_id, slot, item_id, quantity, prefer=None:
        _take_from_backpack(user_id, slot, item_id, quantity, prefer=prefer),
    add_to_backpack=lambda user_id, slot, item_id, quantity:
        _add_to_backpack(user_id, slot, item_id, quantity),
    inventory_of=inventory_payload,
)


# =============================================================================
# REVIVING, AND RESPAWNING
# =============================================================================
# The death/ package since 18 Oct: the death screen's two exits, revive (paid
# in lusions or in gold) and respawn (paid in everything carried and worn), and
# why both had to move onto the server (death/__init__.py). What stays here:
# the three ledgers a death pays through, the account row - asked for on every
# call, because test_concurrency slows it - _derived_stats(), and the grant ids
# the healing reconciler reads. The grant itself is written through status/'s
# note_refill() (_note_refill), as a potion's and a level-up's are.
import death as death_package  # noqa: E402

death_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    ensure_account=lambda user_id: _ensure_account(user_id),
    catalogue=gamedata,
    derived_stats=_derived_stats,
    gold_delta=gold_delta,
    bank_gold_delta=bank_gold_delta,
    lusion_delta=lusion_delta,
    lusion_gold_value=LUSION_GOLD_VALUE,
    revive_grant_id=REVIVE_GRANT_ID,
    respawn_grant_id=RESPAWN_GRANT_ID,
    status_of=status_payload,
    note_refill=_note_refill,
)


# =============================================================================
# THE CHARACTER - the saves/ package (since 18 Oct)
# =============================================================================
# GET and PUT /api/save, GET /api/character and POST /api/character/delete, as
# they were - endpoint names, docstrings and every message (routes.lock).
# saves/__init__.py tells what a save still writes and what it ignores.
# Registered here, after everything it is handed, rather than at SAVES above.
#
# DELETING A CHARACTER was asked for on day 1: four fixed slots, one per
# class, and no way to start a class again. Deleting is the character's own
# rows - its save, its bag, its skills, its loot bags and heal grants - and
# nothing of the account's: the bank, the lusions, friends and guild all
# belong to the account and stay.

CHARACTER_DELETIONS_KEPT = 10
CHARACTER_DELETE_REASON = "character-deleted"


import saves as saves_package  # noqa: E402

saves_package.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    clean_text=clean_player_text,
    owns_item=_owns_item,
    stored_json=_stored_json,
    derived_stats=_derived_stats,
    catalogue=gamedata,
    take_snapshot=take_snapshot,
    status_of=status_payload,
    inventory_of=inventory_payload,
    skills_of=skills_payload,
    find_open_trade=_trade_find_open,
    gold_delta=gold_delta,
    deletions_kept=CHARACTER_DELETIONS_KEPT,
    delete_reason=CHARACTER_DELETE_REASON,
)

# Named here as well, for anything that reads them off this module;
# saves/services.py is where they are set.
VALID_CLASSES = saves_package.VALID_CLASSES
MAX_EXPLORED_BYTES = saves_package.MAX_EXPLORED_BYTES
MAX_EXPLORED_TILES = saves_package.MAX_EXPLORED_TILES


# =============================================================================
# LOOT BAGS - the loot/ package (since 18 Oct)
# =============================================================================
# POST /api/loot/take and GET /api/loot/bag, as they were here - endpoint
# names, docstrings and every message (routes.lock) - and the two tables'
# SQL. loot/__init__.py tells why a bag is the server's. A new bag is made
# there too, by the create_bag() register() hands back (kept here as
# _create_loot_bag(), for the kill), and the money and the backpack the take
# pays into are this file's, handed in at the take's old place under THE
# SERVER'S GRANTS. The other helpers that were under this banner went to
# their own areas: the kill's record to kill/, whether an account holds an
# item and taking from the backpack to carry/.
import loot  # noqa: E402

# Named here as well, for the suites and anything else that reads them off
# this module; loot/services.py is where they are set.
LOOT_BAG_TTL_SECONDS = loot.LOOT_BAG_TTL_SECONDS
LOOT_BAG_CAPACITY = loot.LOOT_BAG_CAPACITY

# The bait, the rod's suffix and the cast bucket are fishing's own: see the
# gathering/ package (gathering/services.py).

# The item_id the client uses for the lusion pile - lootbaginventory.gd's
# LUSION_ITEM_ID. There is no constant for it in gamedata.json because the game
# has never needed to name it; the server does, because it is what a duplicate
# pet turns into.
LUSIONS_ITEM_ID = "lusions"


# =============================================================================
# THE KILL RECORD (game 0.9.0) - the killrecord/ package (since 16 Oct)
# =============================================================================
# GET /api/kills and /api/kills/everyone, the game's Kills window: the counts
# kill_tally holds - paid kills only - and the day counting began. READS ONLY;
# kill/'s record_kill(), in the kill's own transaction, is what adds to it.
# The first area taken OUT of this file (ARCHITECTURE.md): the routes, their
# docstrings and endpoint names are as they were (routes.lock).
import killrecord  # noqa: E402


def _kill_tally_since():
    raw = get_server_setting(KILL_TALLY_SINCE_KEY, None)
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


killrecord.register(app, require_auth=require_auth, get_db=get_db, parse_slot=parse_slot,
                    max_slot=MAX_SLOT, since=_kill_tally_since)


# =============================================================================
# THE SERVER'S GRANTS - a skill's XP, and items into the backpack
# =============================================================================
# Since 18 Oct the grants are the packages' that own what they write: the XP
# grant is skills/'s, the backpack's two are carry/'s, all three registered at
# THE CARRY AND THE SKILLS above and kept callable here as _grant_skill_xp(),
# _add_to_backpack() and _take_from_backpack(). The loot bag below and
# fishing and cooking are handed them by those names.


# THE LOOT BAG'S TWO ROUTES - the loot/ package (see LOOT BAGS). Handed the
# money and the backpack from here: the gold and lusion ledgers, the backpack
# grant, the trade hold, and whether a pet is already held. It hands back the
# making of a new bag, which the kill (kill/, above) is handed as a lookup.
_LOOT = loot.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    bad_request=bad_request,
    parse_stat=parse_stat,
    item_row=gamedata.item_row,
    owns_item=_owns_item,
    lusions_item_id=LUSIONS_ITEM_ID,
    # Asked on each take: the catalogue's constant.
    dupe_pet_lusions=lambda: int(gamedata.CONSTANTS.get("dupe_pet_lusions", 20)),
    # LOOKED UP ON EACH TAKE: test_concurrency.py holds this module's
    # gold_item_value open to widen the race the write lock closes, and a
    # function handed in once would be the one it never slowed.
    gold_item_value=lambda item_id: gold_item_value(item_id),
    gold_delta=gold_delta,
    lusion_delta=lusion_delta,
    ensure_account=_ensure_account,
    add_to_backpack=_add_to_backpack,
    place_trade_hold=place_trade_hold,
    status_of=status_payload,
    inventory_of=inventory_payload,
    bag_cells=INVENTORY_CAPACITY,
)
_create_loot_bag = _LOOT.create_bag


# =============================================================================
# FISHING AND COOKING - the gathering/ package (since 18 Oct)
# =============================================================================
# POST /api/fishing/catch and /api/cooking/cook, as they were here - endpoint
# names, docstrings and every message (routes.lock). What a rod lands and what
# a fire makes of it are the package's: the bait, the rod, the cast bucket.
# What they pay with is handed in - _grant_skill_xp(), _add_to_backpack() and
# _take_from_backpack(), skills/'s and carry/'s since 18 Oct, which the loot
# bag, the shop, trades, the bank and gifts pay with too.
import gathering  # noqa: E402

# Named here as well, for test_gathering.py and anything else that reads them
# off this module; gathering/services.py is where they are set.
FISHING_BAIT_ID = gathering.FISHING_BAIT_ID
CAST_BUCKET_CAPACITY = gathering.CAST_BUCKET_CAPACITY
CAST_TOKENS_PER_SECOND = gathering.CAST_TOKENS_PER_SECOND

gathering.register(
    app,
    require_auth=require_auth,
    get_db=get_db,
    parse_slot=parse_slot,
    max_slot=MAX_SLOT,
    bad_request=bad_request,
    slot_exists=_slot_exists,
    item_row=gamedata.item_row,
    # Looked up on each roll, so a suite's stand-in on gamedata is heard.
    roll_catch=lambda rod_tier, level: gamedata.roll_fishing_catch(rod_tier, level),
    roll_cook=lambda item_id, level: gamedata.roll_cook(item_id, level),
    skills_of=skills_payload,
    take_from_backpack=_take_from_backpack,
    add_to_backpack=_add_to_backpack,
    grant_skill_xp=_grant_skill_xp,
    status_of=status_payload,
    inventory_of=inventory_payload,
    bag_cells=INVENTORY_CAPACITY,
)


# =============================================================================
# THE GIFTS LEDGER'S PAST (see THE GIFTS LEDGER)
# =============================================================================
#
# HERE, AT THE END OF THE FILE, NOT IN init_db(). A gift's worth is
# gift_worth(), which reads gold_item_value() and LUSIONS_ITEM_ID - defined far
# below init_db()'s call, which runs at import (CLAUDE.md, "init_db() runs at
# import time"). By this line everything exists. It still runs at import, so
# every process that serves the routes has run it first.
#
# ONCE, under BEGIN IMMEDIATE: two workers importing together would otherwise
# both find no marker and read the past in twice. The second waits for the
# lock, finds the marker, and leaves.

_GIFT_LINE = re.compile(r"^(\d+) x (\S+) into (?:.*\(slot (\d+)\)|slot (\d+))")


def _backfill_owner_gifts():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        done = db.execute("SELECT value FROM server_settings WHERE key = ?",
                          (OWNER_GIFTS_SINCE_KEY,)).fetchone()
        if done is not None:
            db.rollback()
            return
        read = passed_over = 0
        for row in db.execute(
                "SELECT created_at, actor_id, actor_name, target_id, target_name, detail"
                " FROM staff_actions WHERE action IN ('give', 'grant') ORDER BY id").fetchall():
            found = _GIFT_LINE.match(str(row["detail"] or ""))
            if found is None:
                passed_over += 1
                continue
            slot = found.group(3) if found.group(3) is not None else found.group(4)
            _record_gift(db, {"id": row["actor_id"], "username": row["actor_name"]},
                         row["target_id"], row["target_name"], int(slot) if slot is not None else None,
                         found.group(2), int(found.group(1)), "log", at=row["created_at"])
            read += 1
        # The owner's own gold: staff_gold is only ever the owner to himself.
        for row in db.execute(
                "SELECT l.at, l.user_id, l.slot, l.delta, COALESCE(u.username, '') AS username"
                " FROM gold_ledger l LEFT JOIN users u ON u.id = l.user_id"
                " WHERE l.reason = 'staff_gold' ORDER BY l.id").fetchall():
            who = {"id": row["user_id"], "username": row["username"]}
            _record_gold_gift(db, who, row["user_id"], row["username"], row["slot"], row["delta"],
                              "log", at=row["at"])
            read += 1
        first = db.execute("SELECT MIN(at) FROM owner_gifts").fetchone()[0]
        now = int(time.time())
        db.execute(
            "INSERT INTO server_settings (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)",
            (OWNER_GIFTS_SINCE_KEY, str(int(first) if first is not None else now), "migration", now),
        )
        db.commit()
        if read or passed_over:
            app.logger.info("[GIFTS] the gifts ledger read %d gift(s) back from the logs;"
                            " %d log line(s) it could not read were passed over", read, passed_over)
    finally:
        db.close()


_backfill_owner_gifts()


# =============================================================================
# PLAYER STATS (game 0.47.0) - the playerstats/ package
# =============================================================================
# The owner: "build this and add it in my gm menu as a button somewhere" - who
# signs up, who comes back, where players stop, what gets played. GET
# /api/server/stats, owner only. THE FIRST FEATURE BUILT THE BE105 WAY: its
# route, its arithmetic and its SQL are three files of their own
# (playerstats/routes.py, services.py, data.py), handed this file's app,
# decorators and database here rather than importing them, so the package
# stands on its own and test_stats.py can drive each layer apart.
# Registered above ENTRY POINT, like every route.
import playerstats  # noqa: E402
import events as events_package  # noqa: E402

playerstats.register(
    app,
    require_auth=require_auth,
    require_owner=require_owner,
    get_db=get_db,
    # Never counted as players: the owner and the co-owners, whoever is in.
    left_out_names=lambda: (OWNER_USERNAME,) + tuple(CO_OWNER_USERNAMES),
)


# =============================================================================
# THE WORLD'S EVENTS (game 0.48.0) - the events/ package
# =============================================================================
# Double XP, more loot, a land made to stir - scheduled ahead by the owner on
# the GM panel's Events tab, and on every game's screen while they are on.
# The owner's plan: "Treat events ... as their own feature area with a clean
# public contract". The contract is events/routes.py's; this file asks the
# EventsAPI it hands back in three places: the broadcast poll (what is on),
# world_stir_for() (what a kill pays) and current_world_tick() (which land a
# stir event makes stir). Until it is registered - at import, just here - it
# is None, and those three carry on without it.
def _event_stir_settings():
    """The stirring lands' own XP and loot as set now, for the events'
    guardrails: {on, xp (a scale), bags (a scale)}."""
    settings = world_settings()
    return {"on": bool(settings["on"]),
            "xp": 1.0 + (settings["xp_percent"] / 100.0 if settings["xp_on"] else 0.0),
            "bags": float(settings["bags_times"]) if settings["bags_on"] else 1.0}


def _log_event_action(detail):
    """An owner's event, in the staff log. A named function, not a lambda,
    so test_moderation.py's walk of log_staff_action's callers finds the
    "event" kind written somewhere."""
    log_staff_action(g.user, "event", "server", None, detail)


world_events = events_package.register(
    app,
    require_auth=require_auth,
    require_owner=require_owner,
    require_fresh_password=require_fresh_password,
    get_db=get_db,
    announce=lambda body, by="": post_broadcast(body, "event", by),
    log_action=_log_event_action,
    clean_text=clean_player_text,
    lands=LANDS,
    # The guardrails, the dry run and the report (game 0.49.0).
    left_out_names=lambda: (OWNER_USERNAME,) + tuple(CO_OWNER_USERNAMES),
    stir=_event_stir_settings,
    element_of=lambda enemy_id: int((gamedata.ENEMIES.get(enemy_id) or {}).get("element", 0) or 0),
    online_window=ONLINE_WINDOW_SECONDS,
    kills_kept=KILL_LOG_RETENTION_SECONDS,
)


# =============================================================================
# THE LAND INSIGHTS (game 0.50.0) - the insights/ package
# =============================================================================
# The owner's "Behavior Insights": for each land, kills, players, new players
# there now, when it last stirred and had an event, whether it is busy, quiet
# or ignored, its busiest hour; who is online by hour; and the next stir with
# a reason. GET /api/server/insights, owner only. And the world's own pick
# (pick_land()) asks InsightsAPI.choose(): the least hunted land, never one a
# new player is learning in while another will do.
import insights as insights_package  # noqa: E402

land_insights = insights_package.register(
    app,
    require_auth=require_auth,
    require_owner=require_owner,
    get_db=get_db,
    lands=LANDS,
    element_of=lambda enemy_id: int((gamedata.ENEMIES.get(enemy_id) or {}).get("element", 0) or 0),
    left_out_names=lambda: (OWNER_USERNAME,) + tuple(CO_OWNER_USERNAMES),
    next_stir=world_next_stir,
)


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
