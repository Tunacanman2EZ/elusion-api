from flask import Flask, request, g
from flasgger import Swagger
from werkzeug.security import generate_password_hash, check_password_hash

import gamedata
import sqlite3
import secrets
import time
import os
import re
from functools import wraps

app = Flask(__name__)

app.config["SWAGGER"] = {
    "title": "Elusion RPG API",
    "uiversion": 3,
    "description": "Backend for the Elusion RPG Godot client.",
    "version": "0.1.0",
}
swagger = Swagger(app)


# =============================================================================
# CONFIG
# =============================================================================

DB_PATH = os.environ.get("ELUSION_DB", os.path.join(os.path.dirname(__file__), "elusion.db"))

# how long a login token stays valid. games shouldn't log people out
# constantly, so this is generous - 30 days in seconds.
TOKEN_TTL = 60 * 60 * 24 * 30

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,20}$")
MIN_PASSWORD_LENGTH = 8

REQUIRED_FIELDS = ["username", "password"]

# The account that owns this server, named in the environment rather than
# stored in the database.
#
# That is the whole point. If "owner" were the top value of a role column, then
# whatever endpoint sets roles could set it, and the first bug in that endpoint
# would be a total compromise. It would also survive a stolen database backup.
# An environment variable cannot be written by any request, does not appear in
# elusion.db, and makes "everyone except me" a comparison that no code path can
# make false.
#
# Set it before starting the server:
#     PowerShell   $env:ELUSION_OWNER = "yourname"
#     cmd          set ELUSION_OWNER=yourname
#     bash         export ELUSION_OWNER=yourname
#
# Unset means no owner, and every owner check fails closed - a server with no
# configured owner has no owner, rather than everyone being one.
OWNER_USERNAME = os.environ.get("ELUSION_OWNER", "").strip()


# =============================================================================
# DATABASE
# =============================================================================

def get_db():
    """One connection per request, closed automatically in teardown."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        # enforce foreign keys - off by default in sqlite
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist. Safe to call on every boot."""
    db = sqlite3.connect(DB_PATH)

    # Before the schema block, not after - see the docstring.
    _migrate_rename_admin_actions(db)

    db.executescript(
        """
        -- `role` is the rank system. It replaced a boolean called is_admin,
        -- which _migrate_role_column() carries across and
        -- _migrate_drop_is_admin() then removes. There is no admin rank and
        -- there never was one; that column was a yes/no flag from before
        -- ranks existed.
        --
        -- 'owner' is NOT a legal value here. The CHECK says so, but the CHECK
        -- only reaches databases created from this statement - a database
        -- migrated by _migrate_role_column() gets the column via ALTER
        -- and therefore no constraint at all.
        --
        -- So the CHECK is defence in depth, not the mechanism. role_for() is
        -- the mechanism: it reads anything outside ROLES, and anything equal
        -- to 'owner', as DEFAULT_ROLE. A column holding 'owner' grants
        -- nothing on either shape of database.
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT    NOT NULL,
            role          TEXT    NOT NULL DEFAULT 'player'
                          CHECK (role IN ('player', 'mod', 'dev')),
            is_banned      INTEGER NOT NULL DEFAULT 0,
            -- NULL with is_banned = 1 means permanent. See the migration.
            ban_expires_at INTEGER,
            ban_reason     TEXT    NOT NULL DEFAULT '',
            banned_by      TEXT    NOT NULL DEFAULT '',
            banned_at      INTEGER NOT NULL DEFAULT 0,
            created_at    INTEGER NOT NULL
        );

        -- Every moderation action, forever. Names are stored ALONGSIDE ids
        -- because the log has to still make sense after an account is gone -
        -- an audit trail of orphaned integers answers nothing.
        --
        -- This exists from the first day rather than after the first argument.
        -- Once there are mods it is how THEIR decisions get reviewed, which is
        -- what makes delegating safe rather than merely convenient.
        -- "staff" is not a rank. It is the set of ranks above player -
        -- mod, dev and owner - because all three can act here and the table
        -- needs one word for them.
        CREATE TABLE IF NOT EXISTS staff_actions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id    INTEGER,
            actor_name  TEXT    NOT NULL,
            action      TEXT    NOT NULL,
            target_id   INTEGER,
            target_name TEXT    NOT NULL,
            detail      TEXT    NOT NULL DEFAULT '',
            created_at  INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_staff_actions_target
            ON staff_actions(target_name);

        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT    PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

        -- one row per character slot. the primary key is (user_id, slot)
        -- rather than an autoincrement id, so writing a slot twice is an
        -- upsert instead of a duplicate row. slots are 0..3.
        CREATE TABLE IF NOT EXISTS saves (
            user_id     INTEGER NOT NULL,
            slot        INTEGER NOT NULL,
            class_id    TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            level       INTEGER NOT NULL DEFAULT 1,
            area        TEXT    NOT NULL DEFAULT 'elusion',
            hp          INTEGER NOT NULL DEFAULT 10,
            max_hp      INTEGER NOT NULL DEFAULT 10,
            mana        INTEGER NOT NULL DEFAULT 10,
            max_mana    INTEGER NOT NULL DEFAULT 10,
            stamina     INTEGER NOT NULL DEFAULT 10,
            max_stamina INTEGER NOT NULL DEFAULT 10,
            gold        INTEGER NOT NULL DEFAULT 0,
            xp          INTEGER NOT NULL DEFAULT 0,
            xp_to_next  INTEGER NOT NULL DEFAULT 100,
            bank_gold   INTEGER NOT NULL DEFAULT 0,
            -- which pet this character currently has out, as the client's
            -- item_id (e.g. 'petpoisonslimesmall'). Empty string means none.
            -- Deliberately NOT validated against a list of known pets: the
            -- server has no item registry and inventing one here would mean
            -- every new pet the game adds needs a matching server deploy
            -- before it could be equipped. Same reasoning as bank item_ids.
            active_pet_id TEXT NOT NULL DEFAULT '',
            updated_at  INTEGER NOT NULL,
            PRIMARY KEY (user_id, slot),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- the anti-duplication design is the composite primary key. one row
        -- per item per slot means a deposit can only ever UPDATE a quantity,
        -- never insert a second row for the same item. the database refuses
        -- to represent the duplicated state, so no application bug can
        -- create it.
        -- Account-shared game state. Separate from `users`, which is auth:
        -- a password hash and a rank have nothing to do with a lusion
        -- balance, and keeping them apart means the auth table stays the thing
        -- you can reason about when something goes wrong with logging in.
        CREATE TABLE IF NOT EXISTS accounts (
            user_id   INTEGER PRIMARY KEY,
            lusions   INTEGER NOT NULL DEFAULT 0 CHECK (lusions >= 0),
            bank_gold INTEGER NOT NULL DEFAULT 0 CHECK (bank_gold >= 0),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- THE BANK IS ACCOUNT-SHARED, NOT PER-CHARACTER. characterdata.gd puts
        -- bank_gold and bank_inventory in account_data, commented "account-
        -- shared, safe from death" - that is the whole point of the feature.
        -- This table used to carry a `slot` column, which would have made
        -- anything your warrior banked invisible to your mage.
        --
        -- Positional for the same reason carry_items is: the client holds it as
        -- an array of cells with nulls for the empty ones, and the player
        -- expects things to stay where they put them.
        CREATE TABLE IF NOT EXISTS bank_items (
            user_id  INTEGER NOT NULL,
            position INTEGER NOT NULL,
            item_id  TEXT    NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (user_id, position),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- The backpack. Keyed on POSITION, not item_id, unlike bank_items
        -- above: the bank is a set of stacks where order does not matter, but
        -- the backpack is a grid where the player expects a potion to stay in
        -- the cell they dragged it to, and two stacks of the same item in two
        -- different cells is an ordinary legal state. Keying on item_id would
        -- silently merge them and reshuffle the bag on every login.
        CREATE TABLE IF NOT EXISTS carry_items (
            user_id  INTEGER NOT NULL,
            slot     INTEGER NOT NULL,
            position INTEGER NOT NULL,
            item_id  TEXT    NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (user_id, slot, position),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- A loot bag the server rolled and still owns. The client renders a
        -- copy; taking anything out of it is a request, checked against these
        -- rows. That is what takes `gold` off the list of things a client can
        -- simply declare.
        CREATE TABLE IF NOT EXISTS loot_bags (
            bag_id     TEXT    PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            slot       INTEGER NOT NULL,
            enemy_id   TEXT    NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_loot_bags_created ON loot_bags(created_at);

        -- Position-keyed like every other container here. "Take slot 2" is a
        -- request the server can answer exactly once; "take a healthpotion" is
        -- ambiguous when the bag holds two stacks, and a client sending it twice
        -- would be asking for a duplication bug.
        CREATE TABLE IF NOT EXISTS loot_bag_items (
            bag_id   TEXT    NOT NULL,
            position INTEGER NOT NULL,
            item_id  TEXT    NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (bag_id, position),
            FOREIGN KEY (bag_id) REFERENCES loot_bags(bag_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS skills (
            user_id  INTEGER NOT NULL,
            slot     INTEGER NOT NULL,
            skill_id TEXT    NOT NULL,
            level    INTEGER NOT NULL DEFAULT 1 CHECK (level > 0),
            xp       INTEGER NOT NULL DEFAULT 0 CHECK (xp >= 0),
            PRIMARY KEY (user_id, slot, skill_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )

    _migrate_bank_to_account(db)

    _migrate_add_column(db, "saves", "active_pet_id", "TEXT NOT NULL DEFAULT ''")

    # Millisecond timestamp of the last kill credited to this character, for the
    # rate limit in /api/combat/kill. 0 means "never", which is correctly in the
    # past for every comparison.
    _migrate_add_column(db, "saves", "last_kill_at", "INTEGER NOT NULL DEFAULT 0")

    # Kill-rate tokens. Defaults to the full bucket so an existing character is
    # not penalised for having played before this column existed.
    _migrate_add_column(db, "saves", "kill_tokens", "REAL NOT NULL DEFAULT 20.0")

    _migrate_role_column(db)

    # AFTER the line above, never before. That migration reads is_admin to
    # decide who becomes a dev; dropping the column first would silently demote
    # every one of them on a database that had not migrated yet.
    _migrate_drop_is_admin(db)

    # BAN STATE. Three columns rather than one, because a single nullable
    # timestamp cannot say both "not banned" and "banned forever" - NULL would
    # have to mean both.
    #
    #   is_banned = 0                       not banned
    #   is_banned = 1, ban_expires_at NULL  permanent
    #   is_banned = 1, ban_expires_at set   until that moment
    #
    # Permanent is a real state, queryable and displayable, rather than a very
    # large number. A ban of 9999 days shows the player a date decades away,
    # cannot be told apart from a long timeout, and stops being distinguishable
    # at all the moment someone types a bigger one.
    _migrate_add_column(db, "users", "is_banned", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(db, "users", "ban_expires_at", "INTEGER")
    _migrate_add_column(db, "users", "ban_reason", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(db, "users", "banned_by", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(db, "users", "banned_at", "INTEGER NOT NULL DEFAULT 0")

    db.commit()
    db.close()


def _migrate_bank_to_account(db):
    """
    Move a per-character bank onto the account, and re-key it by position.

    WHY THIS IS NOT _migrate_add_column's JOB: that helper adds a COLUMN.
    bank_items needed its PRIMARY KEY changed - (user_id, slot, item_id) became
    (user_id, position) - and SQLite cannot alter a primary key. The table has
    to be rebuilt.

    WHY IT IS NEEDED AT ALL: CREATE TABLE IF NOT EXISTS does nothing to a table
    that already exists, so the corrected schema above reaches a fresh database
    and no other. elusion.db still had the old shape, and every bank request
    against it failed with "no such column: position" - while the test suite
    passed, because it builds its database from scratch every run.

    IDEMPOTENT. Both halves check for the end state first, so this is safe on
    every boot forever.
    """
    columns = {row[1] for row in db.execute("PRAGMA table_info(bank_items)")}

    if columns and "position" not in columns:
        # Old shape. Read it out before touching anything.
        #
        # SUM across slots: the same item banked by two characters was two rows
        # under the old key and is one stack under the new one. Summing is the
        # only reading that does not throw away items.
        legacy = db.execute(
            """
            SELECT user_id, item_id, SUM(quantity)
              FROM bank_items
             GROUP BY user_id, item_id
             ORDER BY user_id, item_id
            """
        ).fetchall()

        db.execute("ALTER TABLE bank_items RENAME TO bank_items_legacy")
        db.execute(
            """
            CREATE TABLE bank_items (
                user_id  INTEGER NOT NULL,
                position INTEGER NOT NULL,
                item_id  TEXT    NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                PRIMARY KEY (user_id, position),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        # Positions are assigned in item_id order. The old schema had no notion
        # of where anything sat, so there is no original order to preserve -
        # any stable arrangement is as faithful as any other.
        next_position = {}
        for user_id, item_id, quantity in legacy:
            position = next_position.get(user_id, 0)
            db.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (user_id,))
            db.execute(
                "INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, ?, ?, ?)",
                (user_id, position, item_id, quantity),
            )
            next_position[user_id] = position + 1

        db.execute("DROP TABLE bank_items_legacy")

    # Banked GOLD was a column on `saves`, so it was per-character too. Pool it
    # into the account and zero the old column - which is also what makes this
    # half idempotent: a second run finds nothing left to move.
    save_columns = {row[1] for row in db.execute("PRAGMA table_info(saves)")}
    if "bank_gold" in save_columns:
        pooled = db.execute(
            "SELECT user_id, SUM(bank_gold) FROM saves GROUP BY user_id HAVING SUM(bank_gold) > 0"
        ).fetchall()
        for user_id, total in pooled:
            db.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (user_id,))
            db.execute(
                "UPDATE accounts SET bank_gold = bank_gold + ? WHERE user_id = ?",
                (total, user_id),
            )
            db.execute("UPDATE saves SET bank_gold = 0 WHERE user_id = ?", (user_id,))


