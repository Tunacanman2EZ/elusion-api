"""clienterrors - what broke in the players' games, told by the games
themselves (game 0.88.0).

    POST /api/client/errors          a game: the script errors it hit
    GET  /api/server/client-errors   owner and co-owner: what players hit,
                                     grouped, the last hours

The owner rated stability 8 of 10 - "rare live breakages, and fixes go out
fast" - and asked for the 9. A breakage in a player's game reached him only if
the player said so: the game printed its SCRIPT ERROR to a console nobody
reads, and played on half broken. Now the game sends one short line for each
script error - the file and line, the message, how many times, its version and
the area - and the GM panel's Health tab lists what players hit, worst first,
with how many players and how many times. Never who: no name leaves this
package, and the read route counts players without listing them.

Built like serverhealth/ (ARCHITECTURE.md): routes.py (the edge), services.py
(the rules, pure), data.py (the SQL), schema.py (its one table). It never
imports app.py: register() is handed what it needs.
"""
from .routes import register  # noqa: F401
from .services import (DAYS_KEPT, ERRORS_PER_POST, ROWS_PER_DAY, SHOWN, TIMES_MOST,  # noqa: F401
                       WHAT_MOST, WHERE_MOST)
