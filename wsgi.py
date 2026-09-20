# wsgi.py - the entry point a real server imports.
#
# app.py's own `if __name__ == "__main__"` block runs Flask's DEVELOPMENT
# server. That is the right thing for a local dev loop and the wrong thing for
# anything anyone else can reach: it is single-threaded by default, it has no
# request limits, and it is not written to sit on a hostile network.
#
# A WSGI server imports this module and serves `application`. Because the run
# block in app.py is guarded, importing it starts nothing - the routes register
# and the process hands control to the server instead.
#
#   Linux    gunicorn -w 4 -b 127.0.0.1:5000 wsgi:application
#   Windows  waitress-serve --listen=127.0.0.1:5000 --threads=32 --connection-limit=2000 wsgi:application
#
# THE WINDOWS LINE IS ONE LINE ON PURPOSE. It was briefly written wrapped with
# a trailing "\", which is a line continuation in bash and is NOT one in
# PowerShell or cmd - there the backslash is passed through as an argument and
# waitress tries to import a module called "\". A command in a comment gets
# copied and pasted, so it has to be pasteable on the shell it is labelled for.
#
# THOSE TWO WAITRESS FLAGS ARE NOT TUNING, THEY ARE THE DIFFERENCE BETWEEN
# SERVING AND NOT SERVING, and the bare command above them was measured
# failing without them.
#
# waitress defaults to threads=4 and connection_limit=100. The connection
# limit is the one that bites first and it does not degrade - past 100 open
# connections waitress logs "total open connections reached the connection
# limit, no longer accepting new connections" and REFUSES new ones outright.
# A player at that moment does not get a slow game, they get no server.
#
# Measured against this app on a 2-core box: with the defaults, provisioning
# 500 players hit the connection limit and the log filled with refusals. With
# --threads=32 --connection-limit=2000 the same 500 players sustained 254
# saves/second, p99 1.3s, and not one refusal.
#
# 100 connections is roughly 100 players, because a player holds a connection
# for the length of their request and the client saves every 2 seconds. Size
# the limit above the player count you expect, not above the request rate.
#
# THE CEILING BEHIND THESE FLAGS IS SQLITE, and no flag moves it: SQLite
# serialises writes, one at a time, so the whole server has a single write
# lane no matter how many threads feed it. The client debounces saves to one
# per 2 seconds per player, which is ~0.5 writes/sec/player - so the write
# lane is the player ceiling, and it is in the low hundreds rather than the
# thousands. Postgres is the answer when that number is the constraint, and
# nothing above is a substitute for it.
#
# BIND TO LOOPBACK, NOT 0.0.0.0. The reverse proxy in front terminates TLS and
# talks to this over localhost. Binding the app itself to a public interface
# means anyone can reach it directly, skipping the proxy - and skipping the
# proxy means skipping HTTPS, which puts bearer tokens on the wire in clear.
#
# See DEPLOY.md, which lists what must be true before this is reachable.

import os
import sys

# THE PREFLIGHT RUNS BEFORE app IS IMPORTED, and that ordering is the point.
#
# app.py calls init_db() at import time. With a bad ELUSION_DB the import dies
# inside sqlite3.connect() with "unable to open database file" - a true message
# about the wrong layer, and nothing about the setting that caused it. Checking
# first means a misconfiguration is reported as a misconfiguration.
#
# The checks are deliberately noisy rather than fatal for most items: a server
# that refuses to boot at 3am over a warning is its own outage. The one
# genuinely unsafe combination does stop it.
_problems = []
_warnings = []

