"""economywatch/routes.py - the edge: the owner's two instruments over the
economy (0.74.0).

    GET  /api/owner/economy/flow?days=7   where gold came from and went, by
                                          UTC day and by ledger reason, and
                                          what the world holds now
    GET  /api/owner/economy/canary        the last hourly check's report
    POST /api/owner/economy/canary        check now, read-only, and answer

register() is handed app.py's own pieces - it never imports app.py:

    require_auth   the token check (puts the account on g.user)
    require_owner  the owner or a co-owner, the bare 404 to anybody else
    no_write_lock  marks a POST that must not hold the write lock: Check now
                   writes nothing, and reads the whole ledger first
    get_db         the request's database connection
    bad_request    a message -> app.py's 400
    query_int      (name, default, low, high) -> the query's int, or None
    read_canary    () -> the last status file canary.py wrote, or None
    run_canary     () -> a fresh report from canary.py's checks, on a
                   read-only connection of their own

ALL THREE READ, NONE WRITES, and so none asks for the password again: the
owner and a co-owner alike, the GM panel's Economy tab. Check now opens the
database mode=ro through canary.py itself - the same checks the timer runs,
on the same discipline - rather than through the request's connection, which
can write.
"""
import threading
import time
from types import SimpleNamespace

from . import data, services


def register(app, *, require_auth, require_owner, no_write_lock, get_db, bad_request, query_int, read_canary,
             run_canary):
    """Add the three routes, and hand back cache_clear() - for the suites,
    which write ledger rows dated in the past and then read the flow."""

    # THE SETTLED DAYS, read once each: {currency: {UTC day: its rows}}. Per
    # register(), which in the server is once a worker. At most DAYS_MOST
    # days of a few dozen reasons each - a few thousand tuples.
    cache = {"gold": {}, "lusions": {}}
    lock = threading.Lock()

    def cache_clear():
        with lock:
            for held in cache.values():
                held.clear()

    def rows_for(db, currency, first, days, now):
        """Every (day, reason, ...) row of the window: the settled days from
        the cache (read now, once, if they are not in it yet), the rest - today
        - from the ledger."""
        settled = services.last_settled_day(now)
        wanted = range(first, first + days)
        with lock:
            held = cache[currency]
            missing = [day for day in wanted if day <= settled and day not in held]
            if missing:
                low, high = min(missing), max(missing)
                read = {}
                for row in data.flow_rows(db, currency, low * services.DAY, (high + 1) * services.DAY):
                    read.setdefault(row[0], []).append(row)
                for day in range(low, high + 1):
                    held[day] = read.get(day, [])
            # Nothing older than the longest window is ever asked for again.
            for day in [day for day in held if day < settled - services.DAYS_MOST]:
                del held[day]
            rows = [row for day in wanted if day in held for row in held[day]]
        unsettled = max(first, settled + 1)
        if unsettled < first + days:
            rows.extend(data.flow_rows(db, currency, unsettled * services.DAY, (first + days) * services.DAY))
        return rows

    def owner(view):
        return require_auth(require_owner(view))

    def owner_economy_flow():
        """
        Where gold and lusions came from and went - minted and burned per UTC day and per ledger reason, totals per reason, the net per day, and what the world holds now (owner and co-owner)
        ---
        tags:
          - Economy
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: query
            name: days
            type: integer
            required: false
            description: "how many UTC days, today the last of them: 1-90, 7 when left out"
        responses:
          200:
            description: "days, from, now, gold and lusions - each with days (oldest first), reasons (busiest first), minted, burned, net and in_world"
          400:
            description: days is not a whole number from 1 to 90
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner or a co-owner
        """
        # The owner, of the second economy idea: "A daily report of where gold
        # comes from and where it goes, by source." The ledger already says
        # both for every coin - gold_delta() writes a reason with each - so
        # this is a read of it, never a counter kept beside it.
        days = query_int("days", services.DAYS_DEFAULT, 1, services.DAYS_MOST)
        if days is None:
            return bad_request("days must be a whole number from 1 to %d" % services.DAYS_MOST)
        now = int(time.time())
        first = services.first_day(now, days)
        since = first * services.DAY
        db = get_db()
        gold = services.flow(rows_for(db, "gold", first, days, now), first, days)
        carried, banked = data.gold_in_world(db)
        gold["in_world"] = {"carried": carried, "banked": banked, "total": carried + banked}
        lusions = services.flow(rows_for(db, "lusions", first, days, now), first, days)
        lusions["in_world"] = {"total": data.lusions_in_world(db)}
        return {"days": days, "from": since, "now": now, "gold": gold, "lusions": lusions}, 200

    def owner_canary_status():
        """
        The last hourly canary check - when, whether every number added up, what broke and what the mail did - or checked_at null when it has never run (owner and co-owner)
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
            description: "checked_at (null if never), ok, failures, checks, age_seconds, stale - older than an hourly timer should leave it"
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner or a co-owner
        """
        return services.canary_answer(read_canary(), int(time.time())), 200

    def owner_canary_check():
        """
        Run the canary's checks now, on a read-only connection, and answer them - the status file is the timer's and is left as it was (owner and co-owner)
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
            description: "checked_at, ok, failures, checks - a database that cannot be read is a failure too"
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner or a co-owner
        """
        # THE STATUS FILE IS NOT WRITTEN. It is the timer's record - when the
        # hourly check last ran, and when it last mailed - and a check from the
        # panel is the owner already looking, so it neither mails nor moves
        # what the timer knows.
        return run_canary(), 200

    app.add_url_rule("/api/owner/economy/flow", endpoint="owner_economy_flow",
                     view_func=owner(owner_economy_flow), methods=["GET"])
    app.add_url_rule("/api/owner/economy/canary", endpoint="owner_canary_status",
                     view_func=owner(owner_canary_status), methods=["GET"])
    # NOT UNDER THE WRITE LOCK. A POST takes it at the request's first read
    # (app.py, THE WRITE LOCK), and Check now's sums over the whole ledger -
    # on a connection of their own - would hold every kill and every purchase
    # waiting for as long as they take, to write nothing at all.
    app.add_url_rule("/api/owner/economy/canary", endpoint="owner_canary_check",
                     view_func=no_write_lock(owner(owner_canary_check)), methods=["POST"])

    return SimpleNamespace(cache_clear=cache_clear)
