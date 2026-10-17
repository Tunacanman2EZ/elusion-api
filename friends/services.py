"""friends/services.py - the rules, pure: no database, no request."""


def sort_rows(rows, me):
    """THREE LISTS OUT OF ONE READ: ({"friends", "incoming", "outgoing"} of
    the other account's id, {other id: the row}). Which list a row belongs in
    is its state and which end of it this account is on."""
    buckets = {"friends": [], "incoming": [], "outgoing": []}
    other_of = {}
    for row in rows:
        other = int(row["addressee_id"]) if int(row["requester_id"]) == me \
            else int(row["requester_id"])
        other_of[other] = row
        if row["state"] == "accepted":
            buckets["friends"].append(other)
        elif int(row["requester_id"]) == me:
            buckets["outgoing"].append(other)
        else:
            buckets["incoming"].append(other)
    return buckets, other_of


def listed(buckets, other_of, people, person):
    """Each list as the panel reads it: `person(row)` for each account still
    there, with when the link was made or decided - ONLINE FIRST, then by
    name, so the useful half is at the top without the game sorting it.

    An account deleted between the two reads is skipped rather than shown as
    a nameless row (ON DELETE CASCADE will have taken the link too)."""
    out = {}
    for key, id_list in buckets.items():
        entries = []
        for other in id_list:
            row = people.get(other)
            if row is None:
                continue
            entry = person(row)
            entry["since"] = int(other_of[other]["decided_at"] or other_of[other]["created_at"])
            entries.append(entry)
        entries.sort(key=lambda e: (not e["online"], e["username"].casefold()))
        out[key] = entries
    return out


def limits_answer(most_friends, most_pending, accepted, pending_sent):
    return {
        "friends": most_friends,
        "pending_sent": most_pending,
        "friends_used": accepted,
        "pending_sent_used": pending_sent,
    }
