"""shop/services.py - the rules: what one row of a shop's shelf says. Pure: no
database, no request. (What a shop stocks and what it charges or pays are the
catalogue's - gamedata.shop_price() and shop_sell_price() - so a panel and the
till can never disagree about a price.)"""


def stock_row(item_id, definition, price, free=False):
    """One item on the shelf: what it is, what it needs, and what the till
    charges for it - all from the server's own ItemData copy. free marks what
    the shop gives away (0.69.0): price 0, one each, kept rather than sold."""
    return {
        "item_id": item_id,
        "display_name": definition.get("display_name", item_id),
        "tier": definition.get("tier", 1),
        "type_name": definition.get("type_name", ""),
        "required_level": definition.get("required_level", 1),

        # THE OTHER HALF OF THE REQUIREMENT. A cooked fish is gated on
        # COOKING, not on character level - see itemdata.gd's comment on
        # required_skill - so a row carrying only required_level would let
        # the panel print "Needs nothing" for a Reef Clown that needs 70
        # cooking. Sent from the server's own ItemData copy for the same
        # reason the price is: one source, and a stale client cannot
        # disagree with it.
        "required_skill": definition.get("required_skill", ""),
        "required_skill_level": definition.get("required_skill_level", 1),

        # THE THIRD HALF, and the one a weapon rack needs most. A sword
        # and a staff sit side by side at the same price and the same
        # level, and nothing on the row said which of them a warrior can
        # actually hold - so the first time you find out is when the
        # equip endpoint refuses it, after you have paid.
        #
        # An empty list means "anyone", which is most of the catalogue, so
        # the panel prints nothing rather than "Needs: anyone".
        #
        # FROM THE SERVER'S OWN COPY, like the price and the level, for
        # the reason those are: one source, and a stale client cannot
        # disagree with it.
        "required_classes": list(definition.get("required_classes") or []),

        # WHAT IT IS, for the same row. A tooltip that says "Ember Staff"
        # and a price is a tooltip that made you click to learn anything.
        "equip_slot_name": definition.get("equip_slot_name", ""),
        "damage": int(definition.get("damage", 0) or 0),
        "armor_value": int(definition.get("armor_value", 0) or 0),

        "max_stack": definition.get("max_stack", 1),
        "price": price,
        "free": bool(free),
    }


def already_words(display_name, where):
    """The refusal for a second free piece: what, where the first one is, and
    the rule (shop_buy, 0.69.0)."""
    if where == "worn":
        return "You are already wearing your free %s. The shop gives one to each character." % display_name
    if where == "banked":
        return ("Your bank already holds a free %s. Take that one out instead - "
                "the shop gives one to each character." % display_name)
    return "You already have a free %s. The shop gives one to each character." % display_name


def free_words(display_name):
    """Why a free piece will not sell or trade (0.69.0)."""
    return ("%s came free from the shop, so it is yours to keep: "
            "it cannot be sold or traded." % display_name)
