"""carry/services.py - the rules: how high a cell stacks, what a positional
array may hold, which bag this is, where a grant lands, which cells a take
empties and what a drag does. Pure: no database, no request."""
import hashlib


def stack_limit(definition, ceiling):
    """How many of an item may sit in one cell, from its catalogue row (None
    when the server does not recognise it)."""
    if definition is None:
        return ceiling
    if not definition.get("stackable", False):
        return 1
    return max(1, min(int(definition.get("max_stack", 1)), ceiling))


def positional_items(cells, field, limit_of):
    """
    Validate a positional item array - the backpack and the bank are the same
    shape and this is the only place that shape is checked.

    Returns (parsed, None) or (None, the 400's message). The WHOLE array is
    validated before the caller writes any of it: a bad entry at index 17 must
    not leave the first seventeen stored, because a half-saved bag with no error
    is worse than a refused write.
    """
    parsed = []
    for index, cell in enumerate(cells):
        if cell is None:
            continue
        if not isinstance(cell, dict):
            return None, "%s[%d] must be an object or null" % (field, index)

        item_id = str(cell.get("item_id", "")).strip()
        if not item_id or len(item_id) > 64:
            return None, "%s[%d].item_id must be 1-64 characters" % (field, index)

        raw_quantity = cell.get("quantity", 1)
        if isinstance(raw_quantity, bool):
            return None, "%s[%d].quantity must be a positive integer" % (field, index)
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            return None, "%s[%d].quantity must be a positive integer" % (field, index)
        if quantity <= 0:
            return None, "%s[%d].quantity must be a positive integer" % (field, index)

        # The ceiling, which "positive integer" alone does not give you. Without
        # it a client could declare a cell holding a billion potions and this
        # would store it: the table only checks quantity > 0.
        #
        # A known item is capped at its own max_stack, which is the rule the
        # game already plays by. An unknown one falls back to QUANTITY_CEILING,
        # because item ids are deliberately NOT validated against a list here -
        # that is what lets the game add an item without a matching server
        # deploy, and it is worth keeping.
        limit = limit_of(item_id)
        if quantity > limit:
            return None, ("%s[%d].quantity is %d, the most %s stacks to is %d"
                          % (field, index, quantity, item_id, limit))

        parsed.append((index, item_id, quantity))

    return parsed, None


def fingerprint(cells):
    """
    Which bag this is, as a short string: sha1 of "position:item_id:quantity"
    for every filled cell, joined with "|", in position order.

    `cells` is inventory_of()'s positional array - the exact thing every
    route that changes the carry answers with - so the client can work out the
    same fingerprint from what it was handed, with no extra field on any answer.
    CharacterData.bag_fingerprint() in the game is the other half, and both
    test suites pin the same bag to the same string.
    """
    parts = []
    for position, cell in enumerate(cells or []):
        if isinstance(cell, dict) and cell.get("item_id"):
            parts.append("%d:%s:%d" % (position, cell["item_id"], int(cell.get("quantity", 1))))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def cells_of(rows, capacity):
    """
    Everything carried as a POSITIONAL ARRAY of length `capacity`, with None
    in every empty cell - not as a list of the items that happen to exist.
    The bag's cells come first and the rest are the hotbar's keys, in order.

    The client's inventory is an array where index IS the cell, and returning a
    packed list would make the client responsible for rebuilding the gaps. It
    would get that right the first time and wrong the first time someone changed
    the capacity.

    THE HOTBAR RIDES IN THE SAME ARRAY on purpose. Every endpoint that changes
    what a character carries answers with this, and the client applies it in one
    place - so no response can update the bag and leave a key showing an item
    the server has just taken off it.
    """
    cells = [None] * capacity
    for row in rows:
        position = int(row["position"])
        # Defensive: a row outside the current capacity means the capacity was
        # reduced after it was written. Drop it from the view rather than
        # crashing on the index - the next write prunes it for real.
        if 0 <= position < capacity:
            cells[position] = {"item_id": row["item_id"], "quantity": int(row["quantity"])}
    return cells


def preferred_first(cells, prefer):
    """cells - (position, ...) tuples already in take order - with the one at
    `prefer` moved to the front. A stable sort, so the rest keep their order;
    unchanged when prefer is None or names a cell not in the list."""
    if prefer is None:
        return list(cells)
    return sorted(cells, key=lambda cell: int(cell[0]) != int(prefer))