def _migrate_role_column(db):
    """
    Carry the old is_admin boolean across to the `role` column, once.

    The ranks are player < mod < dev < owner. There is no 'admin' rank; the
    column this replaced was called is_admin, which is the only reason that
    word appears here at all.

    A boolean cannot express three ranks. The moment a mod exists, "can this
    person do X" stops being one bit and becomes an expression repeated in
    every endpoint - so the rank became an ordered column before there was any
    endpoint reading it, which is the cheap moment to change it.

    RUNS EXACTLY ONCE, and the guard is that the column did not exist a moment
    ago - not that the values in it look untouched.

    The first version guarded on `role = 'player'`, which reads as "only fill
    in rows nobody has set". It is not: an admin who was DEMOTED is a row with
    role 'player' and is_admin still 1, so every restart promoted them again.
    init_db() runs on every boot, so a migration guarded by anything other than
    "this has not happened yet" is a migration that happens forever.
    """
    existing = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "role" in existing:
        return

    db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'player'")

    # An old is_admin=1 account becomes a dev - the highest rank that is
    # storable, since owner comes from the environment.
    if "is_admin" in existing:
        db.execute("UPDATE users SET role = 'dev' WHERE is_admin = 1")


def _migrate_drop_is_admin(db):
    """
    Remove users.is_admin once `role` has taken over from it.

    MUST RUN AFTER _migrate_role_column(). See the call site.

    Dropping a column from the one database holding real accounts is not
    something to do casually, and the earlier version of this deliberately did
    not: it left the column in place on the grounds that nothing read it. That
    was the right call while the client still received an is_admin key. The
    client does not any more, and a column nobody reads but everybody sees is
    how "there is no admin rank" keeps needing to be explained.

    ALTER TABLE DROP COLUMN needs SQLite 3.35 (March 2021). An older build
    keeps the column, which costs nothing - no query names it.
    """
    existing = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "is_admin" not in existing:
        return
    if "role" not in existing:
        # Never drop the source before the destination exists.
        return

    try:
        db.execute("ALTER TABLE users DROP COLUMN is_admin")
    except sqlite3.OperationalError as exc:
        print(f"[migrate] users.is_admin left in place ({exc}); nothing reads it")


def _migrate_rename_admin_actions(db):
    """
    Rename the audit table off the word 'admin'.

    RUNS BEFORE THE SCHEMA BLOCK, which is the whole point. init_db() creates
    staff_actions with CREATE TABLE IF NOT EXISTS; if that ran first it would
    make an empty staff_actions, this rename would find the name taken and skip,
    and every moderation action ever recorded would be stranded in a table
    nothing queries. An audit trail that silently stops being the audit trail is
    worse than no audit trail.
    """
    names = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}
    if "admin_actions" not in names or "staff_actions" in names:
        return
    db.execute("ALTER TABLE admin_actions RENAME TO staff_actions")


def _migrate_add_column(db, table, column, definition):
    """
    Add a column to an existing table, once.

    CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
    a new column in the schema above reaches a FRESH database and no other.
    elusion.db already holds real accounts and characters, and dropping it to
    pick up a column would delete them - so existing databases get the column
    added here instead.

    PRAGMA table_info is the check rather than catching the "duplicate column"
    error, because a bare try/except around ALTER would also swallow a genuine
    failure (locked database, bad definition) and leave the column silently
    missing until the first query against it.
    """
    existing = {row[1] for row in db.execute("PRAGMA table_info(%s)" % table)}
    if column in existing:
        return
    db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))


init_db()


# =============================================================================
# VALIDATION
# =============================================================================

def validate_credentials(payload):
    """
    Check an incoming register/login body.
    Returns (is_valid, errors_list) - same shape as spells_api.
    """
    errors = []

    if not isinstance(payload, dict):
        errors.append("Payload must be a JSON object.")
        return False, errors

    for field in REQUIRED_FIELDS:
        if field not in payload:
            errors.append(f"Missing required field: {field}")

    if errors:
        return False, errors

    username = payload.get("username")
    password = payload.get("password")

    if not isinstance(username, str) or not USERNAME_PATTERN.match(username):
        errors.append(
            "Field 'username' must be 3-20 characters, letters, numbers and underscore only."
        )

    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        errors.append(
            f"Field 'password' must be a string of at least {MIN_PASSWORD_LENGTH} characters."
        )

    return len(errors) == 0, errors


# =============================================================================
# AUTH HELPERS
# =============================================================================

def issue_token(user_id):
    """Mint a session token and store it. Returns (token, expires_at)."""
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + TOKEN_TTL

    db = get_db()
    db.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires_at),
    )
    db.commit()

    return token, expires_at


def user_for_token(token):
    """Resolve a bearer token to a user row, or None if missing/expired."""
    if not token:
        return None

    db = get_db()
    row = db.execute(
        """
        SELECT u.id, u.username, u.role,
               u.is_banned, u.ban_expires_at, u.ban_reason, u.banned_by, u.banned_at,
               s.expires_at
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token = ?
        """,
        (token,),
    ).fetchone()

    if row is None:
        return None

    if row["expires_at"] < int(time.time()):
        # expired - clean it up rather than leaving dead rows around
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        db.commit()
        return None

    # THE BAN IS CHECKED HERE, not only at login, because login is not the only
    # door. Banning deletes the user's sessions in the same transaction, so a
    # live token should not exist - but "should not exist" is not a security
    # control, and require_auth wraps every authenticated route at once.
    #
    # The session goes with them rather than being left to expire.
    if ban_state(row) is not None:
        db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        db.commit()
        return None

    return row


def bearer_token():
    """Pull the token out of an 'Authorization: Bearer <token>' header."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header[7:].strip()


def is_owner(username):
    """
    True only for the account named by ELUSION_OWNER.

    Case-insensitive, because `users.username` is COLLATE NOCASE - Tunacan and
    tunacan are the same account, so an owner check that disagreed with the
    database about that would lock the owner out of their own server.

    Fails closed: no configured owner means nobody is the owner.
    """
    if not OWNER_USERNAME or not username:
        return False
    return str(username).casefold() == OWNER_USERNAME.casefold()


# The ranks, in order. Index is the comparison - "mod or above" is one
# integer test rather than an expression repeated in every endpoint, which is
# what a pile of booleans turns into the moment there is more than one of them.
#
# 'owner' is last and is NOT storable. users.role has a CHECK that refuses it,
# and role_for() supplies it from the environment instead. That is what makes
# the top rank ungrantable: there is no write that produces it.
ROLES = ("player", "mod", "dev", "owner")

# What a fresh account is, and what an unrecognised value is read as. A row
# holding something this server has never heard of - written by an older build,
# or by hand - must read as the LEAST privilege, never the most.
DEFAULT_ROLE = "player"

# The ranks a moderation endpoint may assign. 'owner' is absent and always will
# be - it comes from ELUSION_OWNER, so no request can grant it. Mirrors
# SETTABLE_ROLES in set_role.py.
SETTABLE_ROLES = tuple(r for r in ROLES if r != "owner")


def role_for(user):
    """
    The effective rank of a user row.

    The owner is decided before the column is consulted, so revoking their row
    cannot lock them out of their own server, and a column set to 'owner' by
    hand cannot grant it.
    """
    if is_owner(user["username"]):
        return "owner"

    stored = user["role"] if "role" in user.keys() else DEFAULT_ROLE
    return stored if stored in ROLES and stored != "owner" else DEFAULT_ROLE


def role_at_least(user, minimum):
    """
    True when this user's rank is `minimum` or higher.

    An unrecognised `minimum` denies rather than raising. It should never
    happen - the argument comes from this codebase, not from a request - but
    ROLES.index() on a name that is not there throws ValueError, and a route
    asking "may I" deserves a "no" rather than a 500 with a traceback in it.

    That is not theoretical: removing the 'admin' rank turned every surviving
    role_at_least(..., "admin") call into a crash.
    """
    if minimum not in ROLES:
        return False
    return ROLES.index(role_for(user)) >= ROLES.index(minimum)


def can_act_on(actor, target):
    """
    True when `actor` may moderate `target`.

    STRICTLY ABOVE, not at-or-above. One comparison produces every rule that
    was wanted:

        a mod cannot ban another mod      equal ranks, refused
        a dev can ban a mod               dev is above mod
        a dev cannot ban another dev      equal ranks, refused
        the owner can ban anyone          nothing is at or above the owner
        nobody can ban the owner          the owner is the top of ROLES
        a player cannot ban anyone        there is nothing below player

    It also refuses acting on yourself, since your own rank is never strictly
    below your own - which saves a separate guard against banning yourself.

    role_for() is used on both sides rather than the raw column, so the owner
    outranks everyone whatever their row says, and an unrecognised rank is
    treated as `player` on both sides of the comparison.
    """
    return ROLES.index(role_for(actor)) > ROLES.index(role_for(target))


def ban_state(user):
    """
    The live ban on a user row, or None when there is not one.

    Reads EXPIRY AGAINST NOW rather than trusting is_banned alone, so a served
    sentence stops mattering the moment it is up - nothing has to run on a
    schedule to release people, and a server that was switched off for a week
    does not keep anyone an extra week.
    """
    if not user["is_banned"]:
        return None

    expires = user["ban_expires_at"]
    if expires is not None and int(expires) <= int(time.time()):
        return None

    return {
        "permanent": expires is None,
        "expires_at": None if expires is None else int(expires),
        "reason": user["ban_reason"] or "",
        "banned_by": user["banned_by"] or "",
        "banned_at": int(user["banned_at"] or 0),
    }


def log_staff_action(actor, action, target_name, target_id=None, detail=""):
    """
    Record one moderation action. Called inside the same transaction as the
    thing it describes, so an action cannot happen without a line about it.
    """
    get_db().execute(
        """
        INSERT INTO staff_actions
               (actor_id, actor_name, action, target_id, target_name, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (actor["id"], actor["username"], action, target_id, target_name,
         detail, int(time.time())),
    )


def _user_by_name(username):
    return get_db().execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def _moderation_target(payload):
    """
    Resolve and authorise the target of a moderation action.

    Returns (row, None) or (None, response). Every refusal is the SAME 404,
    whether the account does not exist or is simply out of your reach - a
    distinguishable answer would let a mod map out who outranks them.
    """
    username = str(payload.get("username", "")).strip()
    if not username:
        return None, bad_request("username is required")

    target = _user_by_name(username)
    not_found = ({"error": "Not Found", "message": "No such account."}, 404)

    if target is None:
        return None, not_found
    if not can_act_on(g.user, target):
        return None, not_found

    return target, None


