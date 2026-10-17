"""insights - what the world's data says about its lands (game 0.50.0).

The owner's ask, from a "Behavior Insights" idea: for each land, recent
players, kills, time since the last event and whether it is overfarmed,
ignored or healthy; a suggested next stir with a reason - weighing new
players against veterans and when each land is busy; and the GM panel to
show it and accept or override.

    GET /api/server/insights    owner: every land's numbers, the next stir
                                and why, and who is online by hour

AND THE WORLD'S OWN PICK uses the same rule (InsightsAPI.choose(), asked by
app.py's pick_land()): the least hunted land, never the one that just stirred
- and now never one where a new player is learning, while another will do. A
stir makes its land's monsters hit harder and take more killing; that is for
the players who are ready for it, not for somebody on their first evening.

Built like playerstats/ and events/: routes.py (the edge, and InsightsAPI),
services.py (the rules, pure functions), data.py (the SQL, reads only). It
never imports app.py; register() is handed what it needs.
"""
from .routes import register  # noqa: F401
