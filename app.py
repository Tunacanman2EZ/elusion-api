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
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT    NOT NULL,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            created_at    INTEGER NOT NULL
        );

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
        -- a password hash and an admin flag have nothing to do with a lusion
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
        SELECT u.id, u.username, u.is_admin, s.expires_at
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

    return row


def bearer_token():
    """Pull the token out of an 'Authorization: Bearer <token>' header."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header[7:].strip()


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
            "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 0, ?)",
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

    return {
        "user_id": user_id,
        "username": username,
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
            is_admin:
              type: boolean
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
        "SELECT id, username, password_hash, is_admin FROM users WHERE username = ?",
        (data["username"],),
    ).fetchone()

    # same response whether the user is missing or the password is wrong -
    # otherwise this endpoint tells an attacker which usernames exist.
    if row is None or not check_password_hash(row["password_hash"], data["password"]):
        return {
            "error": "Unauthorized",
            "message": "Incorrect username or password.",
        }, 401

    token, expires_at = issue_token(row["id"])

    return {
        "user_id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
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
            is_admin:
              type: boolean
            expires_at:
              type: integer
      401:
        description: Missing, invalid or expired token
    """
    return {
        "user_id": g.user["id"],
        "username": g.user["username"],
        "is_admin": bool(g.user["is_admin"]),
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
BANK_CAPACITY = 40
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

    level = max(1, int(payload.get("level", 1) or 1))
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
            level         = excluded.level,
            area          = excluded.area,
            active_pet_id = CASE WHEN ? THEN excluded.active_pet_id
                                 ELSE saves.active_pet_id END,
            updated_at    = excluded.updated_at
        """,
        (g.user["id"], slot, class_id, name, level, area, active_pet_id, now,
         1 if pet_key_sent else 0),
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
    updates = {}
    for field in STATUS_FIELDS:
        if field not in payload:
            continue
        value = parse_stat(payload[field])
        if value is None:
            return bad_request(
                "%s must be a non-negative integer no greater than %d" % (field, STAT_CEILING)
            )
        updates[field] = value

    if not updates:
        return bad_request("no writable fields supplied")

    # Validate against the state AFTER the merge, not against what was sent.
    # Pushing hp=120 alone is illegal if stored max_hp is 100, but legal in the
    # same request that raises max_hp to 120 - and order of keys in JSON must
    # not decide which.
    merged = {field: row[field] for field in STATUS_FIELDS}
    merged.update(updates)

    for field, cap_field in STATUS_FIELDS.items():
        if cap_field is None:
            continue
        if merged[field] > merged[cap_field]:
            return bad_request(
                "%s (%d) cannot exceed %s (%d)" % (field, merged[field], cap_field, merged[cap_field])
            )

    assignments = ", ".join("%s = ?" % f for f in updates)
    values = list(updates.values()) + [int(time.time()), user_id, slot]

    db = get_db()
    db.execute(
        "UPDATE saves SET %s, updated_at = ? WHERE user_id = ? AND slot = ?" % assignments,
        values,
    )
    db.commit()

    return status_payload(user_id, slot), 200


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

# Minimum seconds between two kills on the same character.
#
# A rate limit, not a simulation. The server has no idea where anything is or
# how long a fight should take, so this cannot prove a kill was real - it only
# caps how fast a script could farm one. 0.35s is comfortably under any honest
# kill (your fastest enemy has a 1.2s attack cooldown and 35 hp at the very
# least) while turning "unlimited XP in a loop" into "XP at roughly three per
# second", which is slow enough to be visible in the numbers.
#
# The real answer is the server knowing which enemies exist and having handed
# this client that one. That needs the server to own spawning, which is phase
# two. This is the honest placeholder until then.
KILL_COOLDOWN_SECONDS = 0.35


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
        "SELECT level, xp, xp_to_next, last_kill_at FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    now_ms = int(time.time() * 1000)
    since_last = now_ms - int(row["last_kill_at"])
    if since_last < KILL_COOLDOWN_SECONDS * 1000:
        # 429 rather than 400: nothing about the request is malformed, it simply
        # arrived too soon. An honest client that hit this because of a lag
        # spike can read that and retry; a 400 would tell it to give up.
        return {
            "error": "Too Many Requests",
            "message": "Kills are limited to one per %.2fs." % KILL_COOLDOWN_SECONDS,
        }, 429

    # ---- everything above this line is validation; everything below commits --

    rewards = gamedata.roll_kill_rewards(enemy_id)

    level, xp, xp_to_next, levels_gained = gamedata.apply_xp(
        int(row["level"]), int(row["xp"]), int(row["xp_to_next"]), rewards["xp"]
    )

    # ONE UPDATE. The XP, the level and the cooldown stamp move together or not
    # at all - the same reasoning as the bank gold transfer. Two statements
    # would leave a window where a crash could bank the XP and lose the level,
    # or stamp the cooldown for a kill that was never paid.
    db.execute(
        """
        UPDATE saves
           SET level = ?, xp = ?, xp_to_next = ?, last_kill_at = ?, updated_at = ?
         WHERE user_id = ? AND slot = ?
        """,
        (level, xp, xp_to_next, now_ms, int(time.time()), user_id, slot),
    )
    db.commit()

    # The loot is RETURNED, not stored. The client spawns the bag from this and
    # the player still has to walk over and take it - which is the part that
    # remains client-side for now, and the part the second half of this change
    # has to close. What the server has fixed is that the contents below were
    # decided here, with entropy the client never sees.
    return {
        "enemy_id": enemy_id,
        "xp_gained": rewards["xp"],
        "attack_xp_gained": rewards["attack_xp"],
        "levels_gained": levels_gained,
        "level": level,
        "xp": xp,
        "xp_to_next": xp_to_next,
        "pet_won": rewards["pet_won"],
        "contents": rewards["contents"],
    }, 200


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

    # VALIDATE THE WHOLE ARRAY BEFORE WRITING ANY OF IT. A bad entry at index 17
    # must not leave the first seventeen written and the rest not - the player
    # would see a half-saved bag with no error explaining it.
    parsed = []
    for index, cell in enumerate(cells):
        if cell is None:
            continue
        if not isinstance(cell, dict):
            return bad_request("inventory[%d] must be an object or null" % index)

        item_id = str(cell.get("item_id", "")).strip()
        if not item_id or len(item_id) > 64:
            return bad_request("inventory[%d].item_id must be 1-64 characters" % index)

        raw_quantity = cell.get("quantity", 1)
        if isinstance(raw_quantity, bool):
            return bad_request("inventory[%d].quantity must be a positive integer" % index)
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            return bad_request("inventory[%d].quantity must be a positive integer" % index)
        if quantity <= 0:
            return bad_request("inventory[%d].quantity must be a positive integer" % index)

        parsed.append((user_id, slot, index, item_id, quantity))

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
