"""bank - the account-shared bank, and what the account holds (a package since
18 Oct; the routes are as they were in app.py).

    GET  /api/account          lusions, bank gold, the score, the bank's cells
    PUT  /api/account/bank     the whole bank - the owner's tooling only; a
                               player's is ignored, with the bank in the answer
    PUT  /api/account/lusions  reads the balance back; writes are ignored
    POST /api/bank/items       move an item between a bag and the bank
    POST /api/bank/gold        move gold between a pocket and the bank
    POST /api/bank/move        move, merge or swap one bank cell onto another
    POST /api/bank/discard     destroy the stack in one bank cell

THE BANK IS THE ACCOUNT'S, NOT A CHARACTER'S. characterdata.gd keeps
bank_gold and bank_inventory in account_data, "account-shared, safe from
death", and that is the whole point of the feature: carry gold and carry items
are lost when you die, so the bank is where you put things you do not want to
lose, and it is shared so a second character can use what the first one
banked. (It was modelled per character once - bank_items keyed on (user_id,
slot, item_id), bank_gold a column on saves - which would have hidden your
warrior's bank from your mage.)

THE BANK IS POSITIONAL, like the backpack and for the same reason: the client
holds it as an array of cells with nulls for the empty ones, and the player
expects things to stay where they put them. Keyed on item_id it was a set,
which would silently merge two stacks and reshuffle the grid.

A TRANSFER CONSERVES THE TOTAL. The server holds both sides of an item
transfer - carry_items and bank_items are both its rows - and both sides of a
gold one, so /api/bank/items and /api/bank/gold are ops it can check: what
leaves one side is exactly what arrives at the other. A whole-array replace
cannot conserve anything; two arrays that do not add up are indistinguishable
from two that do. The layout objection is answered rather than ignored: the
server decides which cell a deposited stack lands in (services.pack()), and
the response carries BOTH grids.

Built like death/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs, and hands back account_of() for the pile route. WHAT STAYS IN
app.py: the grid helpers the backpack shares (taking from cells, a move, a
discard - the carry's, step 10), the account row (_ensure_account()), the
ledgers, the backpack grant a withdrawal lands through, and the bank's size
(BANK_CAPACITY, from gamedata.json).
"""
from .routes import register  # noqa: F401
