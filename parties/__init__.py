"""parties - a few players together for a while, and their voice call (a
package since 18 Oct; the routes are as they were in app.py).

    GET  /api/party           your party and the invitations waiting on you
    POST /api/party/invite    ask somebody to join yours - or to start one
    POST /api/party/respond   accept or turn down an invitation
    POST /api/party/leave     leave it
    POST /api/party/kick      remove somebody (its leader only)
    POST /api/party/lead      hand it on (its leader only)
    GET  /api/party/voice     the relay a party's voice call goes through

The owner: "we need to be able to start parties so we need a tab for
parties", and of what a party should do: its members in the Party window,
"Party chat", "Party on the map". Two to MAX_PARTY_MEMBERS players; the leader
invites, removes and hands it on; anybody leaves; down to one, it is gone.
Asked, never put in: an invitation waits PARTY_INVITE_SECONDS for an answer.
Not a guild - no name, no ranks, no cost.

PARTY VOICE (game 0.43.0) is the browser's own - WebRTC - from each game in a
party to each other, set up through the presence socket (presence.py). It
never passes through this server. THROUGH A RELAY, ALWAYS: every call goes
through a TURN relay (Cloudflare's), so each player sees only the relay, never
another player's address. GET /api/party/voice hands out short-lived relay
credentials, made from the server's TURN key. The key is a server setting
and the call out to Cloudflare is the server's, so both stay in app.py
(voice_settings(), _turn_fetch()) and are handed in: the route passes the key
from one to the other and keeps nothing.

Built like guilds/ (ARCHITECTURE.md): routes.py (the edge), services.py
(the rules, pure), data.py (the SQL). It never imports app.py; register() is
handed what it needs. Party chat is chat's, and the poll's view of a party
(_own_party()) is the poll's: both stay in app.py and read through
party_membership(), party_member_rows() and _party_invites_to(), which are
this package's membership(), member_rows() and invites_to().
"""
from .data import invites_to, member_rows, membership  # noqa: F401
from .routes import register  # noqa: F401
from .services import (VOICE_FETCHES_PER_HOUR, VOICE_TURN_REUSE_SECONDS,  # noqa: F401
                       VOICE_TURN_TTL, clean_ice_servers)
