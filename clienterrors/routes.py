"""clienterrors/routes.py - the edge (game 0.88.0).

    POST /api/client/errors          any signed-in game
    GET  /api/server/client-errors   owner and co-owner, read only

register() is handed app.py's own pieces - it never imports app.py:

    require_auth      the token check (puts the account on g.user)
    require_owner     the owner or a co-owner, the bare 404 to anybody else
    get_db            the request's database connection
    bad_request       app.py's 400 answer
"""
import time
from types import SimpleNamespace

from flask import g, request

from . import data, services


def register(app, *, require_auth, require_owner, get_db, bad_request):
    """Add both routes."""

    def report_client_errors():
        """
        A game tells the server the script errors it hit since its last report - each one's script and line, its message and how many times - with its version and area. Kept per account and day, a count rather than a row a time, and shown to the owner without names
        ---
        tags:
          - Ops
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
                version: {type: string, description: "the game's version, 0.88.0"}
                area: {type: string, description: "the area the player was in"}
                errors:
                  type: array
                  description: "at most 20: {where: res://...gd:12, what: the message, count}"
        responses:
          200:
            description: "kept (counted), dropped (refused or past the day's cap)"
          400:
            description: Not a report
          401:
            description: Missing, invalid or expired token
        """
        report = services.clean_report(request.get_json(silent=True))
        if report is None:
            return bad_request("Send {version, area, errors: [{where, what, count}]}.")
        version, area, errors, dropped = report
        now = int(time.time())
        day = now // services.DAY
        build = services.build_of(request.headers.get("X-Elusion-Build"))
        db = get_db()
        user_id = g.user["id"]
        room = services.ROWS_PER_DAY - data.rows_today(db, user_id, day)
        kept = 0
        for where_at, what, times in errors:
            counted = data.add(db, user_id, day, version, build, area, where_at, what, times, now,
                               services.TIMES_MOST, allow_new=room > 0)
            if counted:
                kept += 1
                if counted == "new":
                    room -= 1
            else:
                dropped += 1
        data.prune(db, day - services.DAYS_KEPT)
        db.commit()
        return {"kept": kept, "dropped": dropped}, 200

    def read_client_errors():
        """
        What broke in players' games - every script error games reported in the last hours (24 unless asked), one entry per error, worst first, with how many players hit it, how many times, the versions and areas, and when first and last; no names (owner and co-owner)
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
            name: hours
            type: integer
            required: false
            description: "how far back, 1 to 336; 24 when absent"
        responses:
          200:
            description: "hours, errors (worst first, at most 50), players (who hit any), times, more"
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner or a co-owner
        """
        now = int(time.time())
        hours = services.hours_asked(request.args.get("hours"))
        rows = data.since(get_db(), now - hours * 3600)
        grouped = services.group(rows)
        return {"now": now, "hours": hours, "errors": grouped[:services.SHOWN],
                "more": max(0, len(grouped) - services.SHOWN),
                "players": len({row["user_id"] for row in rows}),
                "times": sum(int(row["times"]) for row in rows)}, 200

    app.add_url_rule("/api/client/errors", endpoint="report_client_errors",
                     view_func=require_auth(report_client_errors), methods=["POST"])
    app.add_url_rule("/api/server/client-errors", endpoint="read_client_errors",
                     view_func=require_auth(require_owner(read_client_errors)), methods=["GET"])
    return SimpleNamespace()
