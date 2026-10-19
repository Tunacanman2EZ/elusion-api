"""auth/data.py - the SQL: accounts made and found, a password set, a login's
misses cleared, sessions ended, the step-up's misses, the recovery requests
counted, a pending code read, and the trusted computers. The tables are app.py's
(init_db()): users, sessions, login_attempts, auth_codes and trusted_devices.
A session is MADE by app.py's issue_token() and looked up by its token check,
and every login attempt is written by app.py's _record_login_attempt() - what
every request passes through stays there. No commits: the routes make them."""


# -- accounts ----------------------------------------------------------------------

def create_account(db, username, password_hash, now):
    """A new account; its id. Raises sqlite3.IntegrityError for a name taken."""
    cursor = db.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, password_hash, now),
    )
    return cursor.lastrowid


def relabel_signup_login(db, ip, username, since):
    """The new account's own 'no-such-user' misses from this address, since
    `since`, no longer count as a spray (see the register route)."""
    db.execute(
        "UPDATE login_attempts SET reason = 'signup-first-login'"
        " WHERE ip = ? AND lower(username) = lower(?) AND reason = 'no-such-user' AND at >= ?",
        (ip, username, since),
    )


def account_named(db, username):
    """The whole users row by name (NOCASE), or None."""
    return db.execute(
        "SELECT * FROM users WHERE username = ?",
        (username,),
    ).fetchone()


def account(db, user_id):
    """The whole users row by id, or None."""
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def verified_account(db, address):
    """The account whose CONFIRMED recovery address this is, or None."""
    return db.execute(
        "SELECT * FROM users WHERE email = ? AND email_verified = 1", (address,)
    ).fetchone()


def password_of(db, user_id):
    """The account's password hash alone, or None."""
    return db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()


def clear_misses(db, user_id):
    """A login that got its token clears the failed-login streak."""
    db.execute(
        "UPDATE users SET failed_logins = 0, lockout_until = 0 WHERE id = ?",
        (user_id,),
    )


def set_password(db, user_id, password_hash):
    """A new password, the streak and any lockout cleared with it."""
    db.execute(
        "UPDATE users SET password_hash = ?, failed_logins = 0, lockout_until = 0 WHERE id = ?",
        (password_hash, user_id),
    )


# -- sessions ----------------------------------------------------------------------

def end_session(db, token_hash):
    """The one session this token is."""
    db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def end_sessions(db, user_id):
    """Every session on the account; how many there were."""
    cursor = db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return cursor.rowcount


def miss_confirm(db, token_hash):
    """One more wrong password typed again on this session."""
    db.execute("UPDATE sessions SET confirm_misses = confirm_misses + 1 WHERE token_hash = ?", (token_hash,))


def confirm_misses(db, token_hash):
    """The session's row with its confirm_misses, or None."""
    return db.execute("SELECT confirm_misses FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()


def confirmed(db, token_hash, now):
    """The password typed again, now: the step-up opens and the misses clear."""
    db.execute("UPDATE sessions SET confirmed_at = ?, confirm_misses = 0 WHERE token_hash = ?", (now, token_hash))


# -- codes -------------------------------------------------------------------------

def recovery_requests(db, ip, since):
    """How many recovery requests came from this address since `since`."""
    return int(db.execute(
        "SELECT COUNT(*) AS n FROM login_attempts "
        "WHERE ip = ? AND reason = 'recover-request' AND at > ?",
        (ip, since),
    ).fetchone()["n"] or 0)


def reset_code(db, user_id):
    """When the account's reset code was made, or None for none."""
    return db.execute(
        "SELECT created_at FROM auth_codes WHERE user_id = ? AND purpose = 'reset'",
        (user_id,),
    ).fetchone()


def pending_code(db, user_id, purpose):
    """When the account's code for `purpose` was made and runs out, or None."""
    return db.execute(
        "SELECT created_at, expires_at FROM auth_codes WHERE user_id = ? AND purpose = ?",
        (user_id, purpose),
    ).fetchone()


# -- trusted computers (TRUSTED DEVICES, services.py) --------------------------------

def device(db, token_hash, user_id):
    """The rank this computer was trusted at and until when, or None."""
    return db.execute(
        "SELECT role, expires_at FROM trusted_devices WHERE token_hash = ? AND user_id = ?",
        (token_hash, user_id),
    ).fetchone()


def device_used(db, token_hash, user_id, now):
    db.execute("UPDATE trusted_devices SET last_used_at = ? WHERE token_hash = ? AND user_id = ?",
               (now, token_hash, user_id))


def add_device(db, token_hash, user_id, role, now, expires_at, kept):
    """A computer that just proved itself, and only the newest `kept` kept."""
    db.execute(
        "INSERT INTO trusted_devices (token_hash, user_id, role, created_at, last_used_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (token_hash, user_id, role, now, now,
         expires_at),
    )
    db.execute(
        "DELETE FROM trusted_devices WHERE user_id = ? AND token_hash NOT IN ("
        " SELECT token_hash FROM trusted_devices WHERE user_id = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?)",
        (user_id, user_id, kept),
    )


def forget_devices(db, user_id):
    db.execute("DELETE FROM trusted_devices WHERE user_id = ?", (user_id,))
