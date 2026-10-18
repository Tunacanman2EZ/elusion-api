"""players - who is around: who is online and where, who is in your area, and
the ticket that lets other players see you (a package since 18 Oct; the routes
are as they were in app.py).

    GET  /api/players/online    everyone playing right now, and where - the
                                who-is-online menu
    GET  /api/players/nearby    other players in your area, right now - the
                                trade panel's list
    POST /api/presence/ticket   a ticket to be seen by other players: handed to
                                the presence socket

AREA, NOT DISTANCE. The server knows which AREA a character is in (saves.area)
and nothing finer, so "nearby" is "in the same area and online", and both lists
say so ("precision": "area"). ONLINE IS A LIVE SESSION SEEN LATELY: a client
heard inside the online window AND a token still allowed to be used - either
one alone is wrong in a different direction (players_nearby says how). And
only the character an account is PLAYING is listed, by app.py's rule
(PLAYING_SLOT_SQL), never every save it has parked somewhere.

WHAT THE ONLINE LIST DISCLOSES, said plainly because it is a choice: every
online player's account name, character name, level, area, rank and guild, to
every other player. That is what a who-is-online list is, and the game already
puts all of it over a character's head.

SEEING EACH OTHER. Players are drawn in each other's worlds by presence.py, a
WebSocket process beside this one (it says why it is a process of its own).
The ticket route is its front door: a game with a login asks here for a
ticket, then hands the ticket to the socket. So bans, a session ended elsewhere
and a stale token are decided here, by require_auth, exactly as for every
other route - and the socket only has to find a ticket row and its login.

THE IDENTITY IS OURS. What other players see over somebody's head - name,
crown or badge, colour, guild tag, class and level - and which pets they may
be seen with are read from this server's rows, never taken from the game. The
game only tells the socket where it is standing and how it is moving.

Built like shop/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. The one table it writes is presence_tickets, which presence.py
reads. WHAT STAYS IN app.py: which character an account is playing (other
areas ask it too), the online window, how a person is shown, the guild lookup,
the token hashing, and the presence socket's settings, read with every other
setting.
"""
from .routes import register  # noqa: F401
from .services import NEARBY_LIMIT, ONLINE_LIST_LIMIT, PRESENCE_PATH, PRESENCE_TICKET_SECONDS  # noqa: F401
