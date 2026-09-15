"""
Set an account's rank, or list every account.

Rank is server-side. Nothing the game client sends can change it, and this
script is the only intended way to set it - it talks to the database directly
rather than through a route, so a rank can only be granted from the machine
holding elusion.db, never over the network. Keep that property.

    python set_role.py Someone dev        promote to dev
    python set_role.py Someone mod        promote to mod
    python set_role.py Someone player     demote

Ranks run player < mod < dev < owner.
    python set_role.py --list             show every account and its rank

The OWNER is not settable here and never will be. It is named by the
ELUSION_OWNER environment variable, so that no write to this table - by this
script, by an endpoint, or by hand - can grant it.
"""

import argparse
import os
import sqlite3
import sys

DB_PATH = os.environ.get("ELUSION_DB", os.path.join(os.path.dirname(__file__), "elusion.db"))

# Must match ROLES in app.py, minus 'owner', which is not storable.
SETTABLE_ROLES = ("player", "mod", "dev")


def connect():
    if not os.path.exists(DB_PATH):
        sys.exit(f"No database at {DB_PATH}. Start app.py once to create it.")
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def list_users(db):
    rows = db.execute("SELECT id, username, role FROM users ORDER BY id").fetchall()

    if not rows:
        print("No accounts yet.")
        return

    owner = os.environ.get("ELUSION_OWNER", "").strip()
    for row in rows:
        rank = row["role"]
        if owner and row["username"].casefold() == owner.casefold():
            rank = "owner (ELUSION_OWNER)"
        print(f'{row["id"]:>3}  {row["username"]:<20} {rank}')

    if not owner:
        print("\nNo ELUSION_OWNER set in this shell, so no owner is shown.")


def set_role(db, username, role):
    if role not in SETTABLE_ROLES:
        sys.exit(f"Rank must be one of: {', '.join(SETTABLE_ROLES)}. "
                 f"'owner' is set by the ELUSION_OWNER environment variable, not here.")

    cursor = db.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
    db.commit()

    if cursor.rowcount == 0:
        sys.exit(f"No account named {username!r}.")

    print(f"{username} is now {role}.")


def main():
    parser = argparse.ArgumentParser(description="Set an account's rank.")
    parser.add_argument("username", nargs="?", help="account to change")
    parser.add_argument("role", nargs="?", help=f"one of: {', '.join(SETTABLE_ROLES)}")
    parser.add_argument("--list", action="store_true", help="list all accounts")
    args = parser.parse_args()

    db = connect()

    if args.list or not args.username:
        list_users(db)
        return

    if not args.role:
        sys.exit(f"Give a rank: {', '.join(SETTABLE_ROLES)}. Use --list to see who holds what.")

    set_role(db, args.username, args.role)


if __name__ == "__main__":
    main()
