"""equipment/services.py - the rules: how the stored equipment map is read,
and what the catalogue's "may this character wear that" says to the player.
Pure: no database, no request."""
import json


def worn_of(raw):
    """The stored equipment map, parsed. Always a dict, even for a row that
    predates it, or one that cannot be read - the character is still playable
    with nothing worn."""
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def refusal(verdict):
    """The 403 for a catalogue verdict that is not ok (an "unknown" item is
    the route's 400)."""
    reason = verdict.get("reason", "")
    if reason == "notgear":
        return {"error": "Forbidden", "message": "That is not equipment."}, 403
    if reason == "class":
        return {"error": "Forbidden",
                "message": "Your class cannot wear that.",
                "allowed": verdict.get("allowed", [])}, 403
    if reason == "level":
        return {"error": "Forbidden",
                "message": "That needs level %d." % int(verdict.get("needs", 0)),
                "needs": int(verdict.get("needs", 0))}, 403
    return {"error": "Forbidden", "message": "That cannot be equipped."}, 403
