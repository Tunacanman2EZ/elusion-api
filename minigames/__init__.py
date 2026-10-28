"""minigames - the games beside the world, and how often each is played
(game 0.93.3).

    GET  /api/minigames         every minigame, and how long until you may
                                go in again
    POST /api/minigames/enter   going in: refused while your wait runs

The owner: "we need to get rid of ladder and make this a button in hud that
says minigames this dungeon will have a 1 day wait timer for players", and
asked when the day starts: "Pressing Go". So going in IS the day: the first
Go writes when, and every Go inside GAMES' wait after it is refused (409,
with how long is left). Per account, so another character does not start a
fresh day; the owner and a co-owner skip the wait (testing), and their going
in is written all the same.

THE HONEST LIMIT. The server keeps the day; the game asks before it goes,
and a modified game could walk into the area without asking. Nothing in a
minigame pays yet. When the Endless's chest does, it pays only inside a day
this table holds - that is where the wait stops being a courtesy.

Built like killrecord/ (ARCHITECTURE.md): routes.py (the edge), services.py
(the rules, pure), data.py (the SQL), schema.py (its table). It never imports
app.py; register() is handed what it needs.
"""
from .routes import register  # noqa: F401
