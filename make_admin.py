"""
Grant or revoke admin on an account.

Admin is a server-side column - nothing the game client sends can change it.
This script is the only intended way to set it.

    python make_admin.py Tunacan          grant
    python make_admin.py Tunacan --revoke revoke
    python make_admin.py --list           show every account and its flag
"""

import argparse
import os
import sqlite3
import sys

DB_PATH = os.environ.get("ELUSION_DB", os.path.join(os.path.dirname(__file__), "elusion.db"))


def connect():
    if not os.path.exists(DB_PATH):
        sys.exit(f"No database at {DB_PATH}. Start app.py once to create it.")
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def list_users(db):
    rows = db.execute(
        "SELECT id, username, is_admin FROM users ORDER BY id"
    ).fetchall()

    if not rows:
        print("No accounts yet.")
        return

    for row in rows:
        flag = "admin" if row["is_admin"] else "-"
        print(f'{row["id"]:>3}  {row["username"]:<20} {flag}')


def set_admin(db, username, value):
    cursor = db.execute(
        "UPDATE users SET is_admin = ? WHERE username = ?",
        (1 if value else 0, username),
    )
    db.commit()

    if cursor.rowcount == 0:
        sys.exit(f"No account named {username!r}.")

    print(f'{username} is {"now an admin" if value else "no longer an admin"}.')


def main():
    parser = argparse.ArgumentParser(description="Grant or revoke admin.")
    parser.add_argument("username", nargs="?", help="account to change")
    parser.add_argument("--revoke", action="store_true", help="remove admin instead of granting")
    parser.add_argument("--list", action="store_true", help="list all accounts")
    args = parser.parse_args()

    db = connect()

    if args.list or not args.username:
        list_users(db)
        return

    set_admin(db, args.username, not args.revoke)


if __name__ == "__main__":
    main()
