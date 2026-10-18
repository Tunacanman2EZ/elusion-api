"""loot - the bags a kill drops, and taking things out of them (a package
since 18 Oct; the routes are as they were in app.py).

    POST /api/loot/take   take one cell of a bag: into the backpack, the purse
                          or the lusions
    GET  /api/loot/bag    what is still in a bag

THE LAST THING THE CLIENT GOT TO ASSERT. /api/combat/kill already decided what
dropped, but the bag then existed only in the client's world: it spawned a
node, the player walked over it, items landed in the local inventory, and the
next save simply TOLD the server what was now being carried. The server rolled
a potion and had no idea whether you picked it up, dropped it, or invented
forty more. So the bag exists HERE:

  1. A kill that drops something creates a bag with an unguessable id
     (app.py's _create_loot_bag(), in the kill's own commit, with
     stamped() and store_bag() from this package).
  2. The client renders it, but the contents it renders are a copy.
  3. Taking an item is a REQUEST. The server checks the bag is yours, that it
     still holds that item, and moves it into your backpack or your purse.

After this, gold and carried items only change through an endpoint that
verified where they came from.

WHY POSITION-KEYED, LIKE EVERY OTHER CONTAINER: "take the item in slot 2" is a
request the server can answer exactly once, whereas "take a
smallhealthpotion" is ambiguous when the bag holds two stacks of them, and a
client that sends it twice would be asking for a duplication bug.

Built like friends/ and gathering/ (ARCHITECTURE.md): routes.py (the edge),
services.py (the rules, pure), data.py (the SQL - loot_bags and
loot_bag_items, app.py's init_db() tables). It
never imports app.py; register() is handed what it needs. THE MONEY AND THE
BACKPACK ARE app.py's AND ARE HANDED IN - the gold and lusion ledgers, the
grant into the backpack, the trade hold, "does this account hold that pet" -
because the shop, trades, the bank and gifts use the same ones.
"""
from .data import store_bag  # noqa: F401
from .routes import register  # noqa: F401
from .services import LOOT_BAG_CAPACITY, LOOT_BAG_TTL_SECONDS, stamped  # noqa: F401