def require_role(minimum):
    """
    Decorator for a route that needs a rank. Sits inside @require_auth, which
    is what puts the user row on g.

        @app.post("/api/staff/whatever")
        @require_auth
        @require_role("mod")
        def whatever(): ...
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not role_at_least(g.user, minimum):
                # 404 rather than 403, same as require_owner. A 403 confirms
                # the route exists and that you are not allowed to use it,
                # which tells someone exactly where to push.
                return {"error": "Not Found", "message": "Not found."}, 404
            return view(*args, **kwargs)
        return wrapped
    return decorator


def require_owner(view):
    """
    Decorator for the handful of things only the server's owner may do.

    Deliberately NOT a check against a database column. See OWNER_USERNAME.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_owner(g.user["username"]):
            # 404, not 403. A 403 confirms the route exists and that you are
            # not allowed to use it, which tells an attacker where to aim.
            return {"error": "Not Found", "message": "Not found."}, 404
        return view(*args, **kwargs)

    return wrapped


def require_auth(view):
    """
    Decorator for any route that needs a logged-in user.
    Puts the user row on g.user so the view can read it.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = user_for_token(bearer_token())
        if user is None:
            return {
                "error": "Unauthorized",
                "message": "Missing, invalid or expired token.",
            }, 401
        g.user = user
        return view(*args, **kwargs)

    return wrapped


# =============================================================================
# AUTH
# =============================================================================

@app.post("/api/auth/register")
def register():
    """
    Create a new account
    ---
    tags:
      - Auth
    consumes:
      - application/json
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            username:
              type: string
            password:
              type: string
    responses:
      201:
        description: Account created, returns a session token
        schema:
          type: object
          properties:
            user_id:
              type: integer
            username:
              type: string
            token:
              type: string
            expires_at:
              type: integer
      400:
        description: Validation error
        schema:
          type: object
          properties:
            error:
              type: string
            message:
              type: array
              items:
                type: string
      409:
        description: Username already taken
        schema:
          type: object
          properties:
            error:
              type: string
            message:
              type: string
    """
    data = request.get_json(silent=True)

    is_valid, errors = validate_credentials(data)
    if not is_valid:
        return {"error": "Bad Request", "message": errors}, 400

    username = data["username"]
    password_hash = generate_password_hash(data["password"])

    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, int(time.time())),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return {
            "error": "Conflict",
            "message": "That username is already taken.",
        }, 409

    user_id = cursor.lastrowid
    token, expires_at = issue_token(user_id)

    # THE SAME SHAPE AS LOGIN AND SESSION. This used to omit keys that those
    # two returned, so a client that read the response after registering got a
    # different object than the one it got after logging in - and the
    # difference was a missing key rather than a false value, which is the kind
    # that surfaces as a crash somewhere else entirely.
    #
    # A new account is always a player, but it can be the owner: registering
    # the account named by ELUSION_OWNER is exactly how a fresh server gets one.
    return {
        "user_id": user_id,
        "username": username,
        "role": "owner" if is_owner(username) else DEFAULT_ROLE,
        "is_owner": is_owner(username),
        "token": token,
        "expires_at": expires_at,
    }, 201


@app.post("/api/auth/login")
def login():
    """
    Log in and receive a session token
    ---
    tags:
      - Auth
    consumes:
      - application/json
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            username:
              type: string
            password:
              type: string
    responses:
      200:
        description: Logged in
        schema:
          type: object
          properties:
            user_id:
              type: integer
            username:
              type: string
            role:
              type: string
            token:
              type: string
            expires_at:
              type: integer
      400:
        description: Validation error
      401:
        description: Bad username or password
        schema:
          type: object
          properties:
            error:
              type: string
            message:
              type: string
    """
    data = request.get_json(silent=True)

    is_valid, errors = validate_credentials(data)
    if not is_valid:
        return {"error": "Bad Request", "message": errors}, 400

    db = get_db()
    row = db.execute(
        # SELECT * rather than a column list: ban_state() and role_for() both read
        # from this row, and a list here is a list that gets forgotten the next
        # time a column is added - which is exactly what happened when the ban
        # columns arrived.
        "SELECT * FROM users WHERE username = ?",
        (data["username"],),
    ).fetchone()

    # same response whether the user is missing or the password is wrong -
    # otherwise this endpoint tells an attacker which usernames exist.
    if row is None or not check_password_hash(row["password_hash"], data["password"]):
        return {
            "error": "Unauthorized",
            "message": "Incorrect username or password.",
        }, 401

    # AFTER the password check, deliberately. Telling someone their account is
    # banned before they have proved it is theirs would make this endpoint a
    # way to find out who is banned.
    #
    # 403 rather than 401, and the reason IS included - unlike the routes that
    # hide behind a 404, a banned player has every right to know they are
    # banned and why. Silence there reads as the game being broken.
    ban = ban_state(row)
    if ban is not None:
        return {
            "error": "Forbidden",
            "message": "This account is banned." if ban["permanent"]
                       else "This account is banned until further notice.",
            "ban": ban,
        }, 403

    token, expires_at = issue_token(row["id"])

    return {
        "user_id": row["id"],
        "username": row["username"],
        # THE OWNER IS ALWAYS THE OWNER, whatever the column says. A stored
        # rank is a row someone could revoke; the owner is configuration.
        # Locking the owner out of their own server with an UPDATE should not
        # be possible, so role_for() answers from ELUSION_OWNER first.
        "role": role_for(row),
        "is_owner": is_owner(row["username"]),
        "token": token,
        "expires_at": expires_at,
    }, 200


@app.get("/api/auth/session")
@require_auth
def session_info():
    """
    Validate a stored token
    ---
    tags:
      - Auth
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200:
        description: Token is valid
        schema:
          type: object
          properties:
            user_id:
              type: integer
            username:
              type: string
            role:
              type: string
            expires_at:
              type: integer
      401:
        description: Missing, invalid or expired token
    """
    return {
        "user_id": g.user["id"],
        "username": g.user["username"],
        "role": role_for(g.user),
        "is_owner": is_owner(g.user["username"]),
        "expires_at": g.user["expires_at"],
    }, 200


@app.post("/api/auth/logout")
@require_auth
def logout():
    """
    Invalidate the current token
    ---
    tags:
      - Auth
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      204:
        description: Token invalidated
      401:
        description: Missing, invalid or expired token
    """
    db = get_db()
    db.execute("DELETE FROM sessions WHERE token = ?", (bearer_token(),))
    db.commit()
    return "", 204


# =============================================================================
# SAVES  -  GET/PUT /api/save
# =============================================================================

MAX_SLOT = 3
VALID_CLASSES = {"warrior", "mage", "tank", "healer"}


def parse_slot(raw):
    """
    Validate a slot number coming from a query string or a JSON body.

    Note the bool check: in Python `True == 1` and `isinstance(True, int)` is
    True, so without it `{"slot": true}` would quietly be accepted as slot 1.
    """
    if isinstance(raw, bool):
        return None
    try:
        slot = int(raw)
    except (TypeError, ValueError):
        return None
    if slot < 0 or slot > MAX_SLOT:
        return None
    return slot


def bad_request(message):
    return {"error": "Bad Request", "message": message}, 400


def parse_pet_id(raw):
    """
    Validate an active_pet_id from a request body.

    Same shape as a bank item_id - any string the client's item registry knows,
    capped at 64 characters. Empty string is valid and means "no pet out".
    Returns None if the value can't be one, so the caller can 400 rather than
    store something meaningless.
    """
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return None
    if not isinstance(raw, (str, int, float)):
        return None
    text = str(raw).strip()
    if len(text) > 64:
        return None
    return text


def save_row_to_dict(row):
    return {
        "slot": row["slot"],
        "class_id": row["class_id"],
        "name": row["name"],
        "level": row["level"],
        "area": row["area"],
        "active_pet_id": row["active_pet_id"],
        "updated_at": row["updated_at"],
    }


@app.get("/api/save")
@require_auth
def list_saves():
    """
    List the account's character slots
    ---
    tags:
      - Save
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200:
        description: Occupied slots only; an empty account returns an empty list
      401:
        description: Missing, invalid or expired token
    """
    rows = get_db().execute(
        "SELECT * FROM saves WHERE user_id = ? ORDER BY slot",
        (g.user["id"],),
    ).fetchall()

    return {
        "username": g.user["username"],
        "slots": [save_row_to_dict(r) for r in rows],
    }, 200


@app.put("/api/save")
@require_auth
def write_save():
    """
    Create or overwrite one character slot
    ---
    tags:
      - Save
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, class_id, name]
          properties:
            slot:     {type: integer, example: 0}
            class_id: {type: string,  example: warrior}
            name:     {type: string,  example: Tunacan}
            level:    {type: integer, example: 12}
            area:     {type: string,  example: elusion}
            active_pet_id:
              type: string
              example: petpoisonslimesmall
              description: "Pet currently out, as the client's item_id. Empty string means none. Omitted leaves the stored value unchanged."
    responses:
      200:
        description: Slot written
      400:
        description: Invalid slot, class, name or active_pet_id
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    class_id = str(payload.get("class_id", "")).strip().lower()
    if class_id not in VALID_CLASSES:
        return bad_request("class_id must be one of: %s" % ", ".join(sorted(VALID_CLASSES)))

    name = str(payload.get("name", "")).strip()
    if not name or len(name) > 20:
        return bad_request("name must be 1-20 characters")

    # NOT from the payload. level is server-owned (see SERVER_OWNED_STATS): a
    # new character starts at 1, and an existing one keeps whatever the server
    # has. Taking it from the client here would have left an open door beside
    # the one /api/player/status just closed.
    level = 1
    area = str(payload.get("area", "elusion")).strip() or "elusion"

    # OMITTED means "leave it alone", not "clear it".
    #
    # The client writes this slot on character creation and again on every save,
    # and not every one of those callers knows or cares which pet is out. If a
    # missing key cleared the field, one save from a caller that doesn't send it
    # would silently unequip the player's pet - and a pet is a 1-in-216 drop, so
    # that is the last thing in the game that should vanish by omission.
    #
    # Sending "" explicitly is how you clear it. That's a deliberate act.
    pet_key_sent = "active_pet_id" in payload
    active_pet_id = parse_pet_id(payload.get("active_pet_id")) if pet_key_sent else ""
    if pet_key_sent and active_pet_id is None:
        return bad_request("active_pet_id must be a string of at most 64 characters")

    now = int(time.time())

    db = get_db()

    # Checked BEFORE the upsert, because afterwards there is no way to tell a
    # character that was just created from one that already existed - and they
    # need different treatment below.
    is_new = db.execute(
        "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (g.user["id"], slot)
    ).fetchone() is None

    # ON CONFLICT rather than DELETE-then-INSERT: an upsert is one statement,
    # so there is no window where the slot exists in neither state.
    #
    # active_pet_id uses excluded.* only when the caller actually sent the key;
    # otherwise it keeps whatever the row already holds. On a fresh INSERT there
    # is nothing to keep, so it lands as the '' default.
    db.execute(
        """
        INSERT INTO saves (user_id, slot, class_id, name, level, area, active_pet_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, slot) DO UPDATE SET
            class_id      = excluded.class_id,
            name          = excluded.name,
            -- level is NOT taken from excluded. The row keeps the level the
            -- server granted through /api/combat/kill; the 1 in VALUES only
            -- ever applies to a brand new character.
            level         = saves.level,
            area          = excluded.area,
            active_pet_id = CASE WHEN ? THEN excluded.active_pet_id
                                 ELSE saves.active_pet_id END,
            updated_at    = excluded.updated_at
        """,
        (g.user["id"], slot, class_id, name, level, area, active_pet_id, now,
         1 if pet_key_sent else 0),
    )
    db.commit()

    # DERIVED STATS BELONG ON A NEW CHARACTER TOO, not only on the next status
    # write. Without this a freshly created warrior sat at the table's DEFAULT 10
    # hp until something happened to push a status - and if the client had been
    # trusted for max_hp, nobody would ever have noticed, because it would have
    # overwritten the 10 on its first save.
    #
    # Recomputed on every save rather than only on creation: max_hp is a pure
    # function of class and level, so there is no state where storing anything
    # else is correct.
    stored_level = db.execute(
        "SELECT level FROM saves WHERE user_id = ? AND slot = ?", (g.user["id"], slot)
    ).fetchone()["level"]
    derived = gamedata.max_stats_for(class_id, stored_level)

    if derived is not None:
        if is_new:
            # A new character starts with full pools. Only on creation - doing it
            # on every save would refill the player's health for free.
            db.execute(
                """
                UPDATE saves
                   SET max_hp = ?, hp = ?, max_mana = ?, mana = ?,
                       max_stamina = ?, stamina = ?
                 WHERE user_id = ? AND slot = ?
                """,
                (derived["max_hp"], derived["max_hp"],
                 derived["max_mana"], derived["max_mana"],
                 derived["max_stamina"], derived["max_stamina"],
                 g.user["id"], slot),
            )
        else:
            db.execute(
                "UPDATE saves SET max_hp = ?, max_mana = ?, max_stamina = ? WHERE user_id = ? AND slot = ?",
                (derived["max_hp"], derived["max_mana"], derived["max_stamina"],
                 g.user["id"], slot),
            )
        db.commit()

    # active_pet_id is echoed back only when the caller actually set it, so a
    # client can tell "you stored this" apart from "we left yours alone".
    result = {"slot": slot, "updated_at": now}
    if pet_key_sent:
        result["active_pet_id"] = active_pet_id
    return result, 200


