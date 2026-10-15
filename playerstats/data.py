"""playerstats/data.py - every read the player stats make, and nothing else.

Plain SQL over tables app.py owns. Each function takes a sqlite3 connection
(rows by name) and returns plain Python - lists and dicts - so services.py
never sees a cursor. READ ONLY: nothing here writes, so the stats cannot
change the game they describe.

A table a database does not have yet answers with nothing rather than an error:
a server one release behind must still answer the owner.
"""
import sqlite3


def _rows(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def accounts(db):
    """Every account: id, username, role, created_at."""
    return [{"id": int(r["id"]), "username": str(r["username"]), "role": str(r["role"] or "player"),
             "created_at": int(r["created_at"] or 0)}
            for r in _rows(db, "SELECT id, username, role, created_at FROM users")]


def last_seen(db):
    """{user_id: the latest moment the account did anything the server keeps}:
    a save, a login (account_ips), a heartbeat (sessions), a kill (kill_tally)."""
    seen = {}
    for sql in ("SELECT user_id, MAX(updated_at) AS at FROM saves GROUP BY user_id",
                "SELECT user_id, MAX(last_seen) AS at FROM account_ips GROUP BY user_id",
                "SELECT user_id, MAX(last_seen_at) AS at FROM sessions GROUP BY user_id",
                "SELECT user_id, MAX(last_at) AS at FROM kill_tally GROUP BY user_id"):
        for r in _rows(db, sql):
            at = int(r["at"] or 0)
            uid = int(r["user_id"])
            if at > seen.get(uid, 0):
                seen[uid] = at
    return seen


def characters(db):
    """{user_id: [{slot, class_id, level, area, updated_at}]}."""
    out = {}
    for r in _rows(db, "SELECT user_id, slot, class_id, level, area, updated_at FROM saves"):
        out.setdefault(int(r["user_id"]), []).append({
            "slot": int(r["slot"]), "class_id": str(r["class_id"] or ""), "level": int(r["level"] or 1),
            "area": str(r["area"] or ""), "updated_at": int(r["updated_at"] or 0)})
    return out


def kill_totals(db):
    """{user_id: (kills ever, first kill at)} from kill_tally."""
    return {int(r["user_id"]): (int(r["kills"] or 0), int(r["first_at"] or 0))
            for r in _rows(db, "SELECT user_id, SUM(kills) AS kills, MIN(first_at) AS first_at"
                               " FROM kill_tally GROUP BY user_id")}


def kills_since(db, since):
    """[(user_id, enemy_id, at)] for every paid kill since `since` (kill_reports
    keeps two weeks)."""
    return [(int(r["user_id"]), str(r["enemy_id"]), int(r["at"]))
            for r in _rows(db, "SELECT user_id, enemy_id, at FROM kill_reports WHERE at >= ?", (int(since),))]


def logins_since(db, since):
    """[(username, at)] for every login that worked since `since`
    (login_attempts keeps two weeks)."""
    return [(str(r["username"]), int(r["at"]))
            for r in _rows(db, "SELECT username, at FROM login_attempts WHERE ok = 1 AND at >= ?", (int(since),))]
