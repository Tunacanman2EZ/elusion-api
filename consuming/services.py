"""consuming/services.py - the rules: what the catalogue's "may this character
use that" says to the player. Pure: no database, no request."""


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
