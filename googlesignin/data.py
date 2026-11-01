"""googlesignin/data.py - the SQL (game 0.101.0). Nothing here commits."""


def starts_since(db, ip, since):
    """How many codes this address has begun since `since`."""
    return int(db.execute("SELECT COUNT(*) FROM google_codes WHERE ip = ? AND created_at >= ?",
                          (str(ip), int(since))).fetchone()[0])


def prune(db, before):
    db.execute("DELETE FROM google_codes WHERE expires_at < ?", (int(before),))


def add_code(db, code_hash, install_hash, ip, purpose, user_id, now, expires_at):
    db.execute("INSERT INTO google_codes (code_hash, install_hash, ip, purpose, user_id, created_at, expires_at)"
               " VALUES (?, ?, ?, ?, ?, ?, ?)",
               (code_hash, install_hash, str(ip), purpose, user_id, int(now), int(expires_at)))


def code(db, code_hash):
    return db.execute("SELECT * FROM google_codes WHERE code_hash = ?", (code_hash,)).fetchone()


def finish_code(db, code_hash, state, sub="", email="", said="", user_id=None, keep_user=True):
    """A waiting code finished by the page - once: False when it was not
    waiting any more (a second token for one code changes nothing)."""
    if keep_user:
        cur = db.execute("UPDATE google_codes SET state = ?, sub = ?, email = ?, said = ?"
                         " WHERE code_hash = ? AND state = 'waiting'", (state, sub, email, said, code_hash))
    else:
        cur = db.execute("UPDATE google_codes SET state = ?, sub = ?, email = ?, said = ?, user_id = ?"
                         " WHERE code_hash = ? AND state = 'waiting'", (state, sub, email, said, user_id, code_hash))
    return cur.rowcount == 1


def use_code(db, code_hash):
    """A code collected - once: False when somebody collected it first."""
    cur = db.execute("UPDATE google_codes SET state = 'used' WHERE code_hash = ? AND state IN ('done', 'refused')",
                     (code_hash,))
    return cur.rowcount == 1


def link_of_sub(db, sub):
    return db.execute("SELECT * FROM google_accounts WHERE sub = ?", (str(sub),)).fetchone()


def link_of_user(db, user_id):
    return db.execute("SELECT * FROM google_accounts WHERE user_id = ?", (int(user_id),)).fetchone()


def add_link(db, user_id, sub, email, now, made_with_google):
    db.execute("INSERT INTO google_accounts (user_id, sub, email, linked_at, made_with_google)"
               " VALUES (?, ?, ?, ?, ?)", (int(user_id), str(sub), str(email), int(now),
                                            1 if made_with_google else 0))


def drop_link(db, user_id):
    db.execute("DELETE FROM google_accounts WHERE user_id = ?", (int(user_id),))


def user(db, user_id):
    return db.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()


def make_account(db, username, password_hash, email, now):
    """A new account made with Google: its id. Google's verified email is its
    recovery address from the start. Raises sqlite3.IntegrityError for a name
    taken."""
    cursor = db.execute(
        "INSERT INTO users (username, password_hash, created_at, email, email_verified) VALUES (?, ?, ?, ?, ?)",
        (username, password_hash, int(now), str(email), 1 if email else 0))
    return cursor.lastrowid


def give_recovery_email(db, user_id, email):
    """A linked account with no confirmed recovery address takes Google's
    verified one (it has proved it owns it)."""
    if email:
        db.execute("UPDATE users SET email = ?, email_verified = 1 WHERE id = ? AND email_verified = 0",
                   (str(email), int(user_id)))
