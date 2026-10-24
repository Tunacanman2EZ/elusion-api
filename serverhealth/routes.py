"""serverhealth/routes.py - the edge: the GM panel's Health tab (game 0.78.0).

    GET /api/server/health    the last minute, today and the week; the
                              month's data; the crowd, the tick, the machine,
                              the routes and the kill receipts

register() is handed app.py's own pieces - it never imports app.py, nor
killwatch.py:

    require_auth      the token check (puts the account on g.user)
    require_owner     the owner or a co-owner, the bare 404 to anybody else
    get_db            the request's database connection
    db_path           () -> where the database is - asked on each call, since
                      the suites point it elsewhere - for the disk it is on
    metrics_summary   () -> app.py's /api/metrics answer, this worker's
    receipts_backing  (db) -> killwatch.py's receipts_backing() for its week,
                      or None before the books began
    receipts_days     the week killwatch measures (RECEIPTS_DAYS)
    receipts_on       () -> whether a kill needs the books' receipt now
    min_client_build  () -> the oldest build allowed in
    receipt_build     the build a game must be to wait for a receipt
                      (kill/'s RECEIPT_BUILD)

READ ONLY, and so it asks for no password again: the owner and a co-owner
alike. The switch it reports on is the Testing tab's and the Health tab's,
POST /api/server/receipts, the owner's alone.

THE RECEIPTS ARE KEPT FIVE MINUTES (RECEIPTS_CACHE_SECONDS), per worker:
receipts_backing() reads a week of combat_kills and kill_reports, and the tab
asks every half minute while it is open. ?fresh=1 reads them now.
"""
import os
import threading
import time
from types import SimpleNamespace

from flask import request

from . import data, services

RECEIPTS_CACHE_SECONDS = 300


def register(app, *, require_auth, require_owner, get_db, db_path, metrics_summary, receipts_backing,
             receipts_days, receipts_on, min_client_build, receipt_build):
    """Add the route, and hand back cache_clear() - for the suites, which
    write books rows and then read the receipts."""

    cache = {"at": None, "backing": None}
    lock = threading.Lock()

    def cache_clear():
        with lock:
            cache["at"] = None
            cache["backing"] = None

    def backing_now(db, now, fresh):
        with lock:
            if not fresh and cache["at"] is not None and now - cache["at"] < RECEIPTS_CACHE_SECONDS:
                return cache["backing"], cache["at"]
        try:
            backing = receipts_backing(db)
        except Exception:  # noqa: BLE001
            app.logger.exception("[HEALTH] could not put the week's kills to the books")
            return None, now
        with lock:
            cache["at"], cache["backing"] = now, backing
        return backing, now

    def read_server_health():
        """
        The live server for the GM panel's Health tab - the last minute, today and the week from presence.py's minutes (players, the biggest crowds, the tick, data out, the machine's network, load and memory), the month that data adds up to against the droplet's allowance, the slowest routes on this worker, the disk, and the kill receipts put to the books with a verdict (owner and co-owner)
        ---
        tags:
          - Ops
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: query
            name: fresh
            type: integer
            required: false
            description: "1 reads the kill receipts now rather than from the last five minutes"
        responses:
          200:
            description: "now, presence (reporting, last, day, week), month, crowd, tick, machine, routes and receipts"
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner or a co-owner
        """
        now = int(time.time())
        db = get_db()
        last_row = data.latest_health(db)
        last = services.last_minute(last_row, now)
        day = services.window(data.health_rows(db, now - services.DAY))
        week = services.window(data.health_rows(db, now - services.WEEK))
        projected = services.month(week)
        backing, backing_at = backing_now(db, now, request.args.get("fresh") == "1")
        free, total, db_bytes = data.disk(str(db_path()))
        return {
            "now": now,
            "presence": {"reporting": bool(last and last["reporting"]), "last": last, "day": day, "week": week},
            "month": projected,
            "crowd": services.crowd_verdict(week, projected),
            "tick": services.tick_verdict(last, day),
            "machine": services.machine(last_row, os.cpu_count(), free, total, db_bytes),
            "routes": services.routes(metrics_summary()),
            "receipts": dict(services.receipts(backing, receipts_on(), min_client_build(), receipt_build,
                                               receipts_days, now), as_of=backing_at),
        }, 200

    app.add_url_rule("/api/server/health", endpoint="read_server_health",
                     view_func=require_auth(require_owner(read_server_health)), methods=["GET"])

    return SimpleNamespace(cache_clear=cache_clear)