def take_from_cells(rows, item_id, quantity, prefer=None):
    """
    Work out which cells to empty or reduce to remove quantity of item_id.

    Returns (list of (position, new_quantity_or_zero), None) or (None, how many
    were actually available) when there are not enough. Shared by the backpack
    and the bank because "take 5 potions out of a positional grid" is one rule,
    and the last time this project had that rule twice the two copies stopped
    agreeing about the stack ceiling.

    HIGHEST POSITION FIRST. Emptying the last cell of a split stack leaves the
    player's grid looking like they expect - things disappear from the end, not
    out of the middle.

    EXCEPT THE CELL THE PLAYER USED, when the request names one. `prefer` is
    that cell, and it is spent first when it holds the item. With the hotbar
    in the same rows as the bag, the highest cell holding a potion is usually a
    KEY - so without this, dragging the potions in your bag into the bank
    emptied the ones on key 1 and left the bag stack you dragged. A `prefer`
    that does not hold the item is ignored rather than refused: the request is
    still "move five potions", and the client's picture may be a moment old.
    """
    held = [(int(r["position"]), int(r["quantity"]))
            for r in rows if r["item_id"] == item_id]
    available = sum(q for _, q in held)
    if available < quantity:
        return None, available

    remaining = int(quantity)
    changes = []
    for position, held_qty in preferred_first(sorted(held, reverse=True), prefer):
        if remaining <= 0:
            break
        taken = min(held_qty, remaining)
        changes.append((position, held_qty - taken))
        remaining -= taken
    return changes, None


def backpack_take(held, quantity, prefer, keys_last, bag_cells):
    """
    Which carried cells a take of `quantity` empties or reduces: [(position,
    what is left there)], or None when fewer than that are held - and then
    NOTHING is to be written.

    `held` is [(position, quantity)] of the one item, HIGHEST POSITION FIRST,
    so a partly-used stack is emptied before a full one is broken into. That
    keeps the bag tidy across a long cooking run instead of leaving a trail of
    one-item stacks.

    `keys_last` spends the bag (cells below `bag_cells`) before the hotbar's
    keys. For a take the player did not point at - a trade takes a quantity,
    not a cell - so the potions on key 1 are the last to go rather than the
    first. See trade/routes.py, execute().

    `prefer` is the cell the player acted on - a potion drunk from key 3, a
    sword equipped from bag cell 12 - and is spent first when it holds the item.
    See take_from_cells() for why, since the hotbar sits in these same rows.
    """
    if sum(have for _position, have in held) < quantity:
        return None

    if keys_last:
        # A stable sort, so within the bag and within the keys the order is
        # still highest-first - a part-used stack still goes before a full one.
        held = sorted(held, key=lambda cell: int(cell[0]) >= bag_cells)

    remaining = quantity
    changes = []
    for position, have in preferred_first(held, prefer):
        if remaining <= 0:
            break
        taken = min(have, remaining)
        remaining -= taken
        changes.append((position, have - taken))
    return changes


def placement(occupied, item_id, quantity, definition, bag_cells):
    """
    Where a grant of `quantity` of `item_id` lands in a carry already holding
    `occupied` ({position: (item_id, quantity)}): [(position, item_id, new
    quantity)], or None when it does not fit - and then NOTHING is to be
    written. A half-completed pickup is the shape of bug that ends with an
    item in neither the bag nor the bag.

    TOPS UP EXISTING STACKS FIRST, then fills empty cells, the way the CLIENT
    would. That is not a nicety: the response hands `inventory` back as the
    authoritative layout, and the client applies it. If this dropped every
    pickup into the lowest free cell while InventoryContainer.add_stack_partial()
    merged onto a part-used stack, the two would lay the same bag out
    differently and the player would watch their potions split across cells on
    every loot.

    THE BAG ONLY - the cells below `bag_cells`, never a hotbar key, not even to
    top up a stack already on one. A key holds what the player put there; loot
    quietly growing the potion stack on key 1 would be the server rearranging
    the one part of the carry the player arranges by hand.
    """
    definition = definition or {}
    stackable = bool(definition.get("stackable", False))
    max_stack = int(definition.get("max_stack", 1)) if stackable else 1
    if max_stack < 1:
        max_stack = 1

    occupied = dict(occupied)
    remaining = int(quantity)
    writes = []  # (position, item_id, new_quantity)

    if stackable:
        for position in sorted(occupied):
            if remaining <= 0 or position >= bag_cells:
                break
            held_id, held_qty = occupied[position]
            if held_id != item_id or held_qty >= max_stack:
                continue
            room = max_stack - held_qty
            moved = min(room, remaining)
            writes.append((position, item_id, held_qty + moved))
            remaining -= moved

    for position in range(bag_cells):
        if remaining <= 0:
            break
        if position in occupied:
            continue
        moved = min(max_stack, remaining)
        writes.append((position, item_id, moved))
        occupied[position] = (item_id, moved)
        remaining -= moved

    if remaining > 0:
        return None
    return writes


def move_after(source, target, moving, there, item_id, limit):
    """
    What the two cells of a drag hold afterwards ({position: (item_id,
    quantity)}), `moving` having been dragged from `source` onto `target`,
    which held `there` (None when empty): onto an empty cell it MOVES, onto
    the same stackable item it MERGES up to `limit` (the rest stays where it
    was), onto anything else it SWAPS.
    """
    if there is None:
        return {target: moving}
    if there[0] == moving[0] and limit > 1:
        room = max(0, limit - there[1])
        shifted = min(room, moving[1])
        after = {target: (item_id, there[1] + shifted)}
        if moving[1] - shifted > 0:
            after[source] = (item_id, moving[1] - shifted)
        return after
    return {target: moving, source: there}
