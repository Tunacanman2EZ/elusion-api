"""consuming/services.py - the rules: what the catalogue's "may this character
use that" says to the player, and what a meal's buff looks like on the wire.
Pure: no database, no request."""


def food_buff_view(row):
    """A food_buffs row as every answer carries it - {"item_id", "kind" (the
    name), "amount", "ends_at" (unix seconds)} - or None for no row. The one
    shape: the consume answer, the character's load (saves/) and the presence
    ticket (players/) all read it, so FoodBuff in the game reads one thing."""
    if row is None:
        return None
    return {"item_id": str(row["item_id"]), "kind": str(row["kind"]),
            "amount": int(row["amount"]), "ends_at": int(row["ends_at"])}


def refusal(verdict):
    """The answer for a catalogue verdict that is not ok: (body, status)."""
    reason = verdict["reason"]
    if reason == "level":
        return {
            "error": "Forbidden",
            "message": "You need level %d." % verdict["needs"],
        }, 403
    if reason == "skill":
        return {
            "error": "Forbidden",
            "message": "You need %s level %d." % (
                verdict["skill"].capitalize(), verdict["needs"]),
        }, 403
    # "unknown" and "nottype" are both "the server will not destroy this", and
    # neither answer tells the caller anything it did not itself send.
    return {"error": "Conflict", "message": "That cannot be used."}, 409
