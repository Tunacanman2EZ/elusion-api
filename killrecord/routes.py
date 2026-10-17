"""killrecord/routes.py - the edge: the two routes, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth   the token check (puts the account on g.user)
    get_db         the request's database connection
    parse_slot     app.py's reading of a character slot (None when bad)
    max_slot       the highest slot, for the 400's words
    since          () -> the unix time the counting began (kill_tally_since)

THE ENDPOINT NAMES ARE app.py's - kills_mine, kills_everyone - because rate
limiting and request timing key on them, and routes.lock holds them.
"""
from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, parse_slot, max_slot, since):

    def kills_mine():
        """
        Your character's kill record: how many of each monster, first and last
        ---
        tags:
          - Combat
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
            description: Which character
        responses:
          200:
            description: "{slot, since, total, kinds, kills: [{enemy_id, kills, first_at, last_at}]} most killed first"
          400:
            description: Bad slot
          401:
            description: Missing, invalid or expired token
        """
        slot = parse_slot(request.args.get("slot"))
        if slot is None:
            return {"error": "Bad Request", "message": "slot must be 0-%d" % max_slot}, 400
        rows = data.mine(get_db(), g.user["id"], slot)
        return services.mine_answer(slot, rows, since()), 200

    def kills_everyone():
        """
        Everybody's kill record: each monster's total, how many players, and who has killed the most
        ---
        tags:
          - Combat
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: "{since, total, players, kills: [{enemy_id, kills, players, top, top_kills, yours}]} most killed first"
          401:
            description: Missing, invalid or expired token
        """
        db = get_db()
        rows = data.everyone(db, g.user["id"])
        return services.everyone_answer(rows, data.players(db), since()), 200

    app.add_url_rule("/api/kills", endpoint="kills_mine",
                     view_func=require_auth(kills_mine), methods=["GET"])
    app.add_url_rule("/api/kills/everyone", endpoint="kills_everyone",
                     view_func=require_auth(kills_everyone), methods=["GET"])
