"""accountdelete - a player deletes their own account (game 0.90.0).

    POST /api/account/delete   the caller's account: everything that is theirs
                               gone, the row left as a numbered stand-in

The owner, of a database diagram that showed no way to remove an account
("lets fix all of those"): before launch players will ask, and privacy laws
in some places give them the right to. Nothing in the server had ever
removed a user row.

THE ROW STAYS, EMPTY. A bare DELETE FROM users would cascade through every
foreign key, and two of the things it reaches are other people's: a finished
trade is the other player's record too (a moderator reads it when somebody
says they were scammed), and the gold and lusion ledgers are the supply's
history. So the account is EMPTIED instead: every row that is the player's -
characters, bags, the bank, skills, sessions, friends, ignores, invitations,
chat lines and pictures, kills, samples, error reports, sign-in records - is
deleted; their gold and lusions are burned through the ledgers first, so the
supply still adds up; and the users row keeps only its id, its creation time
and a name nobody can sign up with (services.stand_in_name(): "deleted player
#12", outside USERNAME_PATTERN). Their old name is free again.

KEPT, WITH THE NAME TAKEN OUT: the staff log (what staff did, and that this
account was deleted), chat reports (what was reported stays, for the record
of the person who reported it), the owner's gifts ledger, trades and both
ledgers (by the stand-in's id, which names nobody).

ASKED THREE TIMES: the account's name typed, the password (a wrong one is
401, counted as a failed login, the way the email route counts one), and the
game's own "Sure?". Refused while an open trade names the account (409), while
it leads a guild with other members in it (409: hand it on first, as leaving
would ask), and for the owner and the co-owners (403).

Built like clienterrors/: routes.py (the edge), services.py (pure), data.py
(the SQL). It never imports app.py: register() is handed what it needs.
"""
from .routes import register  # noqa: F401
from .services import DELETE_REASON, STAND_IN_PREFIX, stand_in_name  # noqa: F401
