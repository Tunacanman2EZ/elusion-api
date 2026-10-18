"""consuming - using one consumable from the bag (a package since 18 Oct; the
route is as it was in app.py).

    POST /api/character/consume   the server takes the item and says what it
                                  restores; the game applies it

WHY THIS ENDPOINT EXISTS AT ALL, when the client could simply drop the item
from its next inventory sync and be done with it.

ItemData has carried `required_level` and `required_skill` for a while, and
inventoryscreen.gd checked both before letting a use through. That check runs
on the player's machine, which made it a courtesy rather than a rule - and its
own comment said so, and named the day it would stop being good enough:

    "It becomes load-bearing the day trade exists."

Trade exists. Before it, the only person the gate protected against was an
honest player using something they had not earned, because an item you hold is
an item you found. Now a level 1 character can be HANDED a level 22 potion by a
friend, and the only thing between them is a check they control.

WHAT THIS CLOSES AND WHAT IT DOES NOT. It makes the requirement real and the
destruction real: the item leaves the bag because the server took it, not
because the client announced it. It does NOT stop a determined cheat, because
PUT /api/player/status still accepts any hp up to the server's derived maximum
- a patched client heals without drinking anything. That is a separate and
larger finding, and this endpoint is what makes it addressable: once healing
has an authorised source, an unexplained rise becomes a question the server
can ask - the healing reconciler reads consume_grants, which this writes.

THE EFFECT IS STILL THE CLIENT'S, but the size of it is the server's: the
catalogue's restore_for() is written into the grant and handed back, so the two
cannot disagree about it.

Built like equipment/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. WHAT STAYS IN app.py: the backpack grant the potion leaves
through (the carry's), the skills it is checked against (skills_payload(),
VALID_SKILLS), and the healing reconciler that reads what this writes.
"""
from .routes import register  # noqa: F401