# =============================================================================
# PLAYER STATUS  -  GET /api/player/status
# =============================================================================

@app.get("/api/player/status")
@require_auth
def player_status():
    """
    Live stat values for the HUD
    ---
    tags:
      - Player
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: query
        name: slot
        type: integer
        required: true
        description: Character slot, 0-3
    responses:
      200:
        description: Current stats for that slot
      400:
        description: Missing or out-of-range slot
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    slot = parse_slot(request.args.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    row = get_db().execute(
        "SELECT * FROM saves WHERE user_id = ? AND slot = ?",
        (g.user["id"], slot),
    ).fetchone()

    # 404 is the right answer for an empty slot: the request was valid, the
    # resource does not exist. The client distinguishes this from a transport
    # failure and shows "no character here" instead of "server is down".
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    return {
        "slot": row["slot"],
        "level": row["level"],
        "hp": row["hp"], "max_hp": row["max_hp"],
        "mana": row["mana"], "max_mana": row["max_mana"],
        "stamina": row["stamina"], "max_stamina": row["max_stamina"],
        "gold": row["gold"],
        "xp": row["xp"], "xp_to_next": row["xp_to_next"],
    }, 200


# =============================================================================
# PLAYER STATUS - WRITE
# =============================================================================

# Every stat the client is allowed to push, and the ceiling each one is
# checked against. The client proposes; the server decides. A value that
# cannot legally exist is refused rather than clamped, because clamping
# hides the bug that produced it.
STATUS_FIELDS = {
    "level":       None,        # no paired maximum
    "hp":          "max_hp",
    "mana":        "max_mana",
    "stamina":     "max_stamina",
    "gold":        None,
    "xp":          None,
    "xp_to_next":  None,
    "max_hp":      None,
    "max_mana":    None,
    "max_stamina": None,
}

# nothing in this game legitimately exceeds these. a number past one of them
# means a corrupted save, an overflow, or someone editing packets - all three
# are worth refusing rather than storing.
STAT_CEILING = 1_000_000_000

# FIELDS THE SERVER OWNS. A client may send them; the server ignores the values
# and keeps its own.
#
# These three are granted by /api/combat/kill, which applies the level-up loop
# itself and commits the result. The server therefore already knows what they
# should be, and an assertion from the client is not evidence of anything.
#
# THIS IS THE HALF THAT MAKES THE OTHER HALF MEAN SOMETHING. Moving the loot
# roll to the server closed the "give myself a pet" hole, but while
# /api/player/status accepted a level, a modified client could simply declare
# itself level 60 and skip the game entirely. It could not do that until there
# was a server-side record to contradict it - which is why this change had to
# come second, not first.
#
# NOT IN THIS LIST, and honestly so:
#
#   gold          arrives by picking up a loot bag, and the pickup is still
#                 client-side. The server rolled what was IN the bag but does
#                 not know the player walked over it. Closing that needs the
#                 bag itself to be server-owned - a bag id, and an endpoint
#                 that transfers from it. That is the next piece.
#   hp/mana/      the result of combat and regen the server does not simulate.
#   stamina       Clamped against their maxima below, which is all the server
#                 can honestly say about them.
#   hp/mana/      the result of combat and regen the server does not simulate.
#   stamina       Clamped against their maxima below, which is all the server
#                 can honestly say about them.
# The two item ids that resolve into carried gold rather than a backpack cell.
# Read from gamedata so the authored source stays baseenemy.gd's constants.
CONSTANTS_GOLD_SMALL = gamedata.CONSTANTS.get("gold_small_id", "smallamountofgold")
CONSTANTS_GOLD_LARGE = gamedata.CONSTANTS.get("gold_large_id", "largeamountofgold")

SERVER_OWNED_STATS = ("level", "xp", "xp_to_next")

# DERIVED, not merely owned. These are not stored from a request at all - they
# are recomputed from the character's class and level every time the status is
# written, using the same curve the client runs:
#
#     max_hp = hp_base + (level - 1) * hp_per_lvl
#
# That curve used to be four literals inside warrior.gd's _set_stat_curve(), so
# the server knew your level AND your class and still could not work out your
# maximum health. ClassData moved it into data/classes/*.tres and the exporter
# carries it here - the same route EnemyData took.
DERIVED_STATS = ("max_hp", "max_mana", "max_stamina")


def parse_stat(raw):
    """Return a non-negative int, or None if the value isn't one."""
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > STAT_CEILING:
        return None
    return value


@app.put("/api/player/status")
@require_auth
def write_player_status():
    """
    Push current stat values for one character slot
    ---
    tags:
      - Player
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot]
          properties:
            slot:        {type: integer, example: 0}
            level:       {type: integer, example: 12}
            hp:          {type: integer, example: 88}
            max_hp:      {type: integer, example: 120}
            mana:        {type: integer, example: 30}
            max_mana:    {type: integer, example: 60}
            stamina:     {type: integer, example: 45}
            max_stamina: {type: integer, example: 50}
            gold:        {type: integer, example: 1450}
            xp:          {type: integer, example: 15320}
            xp_to_next:  {type: integer, example: 2100}
    responses:
      200:
        description: The full status after the write
      400:
        description: A field was not a non-negative integer, or hp exceeded max_hp
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    row = get_db().execute(
        "SELECT * FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    # PARTIAL UPDATE: only fields actually present are touched. A client that
    # knows nothing about stamina can still push hp without silently zeroing
    # everything it didn't mention.
    # RECOMPUTED BEFORE ANYTHING IS VALIDATED, so that hp is checked against the
    # maximum the server believes in rather than the one the client sent. A
    # client declaring max_hp = 999999 alongside hp = 999999 would otherwise pass
    # the paired check below on its own say-so.
    derived = gamedata.max_stats_for(row["class_id"], row["level"])

    updates = {}
    ignored = []
    for field in STATUS_FIELDS:
        if field not in payload:
            continue

        # Derived fields are dropped from the request for the same reason as
        # owned ones, but they are then written back from the curve - see below.
        if derived is not None and field in DERIVED_STATS:
            ignored.append(field)
            continue

        # IGNORED, NOT REFUSED. A 400 here would break every honest save: the
        # client sends its whole status block and has no way to know which
        # fields the server has taken ownership of. Dropping them silently and
        # reporting which ones were dropped lets an honest client carry on and
        # gives a dishonest one nothing.
        if field in SERVER_OWNED_STATS:
            ignored.append(field)
            continue

        value = parse_stat(payload[field])
        if value is None:
            return bad_request(
                "%s must be a non-negative integer no greater than %d" % (field, STAT_CEILING)
            )
        updates[field] = value

    if not updates and not ignored:
        return bad_request("no writable fields supplied")

    # Validate against the state AFTER the merge, not against what was sent.
    # Pushing hp=120 alone is illegal if stored max_hp is 100, but legal in the
    # same request that raises max_hp to 120 - and order of keys in JSON must
    # not decide which.
    merged = {field: row[field] for field in STATUS_FIELDS}
    merged.update(updates)

    if derived is not None:
        # The server's own numbers go in AFTER the client's, so they win, and
        # they are added to `updates` so they are actually written - a character
        # that levelled up needs its new maximum stored, not just enforced.
        merged.update(derived)
        updates.update(derived)

    for field, cap_field in STATUS_FIELDS.items():
        if cap_field is None:
            continue
        if merged[field] > merged[cap_field]:
            # CLAMPED, not refused, when the ceiling is one the server derived.
            #
            # A character at full health who levels DOWN - or whose class curve
            # is retuned downward between releases - legitimately arrives with
            # hp above the new max_hp. Refusing would make that character
            # unsaveable. Clamping is the honest reading: you have as much health
            # as the curve allows.
            if cap_field in DERIVED_STATS and derived is not None:
                merged[field] = merged[cap_field]
                updates[field] = merged[cap_field]
                continue
            return bad_request(
                "%s (%d) cannot exceed %s (%d)" % (field, merged[field], cap_field, merged[cap_field])
            )

    db = get_db()
    if updates:
        assignments = ", ".join("%s = ?" % f for f in updates)
        values = list(updates.values()) + [int(time.time()), user_id, slot]
        db.execute(
            "UPDATE saves SET %s, updated_at = ? WHERE user_id = ? AND slot = ?" % assignments,
            values,
        )
        db.commit()

    result = status_payload(user_id, slot)
    if ignored:
        # The server's own values are already in `result`; this just names what
        # was disregarded, so a client can see it is being corrected rather than
        # wondering why its level did not stick.
        result["ignored"] = ignored
    return result, 200


def status_payload(user_id, slot):
    row = get_db().execute(
        "SELECT * FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()
    if row is None:
        return None
    return {
        "slot": row["slot"],
        "level": row["level"],
        "hp": row["hp"], "max_hp": row["max_hp"],
        "mana": row["mana"], "max_mana": row["max_mana"],
        "stamina": row["stamina"], "max_stamina": row["max_stamina"],
        "gold": row["gold"],
        "xp": row["xp"], "xp_to_next": row["xp_to_next"],
    }


# =============================================================================
# THE BANK AND THE ACCOUNT
# =============================================================================
#
# REWRITTEN. The bank was modelled per-CHARACTER and it is account-shared.
#
# characterdata.gd is unambiguous about this - bank_gold and bank_inventory
# live in account_data, commented "account-shared, safe from death", and that
# is the whole point of the feature: carry gold and carry items are lost when
# you die, so the bank is where you put things you do not want to lose, and it
# is shared so a second character can use what the first one banked.
#
# The server had bank_items keyed on (user_id, slot, item_id) and bank_gold as
# a column on `saves`. Banking something on your warrior would have made it
# invisible to your mage, and dying would have felt like losing the bank too.
# That was not a bug anyone had hit, because nothing in the game talks to these
# endpoints yet - it was a bug waiting for the first client that did.
#
# Two other things move with it:
#
# LUSIONS did not exist on the server at all. They are the account-shared
# premium currency and the revive cost, and they were the one piece of player
# state with no column anywhere.
#
# THE BANK IS POSITIONAL, like the backpack and for the same reason: the client
# holds it as an array of BANK_CAPACITY cells with nulls for the empty ones, and
# the player expects things to stay where they put them. Keyed on item_id it was
# a set, which would silently merge two stacks and reshuffle the grid.
#
# The deposit/withdraw op endpoint is gone with it. An op made sense for a set;
# for a grid the client always holds the whole picture, and a whole-array replace
# has no partial state to reconcile. /api/bank/gold stays an op, because gold is
# the one thing here the server can actually verify - it holds both balances and
# can conserve the total.

# Must match BANK_MAX_SLOTS in characterdata.gd. Exported into gamedata.json so
# there is one authored source; the fallback here only applies to an older
# gamedata that predates the key.
#
# THE ONLY DEFINITION. There used to be a second one, BANK_CAPACITY = 40, six
# hundred lines above this. Python took the later assignment, so the bank really
# held 50 and everything worked - right up until someone tidied away the
# "unused" one and the bank silently shrank by ten cells, at which point every
# save with an item in cells 40-49 starts failing validation.
BANK_CAPACITY = int(gamedata.CONSTANTS.get("bank_capacity", 50))


def _ensure_account(user_id):
    """
    Every user gets an accounts row on first touch.

    Created lazily rather than at registration so existing accounts - which
    registered before this table existed - get one the first time they bank
    anything, with no migration step and no backfill to remember to run.
    """
    db = get_db()
    db.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (user_id,))
    return db.execute(
        "SELECT lusions, bank_gold FROM accounts WHERE user_id = ?", (user_id,)
    ).fetchone()


def account_payload(user_id):
    row = _ensure_account(user_id)
    rows = get_db().execute(
        "SELECT position, item_id, quantity FROM bank_items WHERE user_id = ? ORDER BY position",
        (user_id,),
    ).fetchall()

    cells = [None] * BANK_CAPACITY
    for item in rows:
        position = int(item["position"])
        if 0 <= position < BANK_CAPACITY:
            cells[position] = {"item_id": item["item_id"], "quantity": int(item["quantity"])}

    return {
        "lusions": int(row["lusions"]),
        "bank_gold": int(row["bank_gold"]),
        "bank_inventory": cells,
        "capacity": BANK_CAPACITY,
    }


@app.get("/api/account")
@require_auth
def read_account():
    """
    Account-shared state: lusions, bank gold, bank inventory
    ---
    tags:
      - Account
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
    responses:
      200:
        description: Everything shared across this user's characters
      401:
        description: Missing, invalid or expired token
    """
    # NO slot parameter, deliberately. That absence is the fix.
    return account_payload(g.user["id"]), 200


@app.put("/api/account/bank")
@require_auth
def write_bank():
    """
    Replace the bank's contents
    ---
    tags:
      - Account
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [bank_inventory]
          properties:
            bank_inventory:
              type: array
              description: "Positional; null for an empty cell. At most 50 entries."
    responses:
      200:
        description: The account as stored
      400:
        description: Oversized array, or a malformed entry
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]
    _ensure_account(user_id)

    cells = payload.get("bank_inventory")
    if not isinstance(cells, list):
        return bad_request("bank_inventory must be an array")
    if len(cells) > BANK_CAPACITY:
        return bad_request("bank_inventory has %d entries, capacity is %d" % (len(cells), BANK_CAPACITY))

    parsed, error = _parse_positional_items(cells, "bank_inventory")
    if error is not None:
        return error

    db = get_db()
    db.execute("DELETE FROM bank_items WHERE user_id = ?", (user_id,))
    if parsed:
        db.executemany(
            "INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, ?, ?, ?)",
            [(user_id, position, item_id, quantity) for position, item_id, quantity in parsed],
        )
    db.commit()

    return account_payload(user_id), 200


