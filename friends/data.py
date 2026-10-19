"""friends/data.py - the SQL. The friends table is this package's (schema.py);
other readers there: the broadcast poll's "asks" and the ignore list."""


def link(db, a_id, b_id):
    """The row between two accounts, whichever way round it was made."""
    return db.execute(
        "SELECT * FROM friends"
        " WHERE (requester_id = ? AND addressee_id = ?)"
        "    OR (requester_id = ? AND addressee_id = ?)",
        (a_id, b_id, b_id, a_id),
    ).fetchone()


def counts(db, user_id):
    """(accepted friends, requests this account has outstanding)."""
    accepted = db.execute(
        "SELECT COUNT(*) AS n FROM friends"
        " WHERE state = 'accepted' AND (requester_id = ? OR addressee_id = ?)",
        (user_id, user_id),
    ).fetchone()["n"]
    pending_sent = db.execute(
        "SELECT COUNT(*) AS n FROM friends"
        " WHERE state = 'pending' AND requester_id = ?",
        (user_id,),
    ).fetchone()["n"]
    return int(accepted or 0), int(pending_sent or 0)


def rows_of(db, me):
    """Every row this account is on either end of."""
    return db.execute(
        "SELECT * FROM friends WHERE requester_id = ? OR addressee_id = ?",
        (me, me),
    ).fetchall()


def people(db, ids):
    """{id: row} for the accounts on the other end.

    NAMED COLUMNS, NOT SELECT *. Nothing that leaves the server should be
    able to carry a password hash out with it by accident."""
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {int(row["id"]): row for row in db.execute(
        "SELECT id, username, role, name_hue FROM users WHERE id IN (%s)" % marks,
        tuple(ids),
    ).fetchall()}


def pending_from(db, requester_id, addressee_id):
    """A request from `requester_id` to `addressee_id`, still waiting."""
    return db.execute(
        "SELECT * FROM friends"
        " WHERE requester_id = ? AND addressee_id = ? AND state = 'pending'",
        (requester_id, addressee_id),
    ).fetchone()


def accept(db, requester_id, addressee_id, now):
    db.execute(
        "UPDATE friends SET state = 'accepted', decided_at = ?"
        " WHERE requester_id = ? AND addressee_id = ?",
        (now, requester_id, addressee_id),
    )


def ask(db, requester_id, addressee_id, now):
    db.execute(
        "INSERT INTO friends (requester_id, addressee_id, state, created_at)"
        " VALUES (?, ?, 'pending', ?)",
        (requester_id, addressee_id, now),
    )


def drop_request(db, requester_id, addressee_id):
    db.execute(
        "DELETE FROM friends WHERE requester_id = ? AND addressee_id = ?",
        (requester_id, addressee_id),
    )


def drop_link(db, a_id, b_id):
    """BOTH DIRECTIONS IN ONE DELETE. The row could have been made either way
    round, and after this neither side should still be holding one."""
    db.execute(
        "DELETE FROM friends"
        " WHERE (requester_id = ? AND addressee_id = ?)"
        "    OR (requester_id = ? AND addressee_id = ?)",
        (a_id, b_id, b_id, a_id),
    )