# THE ONE THAT MUST NEVER SHIP. The Werkzeug debugger turns any unhandled
# exception into arbitrary code execution on the box holding elusion.db and its
# password hashes. Under a WSGI server the run block never executes, so this
# flag would not take effect anyway - which is exactly why it is worth refusing
# on: someone who set it believes it is doing something.
if os.environ.get("ELUSION_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
    _problems.append(
        "ELUSION_DEBUG is set. It does nothing under a WSGI server, and it means "
        "this config was copied from a dev machine - check what else came with it."
    )

# WITHOUT AN OWNER THERE IS NO OWNER. app.py fails closed on this already, but
# finding out from a staff route at 3am is worse than finding out at boot.
if not os.environ.get("ELUSION_OWNER", "").strip():
    _warnings.append(
        "ELUSION_OWNER is not set - no account will hold the top rank. "
        "Set it in the environment or in a .env beside app.py."
    )

# THE PROXY SETTING, AND WHY IT IS A WARNING IN BOTH DIRECTIONS.
#
# Left at 0 behind a proxy, request.remote_addr is the proxy for every player
# alive, so the per-IP login throttle shares ONE bucket across the whole player
# base and the first brute-force run locks everybody out.
#
# Set above 0 without a proxy, X-Forwarded-For is attacker-controlled: a spray
# sets a fresh fake address per request, nothing ever trips, and the log fills
# with invented addresses implicating people who did nothing.
#
# Nothing here can tell which is true, so this only says the setting exists and
# what each value means. See client_ip() in app.py.
_hops = os.environ.get("ELUSION_TRUSTED_PROXIES", "0").strip() or "0"
try:
    _hops_n = int(_hops)
except ValueError:
    _problems.append("ELUSION_TRUSTED_PROXIES must be an integer, got %r" % _hops)
    _hops_n = 0

if _hops_n == 0:
    _warnings.append(
        "ELUSION_TRUSTED_PROXIES is 0 - the per-IP throttle will read the socket "
        "address. Correct ONLY if nothing proxies this app. Behind nginx or a "
        "load balancer, set it to the number of proxies you run."
    )

# A STALE EQUIPMENT EXPORT IS A SERVER WITH NO AUTHORITY OVER EQUIPMENT, and
# that is a refusal rather than a warning.
#
# parse_equipment() skips gamedata.equip_check() entirely when gamedata.json
# predates the equipment export, and does so deliberately: without
# equip_slot_name every slot reads as "" and failing closed would refuse every
# honest equip on a half-upgraded server. That reasoning is sound for a server
# mid-upgrade. It is not sound for one about to accept players.
#
# In that state the only checks left are "the slot name is in the fallback
# tuple" and "the item exists" - which is exactly the state parse_equipment's
# own docstring names as the one that let {"helm": "embersword"} through.
# Confirmed against this codebase rather than assumed: a level 1 warrior saved
# a 100-damage two-handed sword into the helm slot and it was written straight
# to saves.equipment.
#
# app.py warns about this at boot. That is the right weight for a dev loop and
# the wrong weight for a deploy, where it scrolls past and the server comes up
# looking healthy with a validation gate silently switched off.
#
# gamedata only opens a JSON file at import, so this is safe to ask here,
# before the database or the app exist.
try:
    import gamedata as _gamedata
except Exception as _exc:                                    # noqa: BLE001
    _problems.append(
        "gamedata.json could not be read (%s). The server validates every save "
        "against it and can check nothing without it." % _exc
    )
else:
    if not _gamedata.EQUIP_EXPORTED:
        _problems.append(
            "gamedata.json predates the equipment export, so the server cannot "
            "tell a helmet from a sword and /api/save will accept any item in "
            "any slot. Re-run src/tools/exportgamedata.gd in the Godot editor, "
            "then copy the new gamedata.json beside app.py."
        )

# The database should not live anywhere a web server might serve it. This
# catches the obvious mistake, not every arrangement.
_db = os.environ.get("ELUSION_DB", "")
if any(part in _db.replace("\\", "/").lower().split("/")
       for part in ("static", "public", "www", "htdocs", "wwwroot")):
    _problems.append(
        "ELUSION_DB (%s) is inside a directory that looks web-served. It holds "
        "real password hashes and must never be fetchable over HTTP." % _db
    )

for _line in _warnings:
    print("[DEPLOY] warning: %s" % _line, file=sys.stderr)

if _problems:
    for _line in _problems:
        print("[DEPLOY] REFUSING TO START: %s" % _line, file=sys.stderr)
    raise SystemExit(
        "wsgi.py preflight failed - see the lines above, and DEPLOY.md for what "
        "each setting is for."
    )

print("[DEPLOY] preflight passed (trusted proxy hops: %d)" % _hops_n, file=sys.stderr)


# ONLY NOW. Everything above ran without touching the database or the app, so a
# bad setting is reported as one rather than surfacing three frames deep.
from app import app as application          # noqa: E402,F401  - this IS the export
