"""guilds - founding, inviting, ranks, leaving, taking down, and the staff
levers on a guild's name (a package since 18 Oct; the routes are as they were
in app.py).

    GET  /api/guild            your guild, its roster, and invitations waiting
    POST /api/guild/create     found one, paying the founding cost
    POST /api/guild/invite     invite somebody (officer or leader)
    POST /api/guild/respond    accept or turn down an invitation
    POST /api/guild/leave      leave it
    POST /api/guild/kick       remove somebody (officer or leader)
    POST /api/guild/rank       change somebody's rank (leader only)
    POST /api/guild/disband    take it down - your own, or any as the owner
    POST /api/staff/guild      rename or disband one whose name is a problem
    GET  /api/guild/list       every guild, for staff

A guild is a name, a roster and a chat channel. What it is NOT, deliberately,
is a second place a player's identity lives: the users table still decides
who you are and what rank you hold on the server, and guild rank is a
separate ladder that means nothing outside the guild.

ONE GUILD PER PLAYER, enforced by guild_members' primary key rather than by a
check in a route - see the note over the table in guilds/schema.py.

INVITED AND ACCEPTED, never simply added. Same argument as the friends list:
joining a guild attaches somebody's chosen name to yours and puts you in a
channel, and doing that without being asked is not a thing to make easy.

Built like friends/ (ARCHITECTURE.md): routes.py (the edge), services.py
(the rules, pure), data.py (the SQL). It never imports app.py; register() is
handed what it needs. WHAT OTHER AREAS SHARE STAYS IN app.py: guild chat (the
channel is chat's), the guild on a chat line, the poll's invitations, the
players list and the kingdom board all read the tables there - and ask who is
in which guild, and how a guild's name is drawn, through guild_membership()
and guild_tag(), which are this package's membership() and guild_tag().
"""
from .data import log_guild_event, membership  # noqa: F401
from .routes import register  # noqa: F401
from .services import (GUILD_ACTIVITY_SHOWN, GUILD_EVENT_KINDS, GUILD_EVENTS_KEPT,  # noqa: F401
                       GUILD_FOUND_COST, MAX_GUILD_NAME, guild_tag)
