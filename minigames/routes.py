"""minigames/routes.py - the edge (game 0.93.3).

    GET  /api/minigames         every minigame and your wait
    POST /api/minigames/enter   going in: 409 while your wait runs

register() is handed app.py's own pieces - it never imports app.py:

    require_auth   the token check (puts the account on g.user)
    get_db         the request's database connection
    bad_request    app.py's 400 answer
    skips_wait     (username) -> whether this account skips every wait: the
                   owner and a co-owner while the switch is on
"""
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, skips_wait):
    """Add both routes."""

    def listing(db, now):
        entries = data.entries_of(db, g.user["id"])
        skip = bool(skips_wait(g.user["username"]))
        games = []
        for game_id, game in services.GAMES.items():
            entered = entries.get(game_id, 0)
            left = 0 if skip else services.ready_in(entered, game["wait"], now)
            games.append({"id": game_id, "name": game["name"], "wait_seconds": game["wait"],
                          "entered_at": entered, "ready_in": left})
        return {"games": games, "skips_wait": skip}

    def minigames_list():
        """
        Every minigame, and how long until you may go in again
        ---
        tags:
          - Minigames
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: "games: id, name, wait_seconds, entered_at (0 never), ready_in (0 now); skips_wait"
          401:
            description: Not logged in
        """
        return listing(get_db(), int(time.time())), 200

    def minigames_enter():
        """
        Go into a minigame - the wait after it starts now, and a second time inside it is refused
        ---
        tags:
          - Minigames
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
              required: [game]
              properties:
                game: {type: string, description: "a minigame's id: endless"}
        responses:
          200:
            description: "in: game, entered_at, ready_in (the next wait)"
          400:
            description: "not a minigame"
          409:
            description: "your wait is still running: ready_in seconds, and the message says how long"
          401:
            description: Not logged in
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return bad_request("body must be a JSON object")
        game_id = payload.get("game")
        game = services.GAMES.get(game_id) if isinstance(game_id, str) else None
        if game is None:
            return bad_request("no such minigame")
        db = get_db()
        now = int(time.time())
        skip = bool(skips_wait(g.user["username"]))
        left = services.ready_in(data.entries_of(db, g.user["id"]).get(game_id, 0), game["wait"], now)
        if left > 0 and not skip:
            return {"error": "Conflict", "ready_in": left,
                    "message": "%s opens for you again in %s." % (game["name"], services.wait_words(left))}, 409
        data.enter(db, g.user["id"], game_id, now)
        db.commit()
        return {"ok": True, "game": game_id, "entered_at": now,
                "ready_in": 0 if skip else int(game["wait"])}, 200

    app.add_url_rule("/api/minigames", endpoint="minigames_list",
                     view_func=require_auth(minigames_list), methods=["GET"])
    app.add_url_rule("/api/minigames/enter", endpoint="minigames_enter",
                     view_func=require_auth(minigames_enter), methods=["POST"])
