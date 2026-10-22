"""economyrules/data.py - the SQL: the owner's table of economy numbers
(economy_rules) and the log of every change to it (economy_rule_changes), both
economyrules/schema.py's. Nothing here commits: a change, its log line and the
staff log's line are one transaction, the route's."""


def database_file(db):
    """The file this connection's main database is - what the cache is keyed
    by. PRAGMA database_list reads no table and touches no disk."""
    for row in db.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            return str(row[2] or ":memory:")
    return ":memory:"


def numbers(db):
    """{key: value} - every number the owner has set. A handful of rows."""
    return {str(row[0]): row[1] for row in db.execute("SELECT key, value FROM economy_rules").fetchall()}


def rules(db):
    """{key: {"value", "updated_at", "updated_by"}} - every row, for the
    owner's panel."""
    return {str(row[0]): {"value": row[1], "updated_at": row[2], "updated_by": row[3]}
            for row in db.execute(
                "SELECT key, value, updated_at, updated_by FROM economy_rules").fetchall()}


def set_rule(db, key, value, at, by):
    """The owner's number for `key`, replacing any before it."""
    db.execute(
        "INSERT INTO economy_rules (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at,"
        " updated_by = excluded.updated_by",
        (key, float(value), int(at), str(by)))


def clear_rule(db, key):
    """Back to the default: the owner's row goes."""
    db.execute("DELETE FROM economy_rules WHERE key = ?", (key,))


def log_change(db, key, before, after, at, by_id, by_name):
    """One line of the change log. `before` and `after` are None for "the
    default", so the log says the difference between setting a rule to the
    number it already defaults to and letting it go back to the game's own."""
    db.execute(
        "INSERT INTO economy_rule_changes (at, key, old_value, new_value, by_id, by_name)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (int(at), key, None if before is None else float(before),
         None if after is None else float(after), by_id, str(by_name)))


def changes(db, limit):
    """The newest `limit` changes, newest first. The table only grows when the
    owner presses Save, so this is a short read off its index for ever."""
    return [{"at": int(row[0]), "key": str(row[1]), "old_value": row[2], "new_value": row[3],
             "by": str(row[4])}
            for row in db.execute(
                "SELECT at, key, old_value, new_value, by_name FROM economy_rule_changes"
                " ORDER BY at DESC, id DESC LIMIT ?", (int(limit),)).fetchall()]
