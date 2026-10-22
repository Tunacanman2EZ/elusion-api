"""economyrules/routes.py - the edge: the economy's numbers, read by every
game and set by the owner (0.74.0).

    GET /api/economy/rules         any player: the numbers the game draws
    GET /api/owner/economy/rules   the owner and a co-owner: every rule, its
                                   bounds, and who changed what
    PUT /api/owner/economy/rules   THE owner: one rule, {key, value}

register() is handed app.py's own pieces - it never imports app.py:

    require_auth            the token check (puts the account on g.user)
    require_owner           the owner or a co-owner, the bare 404 to anybody
                            else
    require_the_owner       the owner alone, a co-owner included out
    require_fresh_password  asks for the password again on a hardened login
    get_db                  the request's database connection
    bad_request             a message -> app.py's 400
    log_staff_action        the moderation log's writer: every change has a
                            line there too, beside the economy's own log
    is_the_owner            users row -> whether it is THE owner (app.py's
                            is_owner()), for the read's can_change

WHO MAY DO WHAT. Reading the table is the co-owner's as well as the owner's -
the same as every other number on the GM panel. CHANGING it is the owner's
alone (require_the_owner), as the co-owners' own switch is: a price is the one
thing on the panel that moves every player's gold at once, so it is kept to the
one account no request can grant. It asks for the password again, like the
switches that change the world.
"""
import time

from flask import g, request

from . import data, services

# How many changes the owner's read brings back, newest first.
CHANGES_SHOWN = 20


def register(app, *, require_auth, require_owner, require_the_owner, require_fresh_password, get_db, bad_request,
             log_staff_action, is_the_owner):

    def owner_payload(db):
        stored = data.rules(db)
        return {
            "rules": services.rule_rows(stored),
            "changes": data.changes(db, CHANGES_SHOWN),
            # Whether THIS reader may change them: the panel draws the editor
            # for the owner and the same numbers, read only, for a co-owner.
            "can_change": bool(is_the_owner(g.user)),
            "cache_seconds": services.CACHE_SECONDS,
        }

    def economy_rules():
        """
        The economy's numbers the game draws - the shop's price and sell multipliers and the trade tax's rate and floor
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
            description: "rules: {key: number} - the owner's number where one is set, else the game's own"
          401:
            description: Missing, invalid or expired token
        """
        # EVERY PLAYER, because every player's shop window draws them. They are
        # prices, not secrets: the shelf already shows what they make.
        return {"rules": services.public(get_db())}, 200

    def owner_economy_rules():
        """
        Every economy rule - the number in force, its default, its bounds, what it does, who set it and when - and the newest changes (owner and co-owner)
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
            description: "rules (one per rule: key, value, default, min, max, whole, about, set, updated_at, updated_by), changes (newest first), can_change"
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner or a co-owner
        """
        return owner_payload(get_db()), 200

    def set_economy_rule():
        """
        Set one economy rule, or put it back to the game's own number (the owner only)
        ---
        tags:
          - Economy
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
              required: [key, value]
              properties:
                key: {type: string, description: "the rule, e.g. trade_tax_rate"}
                value: {type: number, description: "the new number, inside the rule's bounds; null puts the default back"}
        responses:
          200:
            description: "The whole table as the owner's read answers it, with changed - whether anything moved"
          400:
            description: Not a rule, not a number, outside its bounds, or not whole where it must be
          401:
            description: Missing, invalid or expired token
          403:
            description: The password is asked for again (confirm_password)
          404:
            description: Not the owner - a co-owner included
          409:
            description: Would make the shop pay as much as it charges
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return bad_request('send {"key": "<rule>", "value": <a number, or null for the default>}')
        key = str(payload.get("key", "")).strip()
        if key not in services.RULES:
            return bad_request("There is no economy rule called '%s'. The rules: %s."
                               % (key[:64], ", ".join(services.RULES)))
        if "value" not in payload:
            return bad_request('send {"key": "%s", "value": <a number, or null for the default>}' % key)

        db = get_db()
        # READ PAST THE CACHE: a change is checked against the table as it is.
        rows = {row["key"]: row for row in services.rule_rows(data.rules(db))}
        in_force = {name: row["value"] for name, row in rows.items()}
        before = rows[key]["value"] if rows[key]["set"] else None

        raw = payload["value"]
        if raw is None:
            after = None
            effective = services.default(key)
        else:
            number, refusal = services.check(key, raw)
            if refusal:
                return bad_request(refusal)
            after = number
            effective = number
        refusal = services.relational_refusal(key, effective, in_force)
        if refusal:
            return {"error": "Conflict", "message": refusal}, 409

        if after == before:
            # NOTHING MOVED - the same number again, or the default asked for
            # by a rule already at it. Said, and not logged: a log of changes
            # that holds non-changes is harder to read for the one that matters.
            answer = owner_payload(db)
            answer["changed"] = False
            return answer, 200

        now = int(time.time())
        if after is None:
            data.clear_rule(db, key)
        else:
            data.set_rule(db, key, after, now, g.user["username"])
        data.log_change(db, key, before, after, now, g.user["id"], g.user["username"])
        log_staff_action(g.user, "economy_rule", key, None, "%s: %s -> %s" % (
            key,
            services.shown(before) if before is not None else "default (%s)" % services.shown(services.default(key)),
            services.shown(after) if after is not None else "default (%s)" % services.shown(services.default(key))))
        db.commit()
        # CLEARED AT ONCE, in this worker: the next price it answers is the
        # new one. The other worker hears it when its window ends.
        services.clear_cache()
        answer = owner_payload(db)
        answer["changed"] = True
        return answer, 200

    app.add_url_rule("/api/economy/rules", endpoint="economy_rules",
                     view_func=require_auth(economy_rules), methods=["GET"])
    app.add_url_rule("/api/owner/economy/rules", endpoint="owner_economy_rules",
                     view_func=require_auth(require_owner(owner_economy_rules)), methods=["GET"])
    app.add_url_rule("/api/owner/economy/rules", endpoint="set_economy_rule",
                     view_func=require_auth(require_the_owner(require_fresh_password(set_economy_rule))),
                     methods=["PUT"])
