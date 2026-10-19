"""kingdom/data.py - the SQL. Reads only: gold_ledger, lusion_ledger and users
are app.py's (init_db()), the guild tables guilds/'s (guilds/schema.py), and
the board writes nothing - it is
DERIVED, NEVER COUNTED (see routes.economy_kingdom)."""


def gold_givers(db):
    """One row per account that ever destroyed a coin, most given first:
    username, deaths, guild, given, lost.

    CONTRIBUTION IS EVERY GOLD DESTROYED, not only the trade tax, and that is
    a deliberate reading of "what have I given the kingdom". A player who
    never trades but keeps the potion shop in business has still taken gold
    out of the world, and a board that ignored them would say their spending
    did not count. The breakdown (reason_totals) keeps the trade tax visible
    on its own so nothing is hidden by the aggregate.

    users.deaths COMES ALONG FOR NOTHING. The join is already here to turn a
    user_id into a name, so the counter is one more column off a row that was
    being read anyway - no second query, no second pass over the ledger.

    It is a bare column under a GROUP BY, which is legal in SQLite and
    deterministic here: the grouping key is gold_ledger.user_id and the join
    is on users.id, so every row in a group carries the same users row.

    `lost` IS A CASE IN THE SAME AGGREGATE, NOT A SECOND QUERY, and that is
    the whole reason it is affordable. A per-player figure filtered to one
    reason was measured at 303ms of the board's 427ms, because it was a
    SECOND GROUP BY over every negative row in the ledger (see the note in
    routes.py). This is one more SUM over rows this scan already reads - the
    same trade `deaths` makes one line up, and it costs nothing at any size.

    WHAT IT IS FOR. Contribution counts every gold destroyed, and death now
    destroys gold, so dying climbs the board. That follows from the rule and
    it is kept - but a ranking whose top entry is mostly deaths is saying
    something different from one whose top entry is mostly spending, and the
    board should be able to tell the reader which it is looking at."""
    return db.execute(
        """
        SELECT users.username AS username,
               users.deaths   AS deaths,
               guilds.name    AS guild,
               -SUM(gold_ledger.delta) AS given,
               -SUM(CASE WHEN gold_ledger.reason = 'death'
                         THEN gold_ledger.delta ELSE 0 END) AS lost
          FROM gold_ledger
          JOIN users ON users.id = gold_ledger.user_id
          -- THE GUILD COMES ALONG FOR NOTHING, exactly as users.deaths does.
          -- Two LEFT JOINs on indexed keys onto a row this scan already reads.
          -- It is deliberately NOT a second pass over the ledger: the note
          -- in routes.py measured that shape at 303ms of the board's 427ms,
          -- and a guild ranking is built from these rows in Python instead.
          LEFT JOIN guild_members ON guild_members.user_id = users.id
          LEFT JOIN guilds ON guilds.id = guild_members.guild_id
         WHERE gold_ledger.delta < 0
      GROUP BY gold_ledger.user_id
      ORDER BY given DESC
        """
    ).fetchall()


def lusion_givers(db):
    """{username: lusions given}, from their own ledger."""
    given = {}
    for row in db.execute(
        "SELECT users.username AS username, -SUM(lusion_ledger.delta) AS given"
        "  FROM lusion_ledger"
        "  JOIN users ON users.id = lusion_ledger.user_id"
        " WHERE lusion_ledger.delta < 0"
        " GROUP BY lusion_ledger.user_id"
    ).fetchall():
        given[row["username"]] = int(row["given"])
    return given


def solo(db, username):
    """The deaths and the guild of somebody who has only ever given lusions -
    they are not in gold_givers(), so it takes one small lookup. Bounded by how
    many players have ONLY ever paid in lusions, which is a handful, not a
    scan."""
    return db.execute(
        "SELECT u.deaths AS deaths, gl.name AS guild FROM users u"
        "  LEFT JOIN guild_members m ON m.user_id = u.id"
        "  LEFT JOIN guilds gl ON gl.id = m.guild_id"
        " WHERE u.username = ?", (username,)).fetchone()


_BY_REASON = {
    "gold": "SELECT reason, -SUM(delta) AS given FROM gold_ledger"
            " WHERE delta < 0 GROUP BY reason ORDER BY given DESC",
    "lusions": "SELECT reason, -SUM(delta) AS given FROM lusion_ledger"
               " WHERE delta < 0 GROUP BY reason ORDER BY given DESC",
}


def reason_totals(db, currency):
    """{reason: destroyed}, most first, for "gold" or "lusions"."""
    totals = {}
    for row in db.execute(_BY_REASON[currency]).fetchall():
        totals[row["reason"]] = int(row["given"])
    return totals


def total_deaths(db):
    """EVERY DEATH ON THE SERVER, which is one sum over one integer column and
    does not touch the ledger at all."""
    return int(db.execute(
        "SELECT COALESCE(SUM(deaths), 0) FROM users").fetchone()[0] or 0)


def deaths_of(db, user_id):
    row = db.execute("SELECT deaths FROM users WHERE id = ?", (user_id,)).fetchone()
    return int(row["deaths"]) if row else 0


def guild_of(db, user_id):
    """The name of the caller's guild, or ""."""
    row = db.execute(
        "SELECT gl.name FROM guild_members m JOIN guilds gl ON gl.id = m.guild_id"
        " WHERE m.user_id = ?", (user_id,)).fetchone()
    return row["name"] if row else ""
