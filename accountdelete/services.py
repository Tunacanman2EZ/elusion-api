"""accountdelete/services.py - the rules, pure: no Flask, no database."""

# The ledgers' reason for gold and lusions an account took with it.
DELETE_REASON = "account-deleted"
# What the staff log and the reports say in place of the name.
STAND_IN_PREFIX = "deleted player #"
# The kind of the staff log line (staffdesk/'s STAFF_ACTION_KINDS has it).
# A typed name is compared like the database compares names (NOCASE), and a
# password is at most this long before it is even hashed.
PASSWORD_MOST = 200


def stand_in_name(user_id):
    """The name an emptied account keeps: unique (it has the id in it) and
    impossible to sign up with (a space and a #, outside USERNAME_PATTERN)."""
    return "%s%d" % (STAND_IN_PREFIX, int(user_id))


def is_stand_in(username):
    return str(username or "").startswith(STAND_IN_PREFIX)


def confirmed(typed, username):
    """The account's name, typed: any case, spaces round it forgiven."""
    return isinstance(typed, str) and typed.strip().casefold() == str(username).casefold() != ""


def guild_blocks(rank, members):
    """A guild's leader with others in it must hand it on first - the same
    rule as leaving it (guilds/ guild_leave)."""
    return rank == "leader" and members > 1


# THE LIST THAT OUTLIVES A RESTORE (14 Nov). Deleting empties the account in
# the live database, but every nightly copy taken before still holds it, and
# restoring one would bring it back (SECURITY.md, honest limits). So each
# deletion is also one line in a small file beside the database - kept out
# of it on purpose, since a restored copy holds whatever it held - and a
# server starting on a database where a listed account is whole again
# empties it again (app.py, DELETED AGAIN AFTER A RESTORE). A line is the
# account's id, when it was made and when it was deleted: no name, no email.
# Ids are never reused (users.id is AUTOINCREMENT), and an entry matches only
# the row made at that very second, so a line can never empty anybody else.
DELETED_SUFFIX = ".deleted"
AGAIN_REASON = "deleted again: it came back with a restored backup"


def deleted_list_path(db_path):
    """Where the list is kept: beside the database, the database's name and
    DELETED_SUFFIX."""
    return str(db_path) + DELETED_SUFFIX


def list_line(user_id, created_at, deleted_at):
    return "%d %d %d\n" % (int(user_id), int(created_at), int(deleted_at))


def parse_list(text):
    """{user_id: created_at} from the list's text. A line that is not three
    whole numbers is passed over (a torn last line from a crash), never
    guessed at."""
    found = {}
    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) != 3 or not all(part.isdigit() and part.isascii() for part in parts):
            continue
        found[int(parts[0])] = int(parts[1])
    return found

