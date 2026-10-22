"""kingdom/routes.py - the edge: GET /api/economy/kingdom, as it was in app.py,
and since 0.74.0 POST /api/kingdom/donate, a gift to the crown for a title.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth   the token check (puts the account on g.user)
    get_db         the request's database connection
    guild_tag      a guild's name -> the short tag every list draws
    bad_request    a message -> app.py's 400 (0.74.0, and so on below)
    parse_slot     a raw slot -> its int, or None
    max_slot       the highest slot, for the 400's words
    catalogue      gamedata: the patron titles and the smallest gift
                   (CONSTANTS) - asked on every call
    gold_delta     (db, user_id, slot, delta, reason, detail) -> the new
                   balance: the carried gold, through the ledger

and HANDS BACK cache_clear(), which drops the board's snapshot - app.py keeps
it as _kingdom_cache_clear(), which the suites call between moving gold and
reading the board.

THE ENDPOINT NAMES ARE app.py's - economy_kingdom - and this file's own,
kingdom_donate; routes.lock holds them.
"""
import time
from types import SimpleNamespace

from flask import g, request

from . import data, services
from .services import KINGDOM_BOARD_SIZE


def register(app, *, require_auth, get_db, guild_tag, bad_request, parse_slot, max_slot, catalogue, gold_delta):

    # {"at": unix_seconds, "payload": {...}} - the shared half of the last
    # board. Per register(), which in the server is once: one process, one
    # board, and a dict that holds a few hundred rows. Nothing here is worth a
    # cache library.
    cache = {"at": 0, "payload": None}

    def cache_clear():
        """Drop the snapshot.

        FOR TESTS, which move gold and then read the board expecting to see it.
        A fifteen second window is invisible to a player and fatal to a test
        that asserts a trade moved the total, so the suites call this between
        the two.
        """
        cache["at"] = 0
        cache["payload"] = None

    def board_shared(db):
        """Everything on the board that is the same for every reader.

        SPLIT OUT SO IT CAN BE CACHED. The totals, the breakdown and the ranked
        list do not depend on who is asking, so they are computed once per
        window and handed to everybody; what stays in the endpoint is the one
        block that is personal."""
        # A PATRON'S TITLE ON EVERY ROW (0.74.0), from what the account has
        # given the crown in all - "" below the first. The figure itself stays
        # off the board: the title is what a gift buys to be seen.
        titles = services.patron_titles(catalogue.CONSTANTS)
        board = [{"username": r["username"], "contributed": int(r["given"]),
                  "lusions": 0, "deaths": int(r["deaths"] or 0),
                  "lost": int(r["lost"] or 0), "guild": r["guild"] or "",
                  "guild_tag": guild_tag(r["guild"]) if r["guild"] else "",
                  "title": services.title_for(int(r["donated"] or 0), titles)[0]}
                 for r in data.gold_givers(db)]

        # THE COLUMN THAT USED TO BE HERE WAS THE TRADE TAX, PER PLAYER, AND IT
        # WAS MEASURED OUT OF THE BOARD.
        #
        # It was a second GROUP BY over every negative row in the ledger,
        # filtered to one reason. On a year of play - 500 players, two million
        # rows - it timed at 303ms of the board's 427ms, on the request thread,
        # on a panel that refreshes itself every thirty seconds. Ten people with
        # it open was a third of the server's time; fifty was more time than
        # there is.
        #
        # The tax total for the whole realm survives in by_reason below, which
        # is one cheap pass and was always going to be computed anyway. What is
        # gone is the per-player breakdown, and deaths took the column - a
        # counter on a row this query already reads, which costs nothing at any
        # scale.

        # LUSIONS GIVEN, FROM THEIR OWN LEDGER.
        #
        # A revive paid in lusions is a contribution too - the player gave the
        # kingdom something scarce - and counting only the gold one would have
        # made the board quietly reward whichever currency somebody happened to
        # hold.
        #
        # A SECOND COLUMN, NOT A SECOND TERM. They are not added together,
        # because there is no exchange rate between them and inventing one
        # would be the board deciding what a lusion is worth. Twenty lusions and
        # twenty gold are different sacrifices; it shows both and lets the
        # reader weigh them.
        by_user_lusions = data.lusion_givers(db)
        for entry in board:
            entry["lusions"] = by_user_lusions.pop(entry["username"], 0)

        # Anyone who has ONLY ever given lusions is not in the gold board at
        # all, and leaving them out would be a scoreboard that forgot the
        # players who paid in the rarer currency.
        for username, given in by_user_lusions.items():
            solo = data.solo(db, username)
            # `lost` IS 0 HERE AND IT IS NOT AN ASSUMPTION. These are players
            # with no negative gold row at all; losing gold on death writes
            # one, so anybody who ever has is in the gold board above rather
            # than down here.
            # No title either: a title is gold given to the crown, and these
            # have never given a coin of anything.
            board.append({"username": username, "contributed": 0, "lusions": given,
                          "deaths": int(solo["deaths"]) if solo else 0, "lost": 0,
                          "guild": (solo["guild"] or "") if solo else "",
                          "guild_tag": guild_tag(solo["guild"]) if solo and solo["guild"] else "",
                          "title": ""})

        board.sort(key=services.standing, reverse=True)

        # RANK IS COMPUTED HERE, ONCE, and sent with every row.
        #
        # The client used to derive it from the row's position in the list,
        # which is only the same thing while the list is complete and nothing
        # ties. It is neither: the list is capped at KINGDOM_BOARD_SIZE, and two
        # players who gave the same amount are joint, not consecutive.
        services.rank(board)

        total = sum(entry["contributed"] for entry in board)
        total_lusions = sum(entry["lusions"] for entry in board)

        # THE SAME BOARD, BY GUILD - FROM THE ROWS ALREADY IN MEMORY. Not a
        # query. The note above measured a second GROUP BY over the ledger at
        # 303ms of the board's 427ms and deleted it; adding one back for guilds
        # would be repeating that mistake with a different column. Every figure
        # is a sum over `board`, which is at most one entry per player who has
        # ever destroyed a coin.
        #
        # WHY A GUILD BOARD IS WORTH HAVING AT ALL. A guild needs a reason to
        # exist that is not a power bonus - a bonus is a balance problem and a
        # new thing to make server-authoritative. A shared number to chase costs
        # nothing to secure, because it is derived from a ledger that was
        # already being kept honest for other reasons.
        guild_board = services.guild_board(board)

        by_reason = data.reason_totals(db, "gold")
        lusions_by_reason = data.reason_totals(db, "lusions")

        # ALL GOLD THE TRADE TAX HAS DESTROYED, promoted out of by_reason to a
        # field of its own.
        #
        # It was already in the response - by_reason["kingdom_tax"] - but only
        # as one line of a dictionary the client renders as a run-on breakdown,
        # which is not the same as a figure anybody can find. The board's whole
        # claim is that the tax is a contribution rather than a deduction, and
        # that claim needs a number at the top of the screen, not a clause in
        # the middle of one.
        #
        # READ OFF by_reason RATHER THAN QUERIED AGAIN: one SELECT, one truth. A
        # second query would be a second chance to disagree.
        total_taxed = int(by_reason.get("kingdom_tax", 0))

        # AND THE SAME TRICK FOR WHAT THE DEAD LEFT BEHIND. Read off by_reason
        # for the same reason total_taxed is: one SELECT, one truth. A second
        # query would be a second chance to disagree with the breakdown printed
        # beside it.
        total_lost = int(by_reason.get("death", 0))

        # THE WHOLE LIST, NOT THE TOP SLICE. The endpoint cuts it to
        # KINGDOM_BOARD_SIZE for the wire, but it needs the full thing first to
        # find the caller's own row - a player at rank 4,000 still gets a line.
        return {
            "board": board,
            "total": total,
            "total_lusions": total_lusions,
            "total_taxed": total_taxed,
            "total_lost": total_lost,
            "total_deaths": data.total_deaths(db),
            "guilds": guild_board,
            "by_reason": by_reason,
            "lusions_by_reason": lusions_by_reason,
        }

    def patron_line(db, user_id):
        """{"donated", "title", "next_title", "next_at"} for this account, read
        fresh - the caller's own line and the gift's answer say the same."""
        donated = data.donated_by(db, user_id)
        title, next_title, next_at = services.title_for(donated, services.patron_titles(catalogue.CONSTANTS))
        return {"donated": donated, "title": title, "next_title": next_title, "next_at": next_at}

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
        # OPEN TO EVERY PLAYER, unlike /api/economy/supply in app.py, and the
        # difference is what each one reveals. Supply is the total money in the
        # world - an operational figure, and one that tells a gold seller
        # exactly how much a currency is worth. This is the opposite: it is the
        # gold that no longer exists, which is a scoreboard rather than an
        # asset.
        #
        # DERIVED, NEVER COUNTED. There is no kingdom_total column anywhere, on
        # purpose. A running counter is a second place the truth lives, and the
        # first time it disagrees with the ledger there is no way to tell which
        # one is lying. Summing the rows is slower and always right, and the row
        # count here grows one per taxed trade - that is an index, not a
        # problem.
        db = get_db()
        user_id = g.user["id"]

        now = int(time.time())
        shared = services.cached(cache, now)
        if shared is None:
            shared = board_shared(db)
            cache["at"] = now
            cache["payload"] = shared
        board = shared["board"]

        # THE CALLER'S OWN LINE, ALWAYS, AND NEVER FROM THE CACHE. The shared
        # figures are the same for everybody and can be a few seconds old; this
        # one is personal, so it is rebuilt on every request.
        #
        # A board that shows only the top ten tells everybody else they are not
        # on it; the whole point of the rule is that ordinary play adds to the
        # total, so ordinary play has to be able to see itself in it.
        #
        # AND WHICH GUILD THE CALLER IS IN, NEVER FROM THE CACHE EITHER. A
        # player who joined one a moment ago should see their guild's line
        # rather than somebody else's cached absence of it.
        deaths = data.deaths_of(db, user_id)
        my_guild = data.guild_of(db, user_id)
        mine = services.my_line(board, g.user["username"], deaths)
        # AND WHAT THE CALLER HAS GIVEN THE CROWN (0.74.0), with the title it
        # has bought and the one it is climbing to - fresh, like the rest of
        # this line, so a gift a moment ago shows here before the snapshot
        # catches up (the gift drops the snapshot anyway).
        mine.update(patron_line(db, user_id))

        return {
            "total": shared["total"],
            "total_lusions": shared["total_lusions"],
            "total_taxed": shared["total_taxed"],
            "total_lost": shared["total_lost"],
            # THE TOP SLICE, like `top` below it. The whole list is built so the
            # caller's own guild can be found in it; the wire gets the leaders.
            "guilds": shared["guilds"][:KINGDOM_BOARD_SIZE],
            "your_guild": next((dict(e) for e in shared["guilds"]
                                if e["name"] == my_guild), None),
            "total_deaths": shared["total_deaths"],
            "by_reason": shared["by_reason"],
            "lusions_by_reason": shared["lusions_by_reason"],
            "contributors": len(board),
            "top": board[:KINGDOM_BOARD_SIZE],
            "you": mine,
        }, 200

    def kingdom_donate():
        """
        Give gold to the crown, for a patron's title on the kingdom board
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
              required: [slot, amount]
              properties:
                slot:   {type: integer, example: 0}
                amount: {type: integer, example: 1000, description: "Gold from this character's purse; at least the catalogue's donation_minimum"}
        responses:
          200:
            description: "Given and burned. donated is the ACCOUNT's lifetime gifts; title, next_title and next_at the patron's standing (an empty next_title and 0 past the last); gold the purse after"
          400:
            description: Bad slot or amount, a gift under the smallest the crown takes, or more than the purse holds
          404:
            description: That slot is empty
          401:
            description: Missing, invalid or expired token
        """
        # A SINK THE PLAYER CHOOSES (0.74.0). The owner's third economy idea:
        # "things players choose to spend gold on: cosmetics on the kingdom
        # board". The gold is DESTROYED through gold_delta(), reason donation -
        # nobody holds it afterwards, the supply report counts it, and the
        # board, which sums every coin destroyed, counts it as gold given.
        # What it buys is a word beside a name (services.title_for()), which no
        # route can turn back into anything a player could sell.
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        raw = payload.get("amount")
        if isinstance(raw, bool) or raw is None:
            return bad_request("amount must be a positive integer")
        try:
            amount = int(raw)
            whole = float(raw) == amount
        except (TypeError, ValueError, OverflowError):
            return bad_request("amount must be a positive integer")
        if amount <= 0 or not whole:
            return bad_request("amount must be a positive integer")

        db = get_db()
        carried = data.purse(db, user_id, slot)
        if carried is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        smallest = services.donation_minimum(catalogue.CONSTANTS)
        if amount < smallest:
            return bad_request("The smallest gift the crown takes is %d gold." % smallest)
        if amount > carried:
            return bad_request("That is %d gold and you have %d." % (amount, carried))

        # ---- everything above this line is validation; below it commits --------
        remaining = gold_delta(db, user_id, slot, -amount, "donation",
                               "a gift of %d to the crown" % amount)
        db.commit()
        # THE BOARD'S SNAPSHOT GOES: the giver opens the board next, and a
        # title bought a moment ago must be on their row there, not fifteen
        # seconds later.
        cache_clear()

        answer = patron_line(db, user_id)
        answer["gold"] = int(remaining)
        return answer, 200

    app.add_url_rule("/api/economy/kingdom", endpoint="economy_kingdom",
                     view_func=require_auth(economy_kingdom), methods=["GET"])
    app.add_url_rule("/api/kingdom/donate", endpoint="kingdom_donate",
                     view_func=require_auth(kingdom_donate), methods=["POST"])

    return SimpleNamespace(cache_clear=cache_clear, board_shared=board_shared)