@app.put("/api/account/lusions")
@require_auth
def write_lusions():
    """
    Set the account's lusion balance
    ---
    tags:
      - Account
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [lusions]
          properties:
            lusions: {type: integer, example: 40}
    responses:
      200:
        description: The account as stored
      400:
        description: Not a non-negative integer
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]
    _ensure_account(user_id)

    lusions = parse_stat(payload.get("lusions"))
    if lusions is None:
        return bad_request("lusions must be a non-negative integer")

    db = get_db()
    db.execute("UPDATE accounts SET lusions = ? WHERE user_id = ?", (lusions, user_id))
    db.commit()

    return account_payload(user_id), 200


@app.post("/api/bank/gold")
@require_auth
def move_bank_gold():
    """
    Move gold between a character's pocket and the shared bank
    ---
    tags:
      - Account
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, op, amount]
          properties:
            slot:   {type: integer, example: 0}
            op:     {type: string,  enum: [deposit, withdraw]}
            amount: {type: integer, example: 500}
    responses:
      200:
        description: Carried gold and the account afterwards
      400:
        description: Bad slot, op or amount, or not enough gold on the relevant side
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    # STILL TAKES A SLOT, and still an op rather than a replace.
    #
    # The slot is the CHARACTER whose pocket the gold comes out of - that half
    # is genuinely per-character. The bank half is not, and now comes from
    # accounts rather than from saves.bank_gold.
    #
    # It stays an op because this is the one operation on this whole server that
    # can be verified: it holds both balances, so it can enforce that the total
    # is conserved. Everything else here takes the client's word for what it has.
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    op = str(payload.get("op", "")).strip().lower()
    if op not in ("deposit", "withdraw"):
        return bad_request("op must be 'deposit' or 'withdraw'")

    amount = parse_stat(payload.get("amount"))
    if amount is None or amount <= 0:
        return bad_request("amount must be a positive integer")

    db = get_db()
    row = db.execute(
        "SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    account = _ensure_account(user_id)
    carried = int(row["gold"])
    banked = int(account["bank_gold"])

    if op == "deposit":
        if amount > carried:
            return bad_request("Cannot deposit %d - only %d carried." % (amount, carried))
        carried -= amount
        banked += amount
    else:
        if amount > banked:
            return bad_request("Cannot withdraw %d - only %d banked." % (amount, banked))
        banked -= amount
        carried += amount

    # Two tables now, so one transaction rather than one statement. sqlite3
    # opens a transaction on the first write and holds it until commit, so
    # there is still no moment where the gold exists in neither place.
    db.execute(
        "UPDATE saves SET gold = ?, updated_at = ? WHERE user_id = ? AND slot = ?",
        (carried, int(time.time()), user_id, slot),
    )
    db.execute("UPDATE accounts SET bank_gold = ? WHERE user_id = ?", (banked, user_id))
    db.commit()

    result = account_payload(user_id)
    result["slot"] = slot
    result["carried_gold"] = carried
    return result, 200


# =============================================================================
# COMBAT - KILL REWARDS
# =============================================================================
#
# -----------------------------------------------------------------------------
# WHAT THIS CHANGES, AND WHAT IT DOES NOT
# -----------------------------------------------------------------------------
# Before: BaseEnemy._die() granted the XP, rolled the loot and spawned the bag,
# all on the player's machine. A modified client could award itself every pet in
# the game.
#
# After: the client reports WHICH enemy died. The server decides what that is
# worth, rolls the loot with its own entropy, applies the level-ups, and commits
# the result before the client learns any of it.
#
# THIS IS HALF OF A TWO-PART CHANGE AND IS NOT YET A SECURITY FIX ON ITS OWN.
# The server now knows what a player SHOULD have earned, but PUT /api/save and
# PUT /api/player/status still accept a client-asserted level, xp and inventory.
# A modified client can still simply declare itself level 60 holding six pets.
#
# The second half is refusing those assertions and treating the server's own
# record as authoritative - and that half is only POSSIBLE once this one exists,
# because until now the server had no independent record to compare against. It
# is worth being plain about that ordering rather than shipping this and calling
# combat secured.
#
# WHAT IT DOES CLOSE TODAY: the loot roll itself. The 1-in-864 slime pet is now
# decided by the server's SystemRandom, which the client cannot observe, predict
# or re-run. No amount of patching the game can make that roll come up more
# often than it should.
# -----------------------------------------------------------------------------

# A TOKEN BUCKET, NOT A MINIMUM GAP.
#
# This was "one kill per 0.35 seconds", and it refused legitimate kills within
# an hour of shipping:
#
#     [KILL] bushmage refused — Kills are limited to one per 0.35s.
#     [KILL] poisonslimesmall refused — Kills are limited to one per 0.35s.
#
# A large poison slime splits into four smalls. An area attack kills several
# enemies in the same frame. Real combat is BURSTY, and no minimum gap can ever
# allow a burst - that is what a minimum gap is for. The player lost those
# rewards, which is the worst possible failure for a limit that cannot prove
# anything in the first place.
#
# A bucket separates the two shapes. Twenty tokens covers any burst a player can
# actually produce: a slime split, an aura wipe, a slashwave through a group.
# One token per second refills it, so sustained farming is capped at sixty kills
# a minute - roughly double the fastest honest rate observed - while a script
# asking for ten thousand kills now needs about three hours and shows up plainly
# in the data.
#
# WHAT THIS IS NOT: proof. The server cannot tell a real kill from a claimed
# one; it has no idea where anything is. This caps the rate and nothing more.
# The real answer is the server owning which enemies exist and having handed
# this client that one.
# SIZED FOR A GAME THAT THROWS CROWDS AT YOU. Darza's Dominion - one of this
# project's stated inspirations - puts dozens of enemies on screen and expects
# you to clear them without pausing, and Elusion is aiming at the same feel. A
# bucket that a good fight can empty is a bucket that punishes playing well.
#
# Fifty covers any burst a screen can hold. Five a second sustained is three
# hundred kills a minute, comfortably above the fastest honest rate and still
# bounded: a script wanting ten thousand kills needs half an hour rather than a
# few seconds, which is the difference between an exploit and a chore.
KILL_BUCKET_CAPACITY = 50.0
KILL_TOKENS_PER_SECOND = 5.0


@app.post("/api/combat/kill")
@require_auth
def combat_kill():
    """
    Report a kill and receive its rewards
    ---
    tags:
      - Combat
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, enemy_id]
          properties:
            slot:     {type: integer, example: 0}
            enemy_id: {type: string,  example: poisonslimesmall}
    responses:
      200:
        description: XP granted, level-ups applied, and whatever the loot roll produced
      400:
        description: Bad slot, unknown enemy_id, or an enemy that awards nothing
      404:
        description: That slot is empty
      429:
        description: Kills are arriving faster than the cooldown allows
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    enemy_id = str(payload.get("enemy_id", "")).strip()
    if not enemy_id or len(enemy_id) > 64:
        return bad_request("enemy_id must be 1-64 characters")

    enemy = gamedata.ENEMIES.get(enemy_id)
    if enemy is None:
        # Deliberately names the id back. This is not user input in the hostile
        # sense - it is the client and the server disagreeing about the roster,
        # which in practice means gamedata.json is stale on one side. Saying
        # which id was not found is the difference between a five-minute fix
        # and an afternoon.
        return bad_request("Unknown enemy_id '%s'. Is gamedata.json current?" % enemy_id)

    # The large poison slime is the case this exists for: it never dies, it
    # splits, and everything it is worth walks away as four smalls. A client
    # reporting it as a kill is either broken or lying, and either way there is
    # nothing to pay.
    if not enemy.get("grants_rewards", True):
        return bad_request("'%s' awards no rewards - it cannot be killed for profit." % enemy_id)

    db = get_db()
    row = db.execute(
        "SELECT class_id, level, xp, xp_to_next, last_kill_at, kill_tokens FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    now_ms = int(time.time() * 1000)

    # Refill first, then spend. last_kill_at of 0 means this character has never
    # reported a kill, and the elapsed time since 1970 would overfill the bucket
    # - min() against the capacity handles that without a special case.
    elapsed_seconds = max(now_ms - int(row["last_kill_at"]), 0) / 1000.0
    tokens = min(
        KILL_BUCKET_CAPACITY,
        float(row["kill_tokens"]) + elapsed_seconds * KILL_TOKENS_PER_SECOND,
    )

    if tokens < 1.0:
        # 429 rather than 400: nothing about the request is malformed, it simply
        # arrived faster than the bucket allows. An honest client can read that
        # and retry; a 400 would tell it to give up.
        return {
            "error": "Too Many Requests",
            "message": "Kills are arriving faster than %g per second." % KILL_TOKENS_PER_SECOND,
        }, 429

    tokens -= 1.0

    # ---- everything above this line is validation; everything below commits --

    rewards = gamedata.roll_kill_rewards(enemy_id)

    level, xp, xp_to_next, levels_gained = gamedata.apply_xp(
        int(row["level"]), int(row["xp"]), int(row["xp_to_next"]), rewards["xp"]
    )

    # A LEVEL-UP MOVES THE MAXIMA WITH IT.
    #
    # Without this the level changed here and max_hp did not, so a character who
    # levelled from a kill carried the previous level's maximum until something
    # happened to write a status - and /api/player/status GET returns the stored
    # row, so the player would simply see the wrong number.
    #
    # Found by a test asserting max_hp against the curve rather than against a
    # hardcoded 180: the moment a kill-burst test started levelling the
    # character up, the stale value stopped matching.
    derived = gamedata.max_stats_for(row["class_id"], level) if levels_gained else None

    # ONE UPDATE. The XP, the level and the cooldown stamp move together or not
    # at all - the same reasoning as the bank gold transfer. Two statements
    # would leave a window where a crash could bank the XP and lose the level,
    # or stamp the cooldown for a kill that was never paid.
    if derived is None:
        db.execute(
            """
            UPDATE saves
               SET level = ?, xp = ?, xp_to_next = ?,
                   last_kill_at = ?, kill_tokens = ?, updated_at = ?
             WHERE user_id = ? AND slot = ?
            """,
            (level, xp, xp_to_next, now_ms, tokens, int(time.time()), user_id, slot),
        )
    else:
        # One statement, so the level and the maxima it implies can never be
        # stored apart from each other.
        db.execute(
            """
            UPDATE saves
               SET level = ?, xp = ?, xp_to_next = ?,
                   max_hp = ?, max_mana = ?, max_stamina = ?,
                   last_kill_at = ?, kill_tokens = ?, updated_at = ?
             WHERE user_id = ? AND slot = ?
            """,
            (level, xp, xp_to_next,
             derived["max_hp"], derived["max_mana"], derived["max_stamina"],
             now_ms, tokens, int(time.time()), user_id, slot),
        )
    db.commit()

    # THE BAG IS STORED, and its id goes back with the contents. The client
    # spawns a node to render it, but the node is a picture: taking anything out
    # of it goes through /api/loot/take, against these rows.
    #
    # An empty bag_id means nothing dropped, and the client spawns nothing.
    #
    # `stored` is the roll with a position stamped on every entry - which is
    # what the client renders into its grid and what it sends back to take
    # anything out. The roll itself is never returned: position is not a detail
    # the two sides should be inferring separately.
    bag_id, stored = _create_loot_bag(user_id, slot, enemy_id, rewards["contents"])

    return {
        "bag_id": bag_id,
        "enemy_id": enemy_id,
        "xp_gained": rewards["xp"],
        "attack_xp_gained": rewards["attack_xp"],
        "levels_gained": levels_gained,
        "level": level,
        "xp": xp,
        "xp_to_next": xp_to_next,
        "pet_won": rewards["pet_won"],
        "contents": stored,
    }, 200


# =============================================================================
# MODERATION
# =============================================================================
#
# Every route here is @require_role("mod") at the door and can_act_on() inside,
# and the two do different jobs: the decorator says whether you are staff at
# all, can_act_on() says whether this particular person is within your reach.
#
# Refusals are 404, and the same 404 whether the account does not exist or
# simply outranks you. A mod who could tell those apart could map out who is
# above them by guessing names.

# How long a mod may mute someone for. Anything longer, and anything permanent,
# needs a dev or the owner.
#
# The point is not that thirty days is special. It is that a permanent removal
# and a timeout are different decisions, and the person having a bad night at
# 2am should only be able to make the reversible one.
MAX_MOD_BAN_DAYS = 30


@app.post("/api/staff/ban")
@require_auth
@require_role("mod")
def ban_account():
    """
    Ban an account
    ---
    tags:
      - Moderation
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [username, reason]
          properties:
            username: {type: string}
            reason:   {type: string}
            days:     {type: integer, description: "Omit for a permanent ban."}
    responses:
      200:
        description: The ban as stored
      400:
        description: Missing reason, or a days value out of range
      403:
        description: A mod attempting a permanent or over-long ban
      404:
        description: No such account, or one you may not act on
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}

    target, error = _moderation_target(payload)
    if error is not None:
        return error

    reason = str(payload.get("reason", "")).strip()
    if not reason or len(reason) > 500:
        # REQUIRED, not optional. A ban with no reason is one nobody can review
        # later, including the person who issued it.
        return bad_request("reason must be 1-500 characters")

    raw_days = payload.get("days")
    permanent = raw_days is None

    expires_at = None
    if not permanent:
        days = parse_stat(raw_days)
        if days is None or days < 1 or days > 3650:
            return bad_request("days must be an integer 1-3650, or omitted for a permanent ban")
        expires_at = int(time.time()) + days * 86400

    # A PERMANENT BAN IS A HIGHER PERMISSION THAN A TEMPORARY ONE.
    if not role_at_least(g.user, "dev"):
        if permanent:
            return {
                "error": "Forbidden",
                "message": "Only a dev or the owner can ban permanently.",
            }, 403
        if parse_stat(raw_days) > MAX_MOD_BAN_DAYS:
            return {
                "error": "Forbidden",
                "message": "A mod may ban for at most %d days." % MAX_MOD_BAN_DAYS,
            }, 403

    db = get_db()
    now = int(time.time())

    db.execute(
        """
        UPDATE users
           SET is_banned = 1, ban_expires_at = ?, ban_reason = ?,
               banned_by = ?, banned_at = ?
         WHERE id = ?
        """,
        (expires_at, reason, g.user["username"], now, target["id"]),
    )

    # IN THE SAME TRANSACTION. Crossing the name off the list does nothing
    # about the person already inside - a banned player holding a live token
    # keeps playing until it expires, which on this server is thirty days.
    db.execute("DELETE FROM sessions WHERE user_id = ?", (target["id"],))

    log_staff_action(
        g.user, "ban", target["username"], target["id"],
        "permanent: %s" % reason if permanent else "%d days: %s" % (parse_stat(raw_days), reason),
    )
    db.commit()

    return {
        "username": target["username"],
        "banned": True,
        "permanent": permanent,
        "expires_at": expires_at,
        "reason": reason,
        "banned_by": g.user["username"],
    }, 200


