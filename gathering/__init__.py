"""gathering - fishing and cooking: what a rod lands, and what a fire makes
of it (a package since 18 Oct; the routes are as they were in app.py).

    POST /api/fishing/catch   land a fish: one bait spent - the best carried,
                              since 0.74.0 - the catch rolled once a roll the
                              bait gives and the best fish kept, fishing XP
                              paid
    POST /api/cooking/cook    cook one raw fish: the burn rolled, cooking XP
                              paid

A MEAL OF SEVERAL INGREDIENTS is larder/'s (0.74.0), POST /api/cooking/recipe,
which answers in the shape services.cooked() gives a fish.

BOTH BUILT TO THE RULE IN docs/inventoryauthority.md, which named them before
either existed: "Anything that creates, destroys or moves an item is a server
endpoint." The client sends that it fished, never what it caught; it sends
which fish it wants cooked, never whether that fish burned.

Both copy /api/loot/take's shape exactly - validate and 4xx before the first
write, then one transaction ending in a single commit, then hand back the
totals rather than the delta so a client that drops a response is corrected by
the next one it gets.

Built like friends/ (ARCHITECTURE.md): routes.py (the edge), services.py
(the rules, pure), data.py (the SQL). It never imports app.py; register() is
handed what it needs. THE GRANTS ARE app.py's AND ARE HANDED IN - a skill's XP,
an item into the backpack, one out of it - because the loot bag, the shop,
trades, the bank and gifts pay with the same three, and a second copy of
"how a stack tops up" is how two bags come to be laid out differently.
"""
from .routes import register  # noqa: F401
from .services import (CAST_BUCKET_CAPACITY, CAST_TOKENS_PER_SECOND,  # noqa: F401
                       FISHING_BAIT_ID, FISHING_ROD_SUFFIX)
