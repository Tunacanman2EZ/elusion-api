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
