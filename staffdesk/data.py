"""staffdesk/data.py - the SQL. Reads only, but for the one write a note is:
users, sessions, saves, accounts, login_attempts, kill_reports and
staff_actions are app.py's tables (init_db()), and every other area writes
them. staff_actions is written by app.py's log_staff_action() - a note
included, through it - and read here.

THE QUERY BUILDERS ARE ONE FUNCTION FOR THE ROUTE AND FOR THE TEST.
test_moderation.py asks SQLite for the plan of exactly the statements the
routes run, because a plan checked on a copy of a query proves the copy."""
from . import services
from .services import STAFF_PRIVATE_KINDS, STAFF_RECORD_KINDS

# How many are online right now, for the heading over the list. A constant so
# test_moderation.py can ask SQLite for the plan of this exact statement.
#
# THE + IN "+user_id" IS LOAD-BEARING. Plain COUNT(DISTINCT user_id) is a seek
# on idx_sessions_seen in SQLite 3.45 (the live server's) and a walk of EVERY
# session through idx_sessions_user in 3.50 (Python 3.14's, on the owner's PC,
# 11 Oct 2026) - the newer planner reads the user index in order to skip the
# DISTINCT's temporary table, and sessions last thirty days. The unary plus
# changes no value (user_id is an integer) and keeps the user index out of it,
# so both versions seek the last 45 seconds of heartbeats. Not INDEXED BY: that
# turns a missing index into an error on the staff desk.
STAFF_ONLINE_COUNT_SQL = (
    "SELECT COUNT(DISTINCT +user_id) FROM sessions"
    " WHERE last_seen_at >= ? AND expires_at > ?"
)


# -- the list ----------------------------------------------------------------------

def users_query(q, show, after, limit, now, *, rank_sql, default_level, online_window):
    """
    The staff list's SQL: (page_sql, page_params, count_sql, count_params).

    `rank_sql` is app.py's role_for() as SQL, (alias) -> (sql, params), and
    `default_level` the plain player's place on the ladder.
    """
    where = []
    params = []

    if q:
        # A SUBSTRING, NOT A PREFIX. A mod has "sniper" off a chat log and the
        # name is xXsniperXx. That makes it a scan of the name index rather
        # than a seek - on the users table, for a staff-only route, typed rather
        # than polled - which is the right trade.
        where.append("u.username LIKE ? ESCAPE '\\'")
        params.append(services.like_contains(q))

    if show == "online":
        # The same test as the "online" flag on each row, so a row the filter
        # admits never says it is offline.
        where.append("u.id IN (SELECT user_id FROM sessions"
                     " WHERE last_seen_at >= ? AND expires_at > ?)")
        params += [now - online_window, now]
    elif show == "banned":
        # ban_state()'s rule - flagged, and permanent or not yet expired - so a
        # served sentence drops off this filter the moment it is up.
        where.append("u.is_banned = 1 AND (u.ban_expires_at IS NULL OR u.ban_expires_at > ?)")
        params.append(now)
    elif show == "staff":
        level, level_params = rank_sql("u")
        where.append("%s > %d" % (level, default_level))
        params += level_params

    # The count is the whole match, not what is left after the cursor.
    count_sql = "SELECT COUNT(*) FROM users u"
    if where:
        count_sql += " WHERE " + " AND ".join(where)
    count_params = list(params)

    if after:
        # COLLATE NOCASE from the column, which is also the order of the index
        # the ORDER BY walks, so "after" means after in the order shown.
        where.append("u.username > ?")
        params.append(after)

    page_sql = "SELECT * FROM users u"
    if where:
        page_sql += " WHERE " + " AND ".join(where)
    # One row past the page: whether it exists is what "more" means, with no
    # second query to ask.
    page_sql += " ORDER BY u.username LIMIT ?"
    params.append(limit + 1)

    return page_sql, params, count_sql, count_params


def last_seen(db, ids, now):
    """{user_id: latest heartbeat} over the unexpired sessions of these
    accounts - one query for the page. An expired row is dead whatever its
    last heartbeat says."""
    seen = {}
    if not ids:
        return seen
    marks = ",".join("?" * len(ids))
    for r in db.execute(
        "SELECT user_id, MAX(last_seen_at) AS last_seen FROM sessions"
        " WHERE expires_at > ? AND user_id IN (%s) GROUP BY user_id" % marks,
        [now] + list(ids),
    ).fetchall():
        seen[int(r["user_id"])] = int(r["last_seen"] or 0)
    return seen


def records(db, ids):
    """{user_id: {kind: how many}} of the STAFF_RECORD_KINDS against these
    accounts - one query for the page."""
    record = {}
    if not ids:
        return record
    marks = ",".join("?" * len(ids))
    kind_marks = ",".join("?" * len(STAFF_RECORD_KINDS))
    for r in db.execute(
        "SELECT target_id, action, COUNT(*) AS n FROM staff_actions"
        " WHERE target_id IN (%s) AND action IN (%s)"
        " GROUP BY target_id, action" % (marks, kind_marks),
        list(ids) + list(STAFF_RECORD_KINDS),
    ).fetchall():
        record.setdefault(int(r["target_id"]), {})[r["action"]] = int(r["n"])
    return record


def count(db, sql, params):
    return int(db.execute(sql, params).fetchone()[0])


def online_now(db, now, online_window):
    return count(db, STAFF_ONLINE_COUNT_SQL, (now - online_window, now))


# -- the log -----------------------------------------------------------------------

