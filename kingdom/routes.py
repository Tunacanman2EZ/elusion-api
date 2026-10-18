"""kingdom/routes.py - the edge: GET /api/economy/kingdom, as it was in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth   the token check (puts the account on g.user)
    get_db         the request's database connection
    guild_tag      a guild's name -> the short tag every list draws

and HANDS BACK cache_clear(), which drops the board's snapshot - app.py keeps
it as _kingdom_cache_clear(), which the suites call between moving gold and
reading the board.

THE ENDPOINT NAME IS app.py's - economy_kingdom - and routes.lock holds it.
"""
import time
from types import SimpleNamespace

from flask import g

from . import data, services
from .services import KINGDOM_BOARD_SIZE


def register(app, *, require_auth, get_db, guild_tag):

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
        board = [{"username": r["username"], "contributed": int(r["given"]),
                  "lusions": 0, "deaths": int(r["deaths"] or 0),
                  "lost": int(r["lost"] or 0), "guild": r["guild"] or "",
                  "guild_tag": guild_tag(r["guild"]) if r["guild"] else ""}
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
            board.append({"username": username, "contributed": 0, "lusions": given,
                          "deaths": int(solo["deaths"]) if solo else 0, "lost": 0,
                          "guild": (solo["guild"] or "") if solo else "",
                          "guild_tag": guild_tag(solo["guild"]) if solo and solo["guild"] else ""})

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

    app.add_url_rule("/api/economy/kingdom", endpoint="economy_kingdom",
                     view_func=require_auth(economy_kingdom), methods=["GET"])

    return SimpleNamespace(cache_clear=cache_clear, board_shared=board_shared)
