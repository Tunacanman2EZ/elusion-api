from flask import Flask, request, g
from flasgger import Swagger
from werkzeug.security import generate_password_hash, check_password_hash

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
        CREATE TABLE IF NOT EXISTS bank_items (
            user_id  INTEGER NOT NULL,
            slot     INTEGER NOT NULL,
            item_id  TEXT    NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (user_id, slot, item_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )

    _migrate_add_column(db, "saves", "active_pet_id", "TEXT NOT NULL DEFAULT ''")

    db.commit()
    db.close()


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
# BANK  -  GET/POST /api/bank
# =============================================================================

def bank_payload(user_id, slot):
    """
    The WHOLE bank, always. Every bank response returns full state rather than
    a delta, so the client never has to reconstruct what the server did to it.
    Reconstructing is how bank duplication bugs get written.
    """
    db = get_db()

    save = db.execute(
        "SELECT gold, bank_gold FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()

    rows = db.execute(
        "SELECT item_id, quantity FROM bank_items WHERE user_id = ? AND slot = ? ORDER BY item_id",
        (user_id, slot),
    ).fetchall()

    return {
        "slot": slot,
        "gold": save["bank_gold"] if save else 0,
        # what the character is CARRYING. the gold endpoint moves value between
        # these two numbers, so a client that only knew one of them could never
        # show the player whether a withdrawal was even possible.
        "carried_gold": save["gold"] if save else 0,
        "capacity": BANK_CAPACITY,
        "items": [{"item_id": r["item_id"], "quantity": r["quantity"]} for r in rows],
    }


@app.get("/api/bank")
@require_auth
def read_bank():
    """
    Read the bank for one character slot
    ---
    tags:
      - Bank
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
        description: Full bank contents
      400:
        description: Missing or out-of-range slot
      401:
        description: Missing, invalid or expired token
    """
    slot = parse_slot(request.args.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    return bank_payload(g.user["id"], slot), 200


@app.post("/api/bank")
@require_auth
def modify_bank():
    """
    Deposit or withdraw one item stack
    ---
    tags:
      - Bank
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
          required: [slot, op, item_id, quantity]
          properties:
            slot:     {type: integer, example: 0}
            op:       {type: string,  enum: [deposit, withdraw]}
            item_id:  {type: string,  example: healthpotion}
            quantity: {type: integer, example: 5}
    responses:
      200:
        description: The full bank after the operation
      400:
        description: Bad slot, op, item_id, quantity, or withdrawing more than stored
      409:
        description: Deposit would exceed bank capacity
      401:
        description: Missing, invalid or expired token
    """
    payload = request.get_json(silent=True) or {}
    user_id = g.user["id"]

    slot = parse_slot(payload.get("slot"))
    if slot is None:
        return bad_request("slot must be an integer 0-%d" % MAX_SLOT)

    op = str(payload.get("op", "")).strip().lower()
    if op not in ("deposit", "withdraw"):
        return bad_request("op must be 'deposit' or 'withdraw'")

    item_id = str(payload.get("item_id", "")).strip()
    if not item_id or len(item_id) > 64:
        return bad_request("item_id must be 1-64 characters")

    raw_quantity = payload.get("quantity")
    if isinstance(raw_quantity, bool):
        return bad_request("quantity must be a positive integer")
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError):
        return bad_request("quantity must be a positive integer")
    if quantity <= 0:
        return bad_request("quantity must be a positive integer")

    db = get_db()

    existing = db.execute(
        "SELECT quantity FROM bank_items WHERE user_id = ? AND slot = ? AND item_id = ?",
        (user_id, slot, item_id),
    ).fetchone()
    held = existing["quantity"] if existing else 0

    if op == "deposit":
        # Capacity counts DISTINCT stacks, not total items - so topping up a
        # stack you already hold is always allowed even at a full bank.
        if existing is None:
            stacks = db.execute(
                "SELECT COUNT(*) AS n FROM bank_items WHERE user_id = ? AND slot = ?",
                (user_id, slot),
            ).fetchone()["n"]
            if stacks >= BANK_CAPACITY:
                return {
                    "error": "Conflict",
                    "message": "Bank is full (%d stacks)." % BANK_CAPACITY,
                }, 409

        db.execute(
            """
            INSERT INTO bank_items (user_id, slot, item_id, quantity)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, slot, item_id) DO UPDATE SET
                quantity = quantity + excluded.quantity
            """,
            (user_id, slot, item_id, quantity),
        )
    else:
        # Refuse rather than clamp. Clamping a withdrawal of 10 from a stack
        # of 3 silently destroys the player's request and leaves them unsure
        # what they now hold; an error leaves the bank exactly as it was.
        if quantity > held:
            return bad_request(
                "Cannot withdraw %d of '%s' - only %d stored." % (quantity, item_id, held)
            )

        if quantity == held:
            # The CHECK constraint forbids quantity 0, so an emptied stack is
            # deleted rather than zeroed. One representation of "none".
            db.execute(
                "DELETE FROM bank_items WHERE user_id = ? AND slot = ? AND item_id = ?",
                (user_id, slot, item_id),
            )
        else:
            db.execute(
                "UPDATE bank_items SET quantity = quantity - ? WHERE user_id = ? AND slot = ? AND item_id = ?",
                (quantity, user_id, slot, item_id),
            )

    db.commit()
    return bank_payload(user_id, slot), 200


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
# BANK - GOLD
# =============================================================================

@app.post("/api/bank/gold")
@require_auth
def move_bank_gold():
    """
    Move gold between the character and the bank
    ---
    tags:
      - Bank
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
        description: The full bank after the transfer, including carried gold
      400:
        description: Bad slot, op or amount, or not enough gold on the relevant side
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

    op = str(payload.get("op", "")).strip().lower()
    if op not in ("deposit", "withdraw"):
        return bad_request("op must be 'deposit' or 'withdraw'")

    amount = parse_stat(payload.get("amount"))
    if amount is None or amount <= 0:
        return bad_request("amount must be a positive integer")

    db = get_db()
    row = db.execute(
        "SELECT gold, bank_gold FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()
    if row is None:
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    carried = row["gold"]
    banked = row["bank_gold"]

    # GOLD IS A TRANSFER, NOT A DEPOSIT. This is the one thing the server can
    # verify that it cannot verify for items: it holds BOTH balances, so it
    # can enforce that the total is conserved. For items it only ever sees the
    # bank side - the client's inventory is not the server's to check, which is
    # exactly why items and gold are different endpoints rather than one.
    if op == "deposit":
        if amount > carried:
            return bad_request(
                "Cannot deposit %d - only %d carried." % (amount, carried)
            )
        carried -= amount
        banked += amount
    else:
        if amount > banked:
            return bad_request(
                "Cannot withdraw %d - only %d banked." % (amount, banked)
            )
        banked -= amount
        carried += amount

    # One UPDATE, so both sides move together or neither does. Two statements
    # would leave a window where the gold exists in neither place.
    db.execute(
        "UPDATE saves SET gold = ?, bank_gold = ?, updated_at = ? WHERE user_id = ? AND slot = ?",
        (carried, banked, int(time.time()), user_id, slot),
    )
    db.commit()

    return bank_payload(user_id, slot), 200


if __name__ == "__main__":
    app.run(debug=True)