def subject_sql(db, name, id_column, name_column):
    """
    How the log finds entries about, or by, `name`: (sql, params).

    BY ID WHEN THE ACCOUNT EXISTS. The id is what the entry was written
    against, so a player cannot be confused with a guild that shares their
    name - guild actions name the guild and carry no id.

    BY NAME WHEN IT DOES NOT. Names are stored beside ids precisely so the log
    still answers after an account is gone, and "server" and "everyone" never
    had an account to begin with.
    """
    row = db.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()
    if row is not None:
        return "sa.%s = ?" % id_column, [int(row["id"])]
    return "sa.%s = ? COLLATE NOCASE" % name_column, [name]


def log_query(db, viewer_level, player, staff, kind, before, limit, *, rank_sql, default_level, groups):
    """
    The moderation log's SQL: (page_sql, page_params, summary_sql, summary_params).

    The summary is the same WHERE without the kind and the cursor - how many of
    each thing the whole record holds, not how many are on this page.
    """
    level_sql, level_params = rank_sql("t")
    kind_marks = ",".join("?" * len(STAFF_PRIVATE_KINDS))

    # THE PRIVACY RULE, IN THE QUERY. Filtering private rows out in Python
    # after the LIMIT would shorten pages at random and let "more" be wrong;
    # here a page is always a full page of what this viewer may read.
    where = ["(sa.action NOT IN (%s) OR COALESCE(%s, %d) < ?)" % (
        kind_marks, level_sql, default_level)]
    params = list(STAFF_PRIVATE_KINDS) + level_params + [viewer_level]

    if player:
        clause, extra = subject_sql(db, player, "target_id", "target_name")
        where.append(clause)
        params += extra
    if staff:
        clause, extra = subject_sql(db, staff, "actor_id", "actor_name")
        where.append(clause)
        params += extra

    base = "FROM staff_actions sa LEFT JOIN users t ON t.id = sa.target_id WHERE "
    summary_sql = ("SELECT sa.action AS action, COUNT(*) AS n " + base
                   + " AND ".join(where) + " GROUP BY sa.action")
    summary_params = list(params)

    if kind in groups:
        group = groups[kind]
        where.append("sa.action IN (%s)" % ",".join("?" * len(group)))
        params += list(group)
    elif kind:
        where.append("sa.action = ?")
        params.append(kind)
    if before:
        where.append("sa.id < ?")
        params.append(before)

    page_sql = ("SELECT sa.id, sa.created_at, sa.actor_name, sa.action,"
                " sa.target_name, sa.detail " + base + " AND ".join(where)
                + " ORDER BY sa.id DESC LIMIT ?")
    params.append(limit + 1)

    return page_sql, params, summary_sql, summary_params


def last_entry_id(db):
    """The id of the staff_actions row this connection just wrote."""
    return int(db.execute("SELECT last_insert_rowid()").fetchone()[0])


# -- one account -------------------------------------------------------------------

def account(db, username):
    return db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def characters(db, user_id):
    # AREA, AND NOTHING FINER, because that is all `saves` holds. An earlier
    # pass added "x, y" here for the owner panel's "go to them" and broke this
    # route outright - there are no such columns. The x/y that do exist are on
    # `pending_teleports`, which is where a teleport is going, not where a
    # player is. /api/players/nearby says the same thing in words: "There is
    # no position on the server and no heartbeat carrying one."
    return db.execute(
        "SELECT slot, class_id, name, level, area, updated_at FROM saves"
        " WHERE user_id = ? ORDER BY slot", (user_id,)
    ).fetchall()


def login_attempts(db, username, limit):
    return db.execute(
        "SELECT ok, reason, ip, at FROM login_attempts WHERE username = ?"
        " ORDER BY at DESC LIMIT ?", (username, limit)
    ).fetchall()


def login_totals(db, username):
    return db.execute(
        "SELECT COUNT(*) AS n,"
        "       SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS good,"
        "       COUNT(DISTINCT ip) AS addresses"
        " FROM login_attempts WHERE username = ?", (username,)
    ).fetchone()


def kills(db, user_id):
    return db.execute(
        "SELECT enemy_id, COUNT(*) AS n, MAX(at) AS latest, MIN(at) AS earliest,"
        "       SUM(xp) AS xp"
        " FROM kill_reports WHERE user_id = ? GROUP BY enemy_id ORDER BY n DESC",
        (user_id,)
    ).fetchall()


def staff_history(db, user_id, private_too, limit):
    """What staff have done to the account, newest first - the private kinds
    only when `private_too`. ORDER BY id, not created_at: two entries in the
    same second are still in the order they were written."""
    private_marks = ",".join("?" * len(STAFF_PRIVATE_KINDS))
    return db.execute(
        "SELECT action, actor_name, detail, created_at FROM staff_actions"
        " WHERE target_id = ? AND (? OR action NOT IN (%s))"
        " ORDER BY id DESC LIMIT %d" % (private_marks, int(limit)),
        [user_id, 1 if private_too else 0] + list(STAFF_PRIVATE_KINDS),
    ).fetchall()


def lusions(db, user_id):
    row = db.execute("SELECT lusions FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["lusions"]) if row else 0


def live_sessions(db, user_id, now, limit):
    """The unexpired sessions' expiries, latest first. NO TOKENS."""
    return db.execute(
        "SELECT expires_at FROM sessions"
        " WHERE user_id = ? AND expires_at > ? ORDER BY expires_at DESC LIMIT ?",
        (user_id, now, limit),
    ).fetchall()
