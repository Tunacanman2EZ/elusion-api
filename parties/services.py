"""parties/services.py - the rules: how big a party gets, how long an
invitation waits, how a party is shown, and what a voice relay answer may
carry. Pure: no database, no request."""
import re

# Two to MAX_PARTY_MEMBERS players. An invitation waits PARTY_INVITE_SECONDS
# for an answer, like a guild's but short - a party is made for what you are
# doing now.
MAX_PARTY_MEMBERS = 4
PARTY_INVITE_SECONDS = 120
# How many invitations one player may have waiting at once.
MAX_PARTY_INVITES = 8

# PARTY VOICE's relay credentials: how long the relay is asked to make them
# last, how long a player's are handed out again while they have this long
# left, and how often one player may have the relay asked in an hour.
VOICE_TURN_TTL = 24 * 3600
VOICE_TURN_REUSE_SECONDS = 12 * 3600
VOICE_FETCHES_PER_HOUR = 30


def party_answer(party_id, rows, playing, seen, now, online_window):
    """A party as the Party window draws it: who is in it and the character
    each is playing - class, level, area and health, from their save - and
    whether they are on. `rows` are data.member_rows(), `playing` and `seen`
    data.playing_saves() and data.last_seen()."""
    members = []
    leader = ""
    for row in rows:
        save = playing.get(int(row["user_id"]))
        last = seen.get(int(row["user_id"]), 0)
        if int(row["is_leader"] or 0):
            leader = row["username"]
        members.append({
            "username": row["username"],
            "leader": bool(int(row["is_leader"] or 0)),
            "online": bool(last and now - last <= online_window),
            "character": save["name"] if save is not None else "",
            "class_id": save["class_id"] if save is not None else "",
            "level": int(save["level"] or 1) if save is not None else 0,
            "area": save["area"] if save is not None else "",
            "hp": int(save["hp"] or 0) if save is not None else 0,
            "max_hp": int(save["max_hp"] or 0) if save is not None else 0,
        })
    return {"id": int(party_id), "leader": leader, "members": members,
            "size": len(members), "capacity": MAX_PARTY_MEMBERS}


def clean_ice_servers(servers):
    """Only what a browser needs from the relay's answer, checked: STUN and
    TURN addresses (none on port 53, which browsers block - Cloudflare's
    advice), with the username and password the TURN ones need."""
    if isinstance(servers, dict):
        servers = [servers]
    if not isinstance(servers, list):
        return []
    out = []
    for entry in servers[:8]:
        if not isinstance(entry, dict):
            continue
        urls = entry.get("urls")
        urls = [urls] if isinstance(urls, str) else urls
        if not isinstance(urls, list):
            continue
        kept = [u for u in urls[:12] if isinstance(u, str) and len(u) <= 200
                and re.match(r"^(stun|turn|turns):[A-Za-z0-9.:-]+(\?transport=(udp|tcp))?$", u)
                and not re.search(r":53(\?|$)", u)]
        if not kept:
            continue
        item = {"urls": kept}
        if any(u.startswith("turn") for u in kept):
            user, cred = entry.get("username"), entry.get("credential")
            if not isinstance(user, str) or not isinstance(cred, str) or not user or not cred \
                    or len(user) > 512 or len(cred) > 512:
                continue
            item["username"], item["credential"] = user, cred
        out.append(item)
    return out
