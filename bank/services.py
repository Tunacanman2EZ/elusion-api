"""bank/services.py - the rules: where a deposited stack lands, what the
account view holds, how much may move in one request, and what a gold
transfer leaves on each side. Pure: no database, no request."""

OPS = ("deposit", "withdraw")


def pack(occupied, item_id, quantity, max_stack, capacity, stackable):
    """
    Where `quantity` of `item_id` goes in a grid of `capacity` cells already
    holding `occupied` ({position: (item_id, quantity)}): [(position,
    item_id, new quantity)], or None when it does not fit.

    The bank's packing, the backpack's rules: tops up an existing stack before
    opening a cell, and plans NOTHING unless the whole quantity fits. A
    half-completed deposit is the shape of bug that ends with an item in
    neither the bag nor the bank.
    """
    occupied = dict(occupied)
    remaining = int(quantity)
    writes = []

    if stackable:
        for position in sorted(occupied):
            if remaining <= 0:
                break
            held_id, held_qty = occupied[position]
            if held_id != item_id or held_qty >= max_stack:
                continue
            moved = min(max_stack - held_qty, remaining)
            writes.append((position, item_id, held_qty + moved))
            remaining -= moved

    for position in range(capacity):
        if remaining <= 0:
            break
        if position in occupied:
            continue
        moved = min(max_stack, remaining)
        writes.append((position, item_id, moved))
        occupied[position] = (item_id, moved)
        remaining -= moved

    return None if remaining > 0 else writes


def stacking(definition):
    """(stackable, max_stack) for a catalogue row - one per cell when it is
    not stackable, or the row is missing."""
    definition = definition or {}
    stackable = bool(definition.get("stackable", False))
    max_stack = int(definition.get("max_stack", 1)) if stackable else 1
    return stackable, max(1, max_stack)


def grid(rows, capacity):
    """The bank as the client holds it: `capacity` cells, None for an empty
    one."""
    cells = [None] * capacity
    for item in rows:
        position = int(item["position"])
        if 0 <= position < capacity:
            cells[position] = {"item_id": item["item_id"], "quantity": int(item["quantity"])}
    return cells


def account_view(row, cells, capacity):
    """What GET /api/account answers, from the accounts row and the grid."""
    return {
        "lusions": int(row["lusions"]),
        "bank_gold": int(row["bank_gold"]),
        # KEYED WITH .keys() RATHER THAN row["score"], because a sqlite3.Row
        # raises IndexError for a column the SELECT did not fetch, and
        # app.py's _ensure_account() predates this one. The narrow-SELECT trap
        # is how a correct refusal turned into a 500 once already - see E-11.
        "score": int(row["score"]) if "score" in row.keys() else 0,
        "bank_inventory": cells,
        "capacity": capacity,
    }


def quantity_refusal(raw, ceiling):
    """(quantity, None), or (None, the 400's words) for a quantity that is not
    a whole number from 1 to `ceiling`."""
    if isinstance(raw, bool):
        return None, "quantity must be a positive integer"
    try:
        quantity = int(raw)
    except (TypeError, ValueError):
        return None, "quantity must be a positive integer"
    if quantity <= 0:
        return None, "quantity must be a positive integer"
    if quantity > ceiling:
        return None, "quantity must be at most %d" % ceiling
    return quantity, None


def transfer(op, amount, carried, banked):
    """(carried, banked, None) after moving `amount` - or (None, None, the
    400's words) when the side it comes from does not hold it. THE TOTAL IS
    CONSERVED: what leaves one side is exactly what arrives at the other."""
    if op == "deposit":
        if amount > carried:
            return None, None, "Cannot deposit %d - only %d carried." % (amount, carried)
        return carried - amount, banked + amount, None
    if amount > banked:
        return None, None, "Cannot withdraw %d - only %d banked." % (amount, banked)
    return carried + amount, banked - amount, None
