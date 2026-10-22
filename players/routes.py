"""players/routes.py - the edge: the online list, the nearby list and the
presence ticket, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth      the token check (puts the account on g.user)
    get_db            the request's database connection
    bad_request       a message -> app.py's 400
    parse_slot        a raw slot -> its int, or None
    max_slot          the highest slot, for the 400's words
    catalogue         gamedata: what an item is (a pet?) and whether it can be
                      worn, for the ticket - asked on every call
    playing_area      (db, user_id) -> the area of the character the account
                      is playing, or "" (app.py's _playing_area())
    playing_slot_sql  PLAYING_SLOT_SQL: the same rule as one SQL expression,
                      for a list that needs it for many accounts in one query
    online_window     ONLINE_WINDOW_SECONDS: how recently a client must have
                      been heard to count as there
    role_for          a user row -> the rank it shows
    default_role      the rank of an account with none stored
    name_hue_of       a row with name_hue -> the colour its name is drawn in
    guild_tag         a guild's name -> its tag
    pvp_enabled       () -> whether the world allows PvP right now
    own_guild         (db, user_id) -> the caller's guild entry, guild_tag and all
    token_hash        a token -> the hash it is stored by
    bearer_token      () -> the token this request came with
    presence_url      ELUSION_PRESENCE_URL, "" when unset
    presence_port     the presence process's own port (ELUSION_PRESENCE_PORT)
    proxy_hops        TRUSTED_PROXY_HOPS: above 0 means behind the proxy

THE ENDPOINT NAMES ARE app.py's - players_online, presence_ticket,
players_nearby - and routes.lock holds them.
"""
import json
import secrets
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, catalogue, playing_area,
             playing_slot_sql, online_window, role_for, default_role, name_hue_of, guild_tag, pvp_enabled,
             own_guild, token_hash, bearer_token, presence_url, presence_port, proxy_hops):

    def players_online():
        """
        Everyone playing right now, and where
        ---
        tags:
          - Players
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: Who is online, their area, and which of them are with you
          401:
            description: Missing, invalid or expired token
        """
        # ANY LOGGED-IN PLAYER, no rank. This is the "who else is here" list every
        # game has, and it is the honest version of presence available today: the
        # server knows which AREA a character is in and nothing finer, which
        # /api/players/nearby says in its own comment. When real positions arrive
        # this route gains a distance and the client does not change shape.
        #
        # WHAT IT DISCLOSES, said plainly because it is a choice and not an
        # oversight: every online player's account name, character name, level and
        # area, to every other player. That is what a who-is-online list IS, and
        # the friends panel already shows the same thing for friends. It does not
        # disclose anything the game does not already put over a character's head.
        user_id = g.user["id"]
        now = int(time.time())
        db = get_db()

        # WHERE THE CALLER IS, so the list can be grouped into "here with you" and
        # "elsewhere" without the client having to ask a second route for its own
        # area. From the character being PLAYED - see app.py's _playing_slot(); a
        # player has up to four and "saved last" is wrong for the minute after a
        # switch.
        my_area = playing_area(db, user_id)

        # BOTH CONDITIONS. last_seen_at says a client was there in the last 45
        # seconds; expires_at says the session is still allowed to be. Either one
        # alone is wrong in a different direction - see the longer note in
        # players_nearby (data.nearby()), which had exactly that bug for one test
        # run.
        rows = data.online(db, now - online_window, now, services.ONLINE_LIST_LIMIT, playing_slot_sql)

        # ONE ENTRY PER ACCOUNT. The join now picks one character each and the
        # GROUP BY folds an account's several sessions into one row, so this is a
        # guard rather than the mechanism - kept because a list that shows somebody
        # twice is the bug a future join would bring back.
        seen_accounts = set()
        players = []
        for row in rows:
            name = row["username"]
            if name in seen_accounts:
                continue
            seen_accounts.add(name)
            players.append({
                "username": name,
                # THE RANK IS SHOWN, because the game already draws it over their
                # head - see the nameplate colours in api.gd. A list that hid what
                # the world shows would just be a worse list.
                "role": role_for(row) if row["role"] is not None else default_role,
                "name_hue": name_hue_of(row),
                "name": row["name"] or "",
                "level": int(row["level"] or 1),
                "area": row["area"] or "",
                # WHO THEY RUN WITH. Empty for somebody in no guild, so the client
                # has one thing to test rather than a null to remember.
                "guild": row["guild"] or "",
                "guild_tag": guild_tag(row["guild"]) if row["guild"] else "",
                "with_you": bool(row["area"] and row["area"] == my_area),
            })
            if len(players) >= services.ONLINE_LIST_LIMIT:
                break

        return {
            "area": my_area,
            "precision": "area",
            "online": len(players),
            "players": players,
            # SO THE MENU CAN SAY SO. One list, one answer about the world's rules.
            "pvp": pvp_enabled(),
        }, 200

    def presence_ticket():
        """
        A ticket to be seen by other players
        ---
        tags:
          - Players
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
            description: "{ticket, expires_in, socket_url}: hand the ticket to the socket at socket_url"
          400:
            description: Bad slot
          404:
            description: No character in that slot
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]
        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        db = get_db()
        save = data.character(db, user_id, slot)
        if save is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        # THE PETS THIS CHARACTER MAY BE SEEN WITH: the ones the account holds,
        # in this character's bag or the shared bank - the rule /api/save uses for
        # active_pet_id (carry/'s owns_item). The socket draws a claimed pet only
        # when it is on this list, so a modified game cannot parade one it never
        # won.
        pets = services.pets_among(data.held(db, user_id, slot), catalogue)

        # WHAT THE BOOKS HOLD THIS CHARACTER TO (presence.py, combatbook.py): the
        # gear it wears and carries and its three fighting skills, from which
        # gamedata.combat_bounds() works out the biggest hit it can land, its
        # damage a second and how fast it walks. Carried as well as worn, so a
        # sword swapped in mid-fight was always within its bounds. Never shown to
        # another player - presence.py's join entry names what it shares.
        gear = services.gear_of(save["equipment"], data.carried(db, user_id, slot), catalogue)
        skills = data.fighting_skills(db, user_id, slot)

        identity = {
            "name": g.user["username"],
            "character": save["name"] or "",
            "cls": save["class_id"] or "",
            "lvl": int(save["level"] or 1),
            "role": role_for(g.user),
            "hue": g.user["name_hue"],
            "guild": own_guild(db, user_id)["guild_tag"],
            "pets": pets,
            "gear": gear,
            "skills": skills,
        }

        now = int(time.time())
        # THE MEAL IN FORCE (0.74.0), for the books: a MIGHT meal raises the
        # biggest hit and the damage a second this character is held to
        # (gamedata.might_percent()), because a real buffed kill must never
        # look too big. Only while it lasts, and only then is the key here - a
        # character who has eaten nothing has the ticket it always had. The
        # game renews its ticket every minute, so the books hear of a meal
        # within one, and the bounds' SLACK covers the minute itself.
        buff = data.food_buff(db, user_id, slot, now)
        if buff is not None:
            identity["buff"] = {"item_id": str(buff["item_id"]), "kind": str(buff["kind"]),
                                "amount": int(buff["amount"]), "ends_at": int(buff["ends_at"])}

        raw = secrets.token_urlsafe(32)
        data.issue_ticket(db, token_hash(raw), token_hash(bearer_token()), user_id, slot,
                          json.dumps(identity, separators=(",", ":")), now,
                          now + services.PRESENCE_TICKET_SECONDS)
        db.commit()
        return {
            "ticket": raw,
            "expires_in": services.PRESENCE_TICKET_SECONDS,
            "socket_url": services.socket_url(presence_url, proxy_hops, presence_port,
                                              request.scheme, request.host),
        }, 200

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
        # The API stores which AREA a character is in (saves.area, written by
        # PUT /api/save). Where one stands is presence.py's: it draws everyone in
        # an area to each other (0.7.0), and since 19 Oct writes a sample every
        # 15 seconds to position_samples, which nothing here reads yet. So
        # "nearby" means "in the same area and online" - what a trade panel
        # needs.
        #
        # THE SEAM, if distance is ever wanted: the query gains a distance test
        # against the newest sample and the response gains a position, and the
        # client does not change shape.
        user_id = g.user["id"]

        slot = parse_slot(request.args.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        db = get_db()
        me = data.area_of(db, user_id, slot)
        if me is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        area = me["area"]

        # ONLINE IS A LIVE SESSION, not a recent save. A save happens on a timer
        # and on scene changes, so "saved recently" lists somebody who alt-tabbed
        # away an hour ago; an unexpired token is the closest thing the server has
        # to "that client is still there". DISTINCT because one account may hold
        # several valid tokens - two machines, or a login that did not log out.
        # Last seen inside the window AND still valid: data.nearby() says why both.
        now = int(time.time())
        rows = data.nearby(db, area, user_id, now - online_window, now, services.NEARBY_LIMIT, playing_slot_sql)

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
                    # DRAWN LIKE EVERY OTHER LIST: in the colour its owner chose,
                    # with the rank as a badge. See nametag.gd.
                    "role": r["role"],
                    "name_hue": name_hue_of(r),
                }
                for r in rows
            ],
        }, 200

    app.add_url_rule("/api/players/online", endpoint="players_online",
                     view_func=require_auth(players_online), methods=["GET"])
    app.add_url_rule("/api/presence/ticket", endpoint="presence_ticket",
                     view_func=require_auth(presence_ticket), methods=["POST"])
    app.add_url_rule("/api/players/nearby", endpoint="players_nearby",
                     view_func=require_auth(players_nearby), methods=["GET"])
