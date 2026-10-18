"""carry - what a character carries: the bag and the hotbar's keys (a package
since 18 Oct; the routes are as they were in app.py).

    PUT  /api/character/inventory          the whole carry - the owner's
                                           tooling only; a player's is ignored,
                                           with the carry in the answer
    POST /api/character/inventory/move     move, merge or swap one carried cell
                                           onto another
    POST /api/character/inventory/discard  destroy the stack in one carried cell
    POST /api/character/inventory/cash     turn a carried pile of gold coins, or
                                           of lusions, into the balance

AND THE SERVER'S ITEM GRANTS, which is why the package is registered before
every area that pays with them. Everything that puts an item in a bag or takes
one out - loot, the shop, a bank withdrawal, a trade, a catch, a cook, an
equip, a consume, a staff grant - goes through add_to_backpack() and
take_from_backpack() here, and register() hands them back; app.py keeps them
callable as _add_to_backpack() and _take_from_backpack(), the names the
other areas have always been handed.

THE CARRY IS A GRID, NOT A SET. The player drags a potion into the third cell
and it stays in the third cell; two stacks of the same item in two cells is a
legal, ordinary state. So carry_items is keyed on (user_id, slot, position) -
one item per cell, the invariant the inventory UI already enforces - and every
answer that changes the carry hands back inventory_of(): CARRY cells, None in
the empty ones, the bag first and then the hotbar's keys.

ONE CELL AT A TIME. The bag used to be whatever the game last sent as a whole
array, trimmed to what the server had granted: a gain was caught, and the
arrangement and every loss were the client's word. Every act on a bag turned
out to be discrete after all - a drag is a move, the bin is a discard, a pile
is cashed - so each is its own request, carried out on the server's copy. THE
CLIENT NAMES WHAT IT SAW: a move or a discard says which item it expects in
the cell, and when the server's cell holds something else the client's picture
is out of date (a trade finished, a loot take landed). That is a 409 carrying
the carry, and nothing moves.

THE BANK IS THE SAME SHAPE, so the steps are one copy: taking from cells, a
move, a discard (grid_move(), grid_discard(), take_from_cells(),
apply_cell_changes()) are named by table and handed back for bank/ too. The
last time this project had one of these rules twice, the two copies stopped
agreeing about the stack ceiling.

Built like bank/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. WHAT STAYS IN app.py: the carry's sizes (INVENTORY_CAPACITY,
HOTBAR_SIZE, CARRY_CAPACITY, QUANTITY_CEILING - the file's tunables, which the
bank and the shop quote too), whether a character exists (_slot_exists(), the
saves'), the two cell parsers, the trade-resync flag, the ledgers and the
account row a pile is cashed into.
"""
from .routes import register  # noqa: F401
