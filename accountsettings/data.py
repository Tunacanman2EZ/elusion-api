"""accountsettings/data.py - the SQL: the account's own row, its recovery
address and whether it is confirmed, and its name's hue. users is the core's
table (init_db()); these are the columns a player sets for themselves. No
commits: the routes make them."""


def recovery_address(db, user_id):
    """The account's address and whether it is confirmed, or None."""
    return db.execute(
        "SELECT email, email_verified FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def account(db, user_id):
    """The account's whole row (its password hash among it), or None."""
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def set_unverified(db, user_id, address):
    """A new address, unconfirmed until its code comes back."""
    db.execute("UPDATE users SET email = ?, email_verified = 0 WHERE id = ?",
               (address, user_id))


def mark_verified(db, user_id):
    db.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,))


def set_hue(db, user_id, hue):
    db.execute("UPDATE users SET name_hue = ? WHERE id = ?", (hue, user_id))