@app.post("/api/staff/unban")
@require_auth
@require_role("mod")
def unban_account():
    """
    Lift a ban
    ---
    tags:
      - Moderation
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [username]
          properties:
            username: {type: string}
    responses:
      200:
        description: The account is no longer banned
      404:
        description: No such account, or one you may not act on
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}

    target, error = _moderation_target(payload)
    if error is not None:
        return error

    db = get_db()
    db.execute(
        """
        UPDATE users
           SET is_banned = 0, ban_expires_at = NULL, ban_reason = '',
               banned_by = '', banned_at = 0
         WHERE id = ?
        """,
        (target["id"],),
    )
    log_staff_action(g.user, "unban", target["username"], target["id"])
    db.commit()

    # Idempotent on purpose: unbanning someone who is not banned is a 200 that
    # changed nothing. The caller wanted them not-banned, and they are not.
    return {"username": target["username"], "banned": False}, 200


@app.put("/api/staff/role")
@require_auth
@require_role("mod")
def set_account_role():
    """
    Change an account's rank
    ---
    tags:
      - Moderation
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [username, role]
          properties:
            username: {type: string}
            role:     {type: string, description: "player, mod or dev"}
    responses:
      200:
        description: The rank as stored
      400:
        description: Not a settable rank
      403:
        description: Granting a rank at or above your own
      404:
        description: No such account, or one you may not act on
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}

    target, error = _moderation_target(payload)
    if error is not None:
        return error

    new_role = str(payload.get("role", "")).strip().lower()
    if new_role not in SETTABLE_ROLES:
        return bad_request(
            "role must be one of: %s. 'owner' comes from the environment."
            % ", ".join(SETTABLE_ROLES)
        )

    # YOU CANNOT GRANT A RANK AT OR ABOVE YOUR OWN.
    #
    # Without this, one dev promotes another dev, or promotes a player to dev,
    # and a single compromised staff account spreads sideways for as long as
    # nobody is looking. Only the owner makes a dev.
    if ROLES.index(new_role) >= ROLES.index(role_for(g.user)):
        return {
            "error": "Forbidden",
            "message": "You cannot grant a rank at or above your own.",
        }, 403

    was = target["role"]

    db = get_db()
    db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, target["id"]))
    log_staff_action(
        g.user, "role", target["username"], target["id"], "%s -> %s" % (was, new_role)
    )
    db.commit()

    return {"username": target["username"], "role": new_role, "was": was}, 200


@app.get("/api/staff/users")
@require_auth
@require_role("mod")
def list_accounts():
    """
    Every account, with rank and ban state
    ---
    tags:
      - Moderation
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
    responses:
      200:
        description: The account list
      401:
        description: Missing, invalid or expired token
    """
    rows = get_db().execute(
        "SELECT * FROM users ORDER BY id"
    ).fetchall()

    accounts = []
    for row in rows:
        ban = ban_state(row)
        accounts.append({
            "id": row["id"],
            "username": row["username"],
            "role": role_for(row),
            "banned": ban is not None,
            "ban": ban,
            # Whether YOU can act on this person, so a client can grey out the
            # buttons rather than offering them and being refused.
            "actionable": can_act_on(g.user, row),
        })

    return {"accounts": accounts}, 200


@app.get("/api/status")
def server_status():
    """
    Server health and maintenance window
    ---
    tags:
      - Status
    responses:
      200:
        description: The server is up; message and back_at describe any planned downtime
    """
    # NO @require_auth, deliberately. The login screen needs to be able to ask
    # "are you there?" before anyone has logged in, and a 401 is not an answer
    # to that question.
    #
    # WHY THIS EXISTS: the client currently infers the difference between "your
    # internet is down" and "the host's server is down" from an HTTP failure
    # code, and that inference is unreliable - a dead port on localhost reports
    # a timeout rather than a refused connection, and a timeout looks identical
    # either way. It cannot be inferred. It has to be told.
    #
    # A reachable /api/status also means a scheduled restart can ANNOUNCE
    # itself rather than surfacing as an error. Downtime the player was warned
    # about is maintenance; the same downtime unannounced is a broken game.
    return {
        "online": True,
        "gamedata_schema": gamedata.EXPECTED_SCHEMA,
        "items": len(gamedata.ITEMS),
        "enemies": len(gamedata.ENEMIES),
        # Set these by hand before a planned restart. back_at is an RFC3339
        # timestamp the client can render in the player's own timezone; null
        # means no downtime is planned.
        "message": "",
        "back_at": None,
    }, 200


# =============================================================================
# CHARACTER - CARRY INVENTORY AND SKILLS
# =============================================================================
#
# The two things a character is made of that the server could not hold.
#
# `saves` already covers identity and vitals - class, name, level, area, hp,
# mana, stamina, gold, xp. `bank_items` covers storage. Between them they still
# could not represent a character, because the BACKPACK and every SKILL lived
# only in the client's local file. A server that cannot describe a character
# cannot be the authority on one, so this is the prerequisite for moving saves
# off the player's disk at all.
#
# WHY carry_items IS KEYED ON POSITION AND bank_items IS NOT
# ---------------------------------------------------------
# The bank is a set: forty stacks, order irrelevant, so (user_id, slot, item_id)
# is the natural key and topping up a stack is an upsert.
#
# The backpack is a GRID. The player drags a potion into the third cell and it
# stays in the third cell; two different stacks of the same item in two cells is
# a legal, ordinary state. Keying on item_id would silently merge them and shuffle
# everything else, which the player would experience as their bag rearranging
# itself on login. So the key is (user_id, slot, position) - one item per cell,
# which is exactly the invariant the inventory UI already enforces.
#
# WHY BOTH ENDPOINTS REPLACE RATHER THAN PATCH
# --------------------------------------------
# The bank has per-operation endpoints because a deposit is a discrete act the
# server can reason about. An inventory is not: it is the result of picking
# things up, dropping them, dragging them around and using them, and the client
# always holds the whole picture. Twenty cells is small enough to send in full,
# and a whole-array replace has no partial-failure state to reconcile.

# The client's backpack is twenty cells. Kept here rather than inferred from
# whatever arrives so an oversized array is a 400 rather than a slow leak into
# the database.
INVENTORY_CAPACITY = 20

# Skills a character can have. An unknown id is refused rather than stored,
# because a typo'd skill name would otherwise sit in the table forever, be
# returned on every load, and never match anything the client looks for.
# Spelled to match characterdata.gd's SKILL_GROWTH_FACTORS exactly. It says
# "defense", not "defence" - and a skill name that does not match is a level the
# player earned that the server refuses on every save.
VALID_SKILLS = {"attack", "magic", "agility", "defense", "fishing", "cooking"}


# The carry_items and skills tables are created in init_db()'s schema block
# alongside every other table, NOT here. init_db() runs at import time, at the
# top of this file - a helper defined further down does not exist yet when it
# is called, which is exactly the NameError this replaced.


