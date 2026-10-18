"""events - the world's scheduled events (game 0.48.0).

The owner's plan for it: "Treat events (double XP, double loot, forced stir,
etc.) as their own feature area with a clean public contract, not just some
extra flags in config." So a GM schedules them ahead - Double XP Friday 7-9 pm,
the fire lands stirring all Saturday - and every game is told what is on and
what is coming, through one contract:

    GET  /api/world/events/active     what is on now and what is next
                                      (also on every broadcast poll, "events")
    GET  /api/world/events/schedule   the owner's list: past, present, future
    POST /api/world/events            the owner schedules one
    POST /api/world/events/cancel     the owner calls one off
    POST /api/world/events/preview    the owner's dry run (0.49.0)
    GET  /api/world/events/report     the owner's event report (0.49.0)

Built like playerstats/, in three layers:

    routes.py    the HTTP edge, and the one object app.py keeps (EventsAPI)
    services.py  the rules: what an event may be, which are on, what they pay
    data.py      the SQL: world_events, its schema included

THE GAME APPLIES NO MULTIPLIER. More XP and more loot are paid by the server at
the kill (world/'s stir_for(), app.py's world_stir_for(), asks
EventsAPI.kill_scales()); a stir event is the stirring lands' own (world/'s
current_tick() asks forced_stir()). The game is told so it
can show them. And never too much at once (0.49.0): an event is
refused when set if any land's monsters could pass the ceilings with
everything else on, and a kill is held to them when paid (ceiling()). This package never imports app.py: register() is
handed what it needs.
"""
from .routes import register  # noqa: F401
