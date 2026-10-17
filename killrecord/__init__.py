"""killrecord - the kill record: who killed what, and how many (game 0.9.0;
a package since 16 Oct).

The owner, 7 Oct: "lets make a button in game that records all players
kills with icons of the enemies". The game's Kills window reads these two
routes:

    GET /api/kills             this character's kills of each kind of monster
    GET /api/kills/everyone    every monster's total, how many players, and
                               who has killed the most

The counts are kill_tally's - paid kills only, so a monster that grants
nothing (a large slime, which splits instead of dying) is never on it - and
both answers carry `since`, the day counting began, so a board that starts
at zero says why.

READS ONLY. Nothing here changes a count: app.py's _record_kill(), in the
kill's own transaction, is the only writer, and a deleted character's rows
go with it (character_delete).

THE FIRST AREA TAKEN OUT OF app.py rather than built outside it
(ARCHITECTURE.md): the two routes, their docstrings and their endpoint
names are as they were, which test_routes.py's routes.lock holds them to.
Built like playerstats/: routes.py (the edge), services.py (the answers,
pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs.
"""
from .routes import register  # noqa: F401