# The most of anything a single cell may hold when the server does not
# recognise the item. Known items are capped at their own max_stack instead.
#
# 9999 matches the largest real stack in the game (a gold pile). It is not a
# guess at what is reasonable, it is the biggest thing the game itself makes.
QUANTITY_CEILING = 9999


def _stack_limit(item_id):
    """How many of item_id may sit in one cell."""
    definition = gamedata.ITEMS.get(item_id)
    if definition is None:
        return QUANTITY_CEILING
    if not definition.get("stackable", False):
        return 1
    return max(1, min(int(definition.get("max_stack", 1)), QUANTITY_CEILING))


def _parse_positional_items(cells, field):
    """
    Validate a positional item array - the backpack and the bank are the same
    shape and this is the only place that shape is checked.

    Returns (parsed, None) or (None, an error response). The WHOLE array is
    validated before the caller writes any of it: a bad entry at index 17 must
    not leave the first seventeen stored, because a half-saved bag with no error
    is worse than a refused write.
    """
    parsed = []
    for index, cell in enumerate(cells):
        if cell is None:
            continue
        if not isinstance(cell, dict):
            return None, bad_request("%s[%d] must be an object or null" % (field, index))

        item_id = str(cell.get("item_id", "")).strip()
        if not item_id or len(item_id) > 64:
            return None, bad_request("%s[%d].item_id must be 1-64 characters" % (field, index))

        raw_quantity = cell.get("quantity", 1)
        if isinstance(raw_quantity, bool):
            return None, bad_request("%s[%d].quantity must be a positive integer" % (field, index))
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            return None, bad_request("%s[%d].quantity must be a positive integer" % (field, index))
        if quantity <= 0:
            return None, bad_request("%s[%d].quantity must be a positive integer" % (field, index))

        # The ceiling, which "positive integer" alone does not give you. Without
        # it a client could declare a cell holding a billion potions and this
        # would store it: the table only checks quantity > 0.
        #
        # A known item is capped at its own max_stack, which is the rule the
        # game already plays by. An unknown one falls back to QUANTITY_CEILING,
        # because item ids are deliberately NOT validated against a list here -
        # that is what lets the game add an item without a matching server
        # deploy, and it is worth keeping.
        limit = _stack_limit(item_id)
        if quantity > limit:
            return None, bad_request(
                "%s[%d].quantity is %d, the most %s stacks to is %d"
                % (field, index, quantity, item_id, limit)
            )

        parsed.append((index, item_id, quantity))

    return parsed, None


