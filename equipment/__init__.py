"""equipment - putting gear on and taking it off (a package since 18 Oct; the
routes are as they were in app.py).

    POST /api/character/equip     move one item from the bag onto the character
    POST /api/character/unequip   and back off it, into the bag

EQUIPMENT IS A MOVE, NOT A REFERENCE. Equipping used to be a POINTER:
`equipment` named an item that was also sitting in the backpack, and
characterdata.gd's prune_equipment() cleared any slot naming something the bag
did not contain. The bag was the record of ownership and the doll was a view
onto it - which is why an equipped chest piece showed up twice on screen, once
in each panel.

That arrangement carried the ownership check for free: the bag is reconciled
against what the server granted (E-1), so gear pointing into it was reconciled
too. The moment equipping MOVES the item out of the bag, both of those stop
applying, and `equipment` becomes a client-written field that nothing
reconciles - in the column combat reads to decide what a hit is worth. That is
E-1 again, one column over, and it is the reason this is a server endpoint
instead of two lines in the UI.

SO THE SERVER MOVES IT. Taking from the bag IS the ownership check: you cannot
equip what app.py's _take_from_backpack() cannot find. Nothing needs
reconciling afterwards because the client never gets to assert the result.
Same shape as consuming/, for the same reason - the client asks, the server
moves, the client renders the answer.

Built like trade/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. WHAT STAYS IN app.py: the backpack grants the move goes through
(the carry's), the one way to ask a character's maxima (_derived_stats(), which
reads what is worn), and the catalogue's slot names (EQUIP_SLOTS).
"""
from .routes import register  # noqa: F401
