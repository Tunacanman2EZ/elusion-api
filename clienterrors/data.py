"""clienterrors/data.py - the SQL for client_errors. The write is one account's
rows only; the read adds up every account's for the owner, and is the reason
this file reads past one user_id (it answers counts, never a name).
"""


def rows_today(db, user_id, day):
    """How many distinct errors this account has kept today."""
    return int(db.execute("SELECT COUNT(*) FROM client_errors WHERE user_id = ? AND day = ?",
                          (user_id, day)).fetchone()[0])


def add(db, user_id, day, version, build, area, where_at, what, times, now, times_most, allow_new):
    """Count one error for this account today: the row's times go up (held to
    times_most) and its area and last_at move on; a new row only when
    allow_new. Answers "seen" (an existing row counted), "new" (a row
    added) or "" (not counted: new, and no room left today)."""
    cur = db.execute(
        "UPDATE client_errors SET times = MIN(times + ?, ?), last_at = ?, area = ?, build = ? "
        "WHERE day = ? AND user_id = ? AND version = ? AND where_at = ? AND what = ?",
        (times, times_most, now, area, build, day, user_id, version, where_at, what))
    if cur.rowcount:
        return "seen"
    if not allow_new:
        return ""
    db.execute(
        "INSERT INTO client_errors (day, user_id, version, build, where_at, what, area, times, first_at, last_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (day, user_id, version, build, where_at, what, area, min(times, times_most), now, now))
    return "new"


def prune(db, before_day):
    """Rows from before before_day go."""
    db.execute("DELETE FROM client_errors WHERE day < ?", (before_day,))


def since(db, at):
    """Every account's rows last seen at or after `at`, for the owner's list -
    no names: user_id only to count players."""
    return [dict(row) for row in db.execute(
        "SELECT user_id, version, where_at, what, area, times, first_at, last_at "
        "FROM client_errors WHERE last_at >= ?", (int(at),)).fetchall()]