def _slot_exists(user_id, slot):
    row = get_db().execute(
        "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    return row is not None


def inventory_payload(user_id, slot):
    """
    The backpack as a POSITIONAL ARRAY of length INVENTORY_CAPACITY, with null
    in every empty cell - not as a list of the items that happen to exist.

    The client's inventory is an array where index IS the grid cell, and
    returning a packed list would make the client responsible for rebuilding
    the gaps. It would get that right the first time and wrong the first time
    someone changed the capacity.
    """
    rows = get_db().execute(
        "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ? ORDER BY position",
        (user_id, slot),
    ).fetchall()

    cells = [None] * INVENTORY_CAPACITY
    for row in rows:
        position = int(row["position"])
        # Defensive: a row outside the current capacity means the capacity was
        # reduced after it was written. Drop it from the view rather than
        # crashing on the index - the next write prunes it for real.
        if 0 <= position < INVENTORY_CAPACITY:
            cells[position] = {"item_id": row["item_id"], "quantity": int(row["quantity"])}

    return cells


def skills_payload(user_id, slot):
    rows = get_db().execute(
        "SELECT skill_id, level, xp FROM skills WHERE user_id = ? AND slot = ? ORDER BY skill_id",
        (user_id, slot),
    ).fetchall()
    return {row["skill_id"]: {"level": int(row["level"]), "xp": int(row["xp"])} for row in rows}


@app.get("/api/character")
@require_auth
def read_character():
    """
    Everything one character is, in a single call
    ---
    tags:
      - Character
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: query
        name: slot
        type: integer
        required: true
    responses:
      200:
        description: Identity, vitals, backpack and skills together
      400:
        description: Bad slot
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    # ONE CALL, NOT FOUR. Loading a character used to mean status, then
    # inventory, then skills, then bank - four round trips on a screen the
    # player is staring at. They are all keyed on the same (user_id, slot) and
    # none of them is useful without the others.
    slot = parse_slot(request.args.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    user_id = g.user["id"]
    status = status_payload(user_id, slot)
    if status is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    row = get_db().execute(
        "SELECT class_id, name, area, active_pet_id, bank_gold FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()

    return {
        "slot": slot,
        "class_id": row["class_id"],
        "name": row["name"],
        "area": row["area"],
        "active_pet_id": row["active_pet_id"],
        "status": status,
        "inventory": inventory_payload(user_id, slot),
        "skills": skills_payload(user_id, slot),
    }, 200


@app.put("/api/character/inventory")
@require_auth
def write_inventory():
    """
    Replace a character's backpack
    ---
    tags:
      - Character
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, inventory]
          properties:
            slot: {type: integer, example: 0}
            inventory:
              type: array
              description: "Positional; null for an empty cell. At most 20 entries."
    responses:
      200:
        description: The backpack as stored
      400:
        description: Bad slot, oversized array, or a malformed entry
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    if not _slot_exists(user_id, slot):
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    cells = payload.get("inventory")
    if not isinstance(cells, list):
        return bad_request("inventory must be an array")
    if len(cells) > INVENTORY_CAPACITY:
        return bad_request("inventory has %d entries, capacity is %d" % (len(cells), INVENTORY_CAPACITY))

    # THE SHARED VALIDATOR, not a second copy of it. This loop used to be
    # written out here as well as in _parse_positional_items(), twenty identical
    # lines in two places - so a rule added to one of them (the stack ceiling
    # was exactly that) would silently not apply to the other.
    #
    # It validates the whole array before the caller writes any of it: a bad
    # entry at index 17 must not leave the first seventeen stored.
    items, error = _parse_positional_items(cells, "inventory")
    if error is not None:
        return error

    parsed = [(user_id, slot, index, item_id, quantity)
              for index, item_id, quantity in items]

    db = get_db()
    # DELETE then INSERT, in one transaction. A replace has no upsert form: cells
    # the client cleared have to disappear, and an upsert would leave them.
    # sqlite3 opens a transaction implicitly on the first write and holds it
    # until commit, so the table is never observably empty.
    db.execute("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (user_id, slot))
    if parsed:
        db.executemany(
            "INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, ?, ?, ?, ?)",
            parsed,
        )
    db.commit()

    return {"slot": slot, "inventory": inventory_payload(user_id, slot)}, 200


@app.put("/api/character/skills")
@require_auth
def write_skills():
    """
    Replace a character's skills
    ---
    tags:
      - Character
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [slot, skills]
          properties:
            slot: {type: integer, example: 0}
            skills:
              type: object
              description: '{"attack": {"level": 12, "xp": 340}, ...}'
    responses:
      200:
        description: The skills as stored
      400:
        description: Bad slot, unknown skill, or a malformed level/xp
      404:
        description: That slot is empty
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    if not _slot_exists(user_id, slot):
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    skills = payload.get("skills")
    if not isinstance(skills, dict):
        return bad_request("skills must be an object")

    parsed = []
    for skill_id, values in skills.items():
        name = str(skill_id).strip().lower()
        if name not in VALID_SKILLS:
            # Refused rather than ignored. A silently dropped skill is a level
            # the player earned and cannot see, with nothing anywhere to say so.
            return bad_request("Unknown skill '%s'. Known skills: %s" % (name, ", ".join(sorted(VALID_SKILLS))))

        if not isinstance(values, dict):
            return bad_request("skills.%s must be an object with level and xp" % name)

        level = parse_stat(values.get("level", 1))
        xp = parse_stat(values.get("xp", 0))
        if level is None or level < 1:
            return bad_request("skills.%s.level must be a positive integer" % name)
        if xp is None:
            return bad_request("skills.%s.xp must be a non-negative integer" % name)

        parsed.append((user_id, slot, name, level, xp))

    db = get_db()
    db.execute("DELETE FROM skills WHERE user_id = ? AND slot = ?", (user_id, slot))
    if parsed:
        db.executemany(
            "INSERT INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, ?, ?, ?, ?)",
            parsed,
        )
    db.commit()

    return {"slot": slot, "skills": skills_payload(user_id, slot)}, 200


# =============================================================================
# LOOT BAGS
# =============================================================================
#
# THE LAST THING THE CLIENT GETS TO ASSERT.
#
# /api/combat/kill already decides what drops. But the bag then existed only in
# the client's world: it spawned a node, the player walked over it, items landed
# in the local inventory, and the next save simply TOLD the server what was now
# being carried. The server rolled a potion and had no idea whether you picked it
# up, dropped it, or invented forty more.
#
# That is why `gold` is still on the client-asserted list next to
# SERVER_OWNED_STATS. Closing it means the bag has to exist HERE:
#
#   1. A kill that drops something creates a bag with an unguessable id.
#   2. The client renders it, but the contents it renders are a copy.
#   3. Taking an item is a REQUEST. The server checks the bag is yours, that it
#      still holds that item, and moves it into your backpack or your purse.
#
# After this, gold and carried items only change through an endpoint that
# verified where they came from.
#
# WHY POSITION-KEYED, LIKE EVERY OTHER CONTAINER HERE
# ---------------------------------------------------
# Same reason as carry_items and bank_items: "take the item in slot 2" is a
# request the server can answer exactly once, whereas "take a smallhealthpotion"
# is ambiguous when the bag holds two stacks of them, and a client that sends it
# twice would be asking for a duplication bug.

# How long the server will honour a bag after it is created.
#
# Deliberately far longer than the client's LOOT_BAG_DESPAWN_SECONDS (20). The
# client despawning the node is a display decision; if the two disagree the
# player should lose the bag to the ANIMATION, never to a 410 from a server that
# expired it a moment early. This exists to stop bags accumulating forever, not
# to enforce the despawn.
LOOT_BAG_TTL_SECONDS = 600

# Matches BANK_CAPACITY's role for the backpack. The client's grid is 20 cells.
CARRY_CAPACITY = INVENTORY_CAPACITY

# The loot panel's grid, in cells. lootbaginventory.gd's LOOT_SIZE.
#
# The position IS the grid cell on both sides - that is what lets the client
# send "take cell 2" and mean the thing the player is looking at. A bag rolled
# with more entries than the panel can show would put loot behind a cell that
# does not exist, and the player would never be able to ask for it. Rolls are
# currently capped at five (one gold pile, three item slots, one pet), so this
# is a guard against a future enemy config, not a live condition.
LOOT_BAG_CAPACITY = 6

# The item_id the client uses for the lusion pile - lootbaginventory.gd's
# LUSION_ITEM_ID. There is no constant for it in gamedata.json because the game
# has never needed to name it; the server does, because it is what a duplicate
# pet turns into.
LUSIONS_ITEM_ID = "lusions"


def _create_loot_bag(user_id, slot, enemy_id, contents):
    """
    Store a rolled bag and return (bag_id, stored_contents).

    stored_contents carries the POSITION of every entry, because that position
    is the only thing /api/loot/take accepts. Returning the roll unstamped and
    letting the client infer position from array order would work right up until
    something reordered or dropped an entry between here and there - and the
    failure would be the client asking for the wrong item, silently.

    bag_id is secrets.token_urlsafe, not a row counter: a sequential id would
    let a client ask for bag 4,102 and find out what someone else killed.
    """
    if not contents:
        return "", []

    if len(contents) > LOOT_BAG_CAPACITY:
        # Logged rather than raised. A player mid-fight should not lose a kill
        # because an enemy was configured to drop seven things; they lose the
        # overflow, and the log says why.
        app.logger.warning(
            "loot: %s rolled %d entries, truncating to the panel's %d cells",
            enemy_id, len(contents), LOOT_BAG_CAPACITY,
        )
        contents = contents[:LOOT_BAG_CAPACITY]

    stored = [
        {"position": position, "item_id": entry["item_id"], "quantity": int(entry["quantity"])}
        for position, entry in enumerate(contents)
    ]

    bag_id = secrets.token_urlsafe(16)
    now = int(time.time())

    db = get_db()
    db.execute(
        "INSERT INTO loot_bags (bag_id, user_id, slot, enemy_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (bag_id, user_id, slot, enemy_id, now),
    )
    db.executemany(
        "INSERT INTO loot_bag_items (bag_id, position, item_id, quantity) VALUES (?, ?, ?, ?)",
        [(bag_id, e["position"], e["item_id"], e["quantity"]) for e in stored],
    )

    # Opportunistic cleanup. Bags are small and expire on their own terms, so
    # this runs here rather than on a schedule - a player who is killing things
    # is exactly the player generating rows worth clearing.
    db.execute(
        "DELETE FROM loot_bags WHERE created_at < ?", (now - LOOT_BAG_TTL_SECONDS,)
    )

    db.commit()
    return bag_id, stored


def _owns_item(user_id, slot, item_id):
    """
    True when this account already holds item_id in the backpack or the bank.

    Only pets ask. The bank is account-scoped and the backpack is per-character,
    which is deliberate: a pet is a collectible, and having caught one on your
    warrior should stop it dropping again for your mage - which is exactly what
    the bank half covers, since that is where a collection ends up.
    """
    if get_db().execute(
        "SELECT 1 FROM carry_items WHERE user_id = ? AND slot = ? AND item_id = ? LIMIT 1",
        (user_id, slot, item_id),
    ).fetchone():
        return True
    return get_db().execute(
        "SELECT 1 FROM bank_items WHERE user_id = ? AND item_id = ? LIMIT 1",
        (user_id, item_id),
    ).fetchone() is not None


def _add_to_backpack(user_id, slot, item_id, quantity):
    """
    Put quantity of item_id into the backpack the way the CLIENT would, and
    return the cells touched - or None when it does not fit.

    TOPS UP EXISTING STACKS FIRST, then fills empty cells. That is not a nicety:
    the response hands `inventory` back as the authoritative layout, and the
    client applies it. If this dropped every pickup into the lowest free cell
    while InventoryContainer.add_stack_partial() merged onto a part-used stack,
    the two would lay the same bag out differently and the player would watch
    their potions split across cells on every loot.

    Writes nothing unless the whole quantity fits. A half-completed pickup is
    the shape of bug that ends with an item in neither the bag nor the bag.
    """
    definition = gamedata.ITEMS.get(item_id, {})
    stackable = bool(definition.get("stackable", False))
    max_stack = int(definition.get("max_stack", 1)) if stackable else 1
    if max_stack < 1:
        max_stack = 1

    rows = get_db().execute(
        "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ? ORDER BY position",
        (user_id, slot),
    ).fetchall()

    occupied = {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in rows}

    remaining = int(quantity)
    writes = []  # (position, item_id, new_quantity)

    if stackable:
        for position in sorted(occupied):
            if remaining <= 0:
                break
            held_id, held_qty = occupied[position]
            if held_id != item_id or held_qty >= max_stack:
                continue
            room = max_stack - held_qty
            moved = min(room, remaining)
            writes.append((position, item_id, held_qty + moved))
            remaining -= moved

    for position in range(CARRY_CAPACITY):
        if remaining <= 0:
            break
        if position in occupied:
            continue
        moved = min(max_stack, remaining)
        writes.append((position, item_id, moved))
        occupied[position] = (item_id, moved)
        remaining -= moved

    if remaining > 0:
        return None

    db = get_db()
    for position, written_id, written_qty in writes:
        db.execute(
            """
            INSERT INTO carry_items (user_id, slot, position, item_id, quantity)
                 VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, slot, position)
              DO UPDATE SET item_id = excluded.item_id, quantity = excluded.quantity
            """,
            (user_id, slot, position, written_id, written_qty),
        )

    return [position for position, _, _ in writes]


@app.post("/api/loot/take")
@require_auth
def take_loot():
    """
    Take one item out of a loot bag
    ---
    tags:
      - Loot
    consumes:
      - application/json
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [bag_id, position]
          properties:
            bag_id:   {type: string,  example: k3Jx9_QpZ2mNvRt1}
            position: {type: integer, example: 0}
    responses:
      200:
        description: What was taken, and the state it landed in
      400:
        description: Bad bag_id or position
      404:
        description: No such bag, not yours, or that cell is already empty
      409:
        description: Backpack full - the item stays in the bag
      410:
        description: The bag has expired
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    bag_id = str(payload.get("bag_id", "")).strip()
    if not bag_id or len(bag_id) > 64:
        return bad_request("bag_id must be 1-64 characters")

    # Bounded by the BAG's capacity, not the backpack's. They were the same
    # number by accident, and a cell index from a 6-cell grid validated against
    # a 20-cell one is a check that reads as if it is doing something.
    position = parse_stat(payload.get("position"))
    if position is None or position >= LOOT_BAG_CAPACITY:
        return bad_request("position must be an integer 0-%d" % (LOOT_BAG_CAPACITY - 1))

    db = get_db()

    # SCOPED TO user_id IN THE QUERY ITSELF, not checked afterwards. A bag that
    # belongs to someone else and a bag that does not exist return exactly the
    # same 404 - there is nothing to learn from asking.
    bag = db.execute(
        "SELECT slot, created_at FROM loot_bags WHERE bag_id = ? AND user_id = ?",
        (bag_id, user_id),
    ).fetchone()
    if bag is None:
        return {"error": "Not Found", "message": "No such loot bag."}, 404

    if int(bag["created_at"]) < int(time.time()) - LOOT_BAG_TTL_SECONDS:
        db.execute("DELETE FROM loot_bags WHERE bag_id = ?", (bag_id,))
        db.commit()
        return {"error": "Gone", "message": "That loot bag has expired."}, 410

    item = db.execute(
        "SELECT item_id, quantity FROM loot_bag_items WHERE bag_id = ? AND position = ?",
        (bag_id, position),
    ).fetchone()
    if item is None:
        # Already taken, or never there. Same answer either way, and it is the
        # answer that makes a duplicate request harmless: the second one finds
        # nothing and changes nothing.
        return {"error": "Not Found", "message": "Nothing in that slot of the bag."}, 404

    slot = int(bag["slot"])
    item_id = item["item_id"]
    quantity = int(item["quantity"])
    definition = gamedata.ITEMS.get(item_id, {})
    kind = definition.get("type_name", "")

    # WHAT WAS IN THE BAG vs WHAT THE PLAYER ACTUALLY GETS. Those are the same
    # thing for everything except a pet you already own, and the client needs
    # both: item_id/quantity to know which cell to clear, granted_* to know what
    # to say in the notice.
    result = {"bag_id": bag_id, "position": position, "item_id": item_id, "quantity": quantity}
    granted_id = item_id
    granted_qty = quantity

    # A DUPLICATE PET BECOMES LUSIONS, AND THAT DECISION MOVED HERE.
    #
    # lootbaginventory.gd used to make it, in _load_contents(), by checking the
    # local inventory and the local bank. Both of those are now views of rows
    # this process owns, so the client was checking a copy to decide what a pet
    # was worth - and a client that decides what it is owed is the thing this
    # whole endpoint exists to stop.
    #
    # The panel still DISPLAYS the substitution so the player sees lusions in
    # the bag rather than a pet that turns into lusions. That is cosmetic. This
    # is the grant.
    if kind == "PET" and _owns_item(user_id, slot, item_id):
        kind = "CURRENCY"
        granted_id = LUSIONS_ITEM_ID
        granted_qty = int(gamedata.CONSTANTS.get("dupe_pet_lusions", 20))
        result["duplicate_pet"] = True

    if kind == "CURRENCY":
        # CURRENCY RESOLVES INTO A BALANCE RATHER THAN A BACKPACK CELL. This is
        # the whole point of the exercise: gold entering the game now goes
        # through a statement the server wrote, against a bag the server rolled.
        if granted_id in (CONSTANTS_GOLD_SMALL, CONSTANTS_GOLD_LARGE):
            db.execute(
                "UPDATE saves SET gold = gold + ?, updated_at = ? WHERE user_id = ? AND slot = ?",
                (granted_qty, int(time.time()), user_id, slot),
            )
            result["credited"] = "gold"
        else:
            # Lusions, and anything else account-scoped that shows up later.
            _ensure_account(user_id)
            db.execute(
                "UPDATE accounts SET lusions = lusions + ? WHERE user_id = ?",
                (granted_qty, user_id),
            )
            result["credited"] = "lusions"
    else:
        written = _add_to_backpack(user_id, slot, granted_id, granted_qty)
        if written is None:
            # 409, and the item stays where it is. Refusing beats dropping it on
            # the floor: the player can make room and ask again. Nothing was
            # written - _add_to_backpack is all-or-nothing.
            return {
                "error": "Conflict",
                "message": "Your backpack is full (%d slots)." % CARRY_CAPACITY,
            }, 409

        result["credited"] = "inventory"
        result["carry_positions"] = written

    result["granted_item_id"] = granted_id
    result["granted_quantity"] = granted_qty

    # Removed only after it has landed somewhere. If the insert above had failed
    # the transaction carries the delete with it, so an item cannot evaporate
    # between the two.
    db.execute(
        "DELETE FROM loot_bag_items WHERE bag_id = ? AND position = ?", (bag_id, position)
    )

    remaining = db.execute(
        "SELECT COUNT(*) FROM loot_bag_items WHERE bag_id = ?", (bag_id,)
    ).fetchone()[0]
    if remaining == 0:
        db.execute("DELETE FROM loot_bags WHERE bag_id = ?", (bag_id,))

    db.commit()

    result["bag_empty"] = remaining == 0

    # THE BALANCES GO BACK AS TOTALS, NOT AS THE DELTA THAT WAS JUST APPLIED.
    #
    # The client still pushes `gold` on every save, so a client that added the
    # delta to its own figure and got it wrong once would overwrite this row
    # with the wrong number on the very next save - and the loss would look like
    # nothing at all. Handing back the balance it landed on means a client that
    # misses a response is corrected by the next one it does get.
    result["status"] = status_payload(user_id, slot)
    result["inventory"] = inventory_payload(user_id, slot)
    result["lusions"] = int(_ensure_account(user_id)["lusions"])
    return result, 200


@app.get("/api/loot/bag")
@require_auth
def read_loot_bag():
    """
    What is still in a loot bag
    ---
    tags:
      - Loot
    parameters:
      - in: header
        name: Authorization
        type: string
        required: true
        description: "Bearer <token>"
      - in: query
        name: bag_id
        type: string
        required: true
    responses:
      200:
        description: The bag's remaining contents
      404:
        description: No such bag, or not yours
      401:
        description: Missing, invalid or expired token
    """
    # Exists so a client that reconnects, or one that is unsure whether a take
    # landed, can ask rather than guess. A bag the server has already emptied is
    # a 404, which is the same answer as never having existed - and the right
    # one, because in both cases there is nothing to collect.
    bag_id = str(request.args.get("bag_id", "")).strip()
    if not bag_id:
        return bad_request("bag_id is required")

    user_id = g.user["id"]
    bag = get_db().execute(
        "SELECT slot, enemy_id, created_at FROM loot_bags WHERE bag_id = ? AND user_id = ?",
        (bag_id, user_id),
    ).fetchone()
    if bag is None:
        return {"error": "Not Found", "message": "No such loot bag."}, 404

    rows = get_db().execute(
        "SELECT position, item_id, quantity FROM loot_bag_items WHERE bag_id = ? ORDER BY position",
        (bag_id,),
    ).fetchall()

    return {
        "bag_id": bag_id,
        "slot": int(bag["slot"]),
        "enemy_id": bag["enemy_id"],
        "contents": [
            {"position": int(r["position"]), "item_id": r["item_id"], "quantity": int(r["quantity"])}
            for r in rows
        ],
    }, 200


# =============================================================================
# ENTRY POINT
# =============================================================================
#
# LAST LINE OF THE FILE, AND IT HAS TO BE.
#
# app.run() BLOCKS. Every @app.route below it would be parsed but never
# executed, so those routes would simply not exist on the running server - with
# no error anywhere, because nothing is wrong with the code. The client would
# get a 404 from an endpoint that is plainly right there in the file.
#
# This block sat in the middle for a while after endpoints were appended below
# it, and /api/combat/kill was silently unregistered the whole time. Keep new
# routes ABOVE this line.

if __name__ == "__main__":
    app.run(debug=True)
