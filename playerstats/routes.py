"""playerstats/routes.py - the HTTP edge of the player stats: one route.

    GET /api/server/stats?offset=<minutes east of UTC>&days=<1-365>

Owner only - require_owner, the same bare 404 to everybody else as every
owner's route, and on /api/staff/powers under the co-owner like the rest. It
only reads, so it asks for no password again (STEP-UP is for what changes
things) and takes no write lock.

THE CONTRACT is the JSON services.build_report() returns; the game's Player
stats window (src/ui/owner/playerstats.gd) is its only reader, and
test_stats.py holds its shape.
"""
import time

from flask import request

import gamedata

from . import data, services


def register(app, *, require_auth, require_owner, get_db, left_out_names):
    """Add the route to `app`. `left_out_names` is called on each request for
    the names never counted as players - the owner and co-owners."""

    def read_player_stats():
        """
        Player numbers: who signs up, who comes back, where players stop, what gets played (owner only)
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
            name: offset
            type: integer
            description: "The owner's minutes east of UTC, so days start at their midnight (-840 to 840)"
          - in: query
            name: days
            type: integer
            description: "How far back a new player signed up (1 to 365, default 30)"
        responses:
          200:
            description: players, road, quit, daily, monsters, classes
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
        """
        try:
            days = int(request.args.get("days", services.WINDOW_DAYS))
        except (TypeError, ValueError):
            days = services.WINDOW_DAYS
        offset = services.clamp_offset(request.args.get("offset", 0))
        now = int(time.time())
        db = get_db()
        report = services.build_report(
            accounts=data.accounts(db),
            characters=data.characters(db),
            last_seen=data.last_seen(db),
            kill_totals=data.kill_totals(db),
            kills_recent=data.kills_since(db, now - (services.DAILY_DAYS + 1) * services.DAY),
            logins_recent=data.logins_since(db, now - (services.DAILY_DAYS + 1) * services.DAY),
            enemies=gamedata.ENEMIES,
            now=now,
            offset_minutes=offset,
            left_out_names=left_out_names(),
            window_days=days,
        )
        return report, 200

    view = require_auth(require_owner(read_player_stats))
    app.add_url_rule("/api/server/stats", endpoint="read_player_stats", view_func=view, methods=["GET"])
    return view
