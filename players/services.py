"""players/services.py - the rules: how long each list may be, how long a
presence ticket lives, where the game opens its socket, and which of the things
a character holds are its pets and its gear. Pure: no database, no request."""
import json

# The most names the nearby list will return. A cap rather than paging: this
# feeds a panel you glance at to find somebody to trade with, and a list longer
# than this is one nobody reads to the end of anyway.
NEARBY_LIMIT = 20

# The most the who-is-online menu returns. A cap rather than paging, like
# NEARBY_LIMIT above: a list nobody can scroll to the end of has stopped being a
# list, and at fifty concurrent players - the size this server is provisioned
# for - it is not reached. If it ever is, that is a good problem and the fix is
# paging rather than a bigger number.
ONLINE_LIST_LIMIT = 60

# A TICKET LIVES TWO MINUTES and the game renews it every minute, which is how
# a level-up or a new guild reaches other screens (presence.py's header).
PRESENCE_TICKET_SECONDS = 120

# Where presence.py listens - its PATH - and so where the proxy sends it.
PRESENCE_PATH = "/ws/presence"


def socket_url(configured, proxy_hops, port, scheme, host):
    """
    Where the game should open its socket.

    ELUSION_PRESENCE_URL when it is set. Otherwise, BEHIND THE PROXY (Caddy,
    ELUSION_TRUSTED_PROXIES above 0) it is this same host under /ws/presence,
    wss:// when the page came over https - one address for the game, the API and
    the socket, as the browser needs. Run locally, it is the presence process's
    own port on the host the game reached the API by.

    `configured` is ELUSION_PRESENCE_URL ("" when unset), `proxy_hops` the
    trusted proxy count and `port` the presence process's own, all app.py's
    settings; `scheme` and `host` are the request's.
    """
    if configured:
        return configured
    if proxy_hops > 0:
        # The request's scheme is already the proxy's (ProxyFix, x_proto, in
        # app.py), and Caddy passes the page's Host through unchanged.
        return "%s://%s%s" % ("wss" if scheme == "https" else "ws", host, PRESENCE_PATH)
    host = host or "127.0.0.1"
    if host.startswith("["):
        host = host[:host.index("]") + 1]
    else:
        host = host.split(":")[0]
    return "ws://%s:%d%s" % (host, port, PRESENCE_PATH)


def pets_among(item_ids, catalogue):
    """The pets among what a character holds, each once, sorted."""
    return sorted({item_id for item_id in item_ids
                   if (catalogue.item_row(item_id) or {}).get("type_name") == "PET"})


def gear_of(equipment, carried, catalogue):
    """
    Every piece a character could fight with, sorted: what it wears (the save's
    equipment, as stored) and every carried piece that can be worn.

    Carried as well as worn, so a sword swapped in mid-fight was always within
    its bounds. Stored equipment that does not parse wears nothing.
    """
    try:
        worn = json.loads(equipment or "{}")
    except (TypeError, ValueError):
        worn = {}
    gear = {str(v) for v in (worn.values() if isinstance(worn, dict) else []) if isinstance(v, str) and v}
    gear |= {item_id for item_id in carried if catalogue.equip_slot_for(item_id)}
    return sorted(gear)
