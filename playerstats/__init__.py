"""playerstats - the owner's numbers on the players (game 0.47.0).

The owner: "build this and add it in my gm menu as a button somewhere" - the
analytics idea from the BE105 lesson: where do new players stop, who comes
back, what gets played. Every number comes from rows the server already keeps
for its own reasons; nothing new is recorded about anybody.

THE FIRST FEATURE BUILT THE BE105 WAY, beside the one big app.py:

    routes.py    the HTTP edge: one GET route, its query, its refusals
    services.py  the arithmetic: pure functions of plain rows and a clock
    data.py      the SQL: every read, and nothing but reads

app.py hands it what it already has (the app, its decorators, its database)
through register(); this package imports nothing from app.py, so it can be
tested on its own and moved without touching anything else.
"""
from .routes import register  # noqa: F401
