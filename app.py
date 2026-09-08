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
# constantly, so this is generous — 30 days in seconds.
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
        # enforce foreign keys — off by default in sqlite
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
        """
    )
    db.commit()
    db.close()


init_db()


# =============================================================================
# VALIDATION
# =============================================================================

def validate_credentials(payload):
    """
    Check an incoming register/login body.
    Returns (is_valid, errors_list) — same shape as spells_api.
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
        # expired — clean it up rather than leaving dead rows around
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

    # same response whether the user is missing or the password is wrong —
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


if __name__ == "__main__":
    app.run(debug=True)
