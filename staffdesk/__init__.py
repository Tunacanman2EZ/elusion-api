"""staffdesk - the staff list and the moderation record, for a server that is
not small (a package since 18 Oct; the routes are as they were in app.py).

    GET  /api/staff/users             one page of accounts: rank, ban, mute,
                                      record, online - searched, filtered,
                                      a cursor and a limit
    GET  /api/staff/actions           the moderation log, newest first, a page
                                      at a time - about a player, by a member
                                      of staff, one kind or a set of them
    POST /api/staff/note              a staff-only note or warning on an
                                      account's record
    GET  /api/staff/user/<username>   everything staff may know about one
                                      account

The staff panel used to ask for every account on the server, every ten seconds
for as long as it was open, and search and sort them on the client. Fine at
forty accounts; at forty thousand it is the server denying service to itself,
and the panel is open exactly when things are busy. So the list answers a PAGE
- a search, a filter, a cursor and a limit - and the client asks for the next
page when somebody scrolls to it.

KEYSET, NOT OFFSET. "Skip 3,000 rows" makes SQLite walk 3,000 rows to throw
them away, and a row added or removed between two pages shifts every later one
- a player registering while a mod pages through the list pushes somebody else
off the edge of a page, unseen. "Names after zed_7" is one index seek and
cannot skip anyone.

NOTES AND WARNINGS ARE PRIVATE to the people who could act on the account
(STAFF_PRIVATE_KINDS, can_act_on(), strictly above) - in the log's own WHERE,
on the list's counts and on the account's page. So are its addresses and the
accounts linked through them. The player never sees any of it.

Built like moderation/ (ARCHITECTURE.md): routes.py (the edge), services.py
(the rules, pure), data.py (the SQL, and the two query builders whose plans
test_moderation.py checks). It never imports app.py; register() is handed what
it needs and hands back the builders. WHAT STAYS IN app.py: the moderation
log's writer (log_staff_action()), which every staff action calls; the rank
ladder and its SQL twin (_rank_level_sql()), which reads the co-owners as they
are; the linked-accounts read, which belongs with the install ids; and
/api/staff/powers, which reads every route on the server.
"""
from .data import STAFF_ONLINE_COUNT_SQL  # noqa: F401
from .routes import register  # noqa: F401
from .services import (STAFF_ACTION_GROUPS, STAFF_ACTION_KINDS, STAFF_LIST_SHOWS,  # noqa: F401
                       STAFF_NOTE_MAX, STAFF_PAGE_DEFAULT, STAFF_PAGE_MAX, STAFF_PRIVATE_KINDS,
                       STAFF_RECORD_KINDS)
