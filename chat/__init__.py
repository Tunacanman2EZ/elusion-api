"""chat - what the players say: world chat and its four other channels,
pictures in chat, taking a line down, and the ignore list (a package since 18
Oct; the routes are as they were in app.py).

    POST /api/chat/send            say something - in world, a whisper, to
                                   friends, guild or party - with or without
                                   a picture
    GET  /api/chat                 everything said in a channel since an id,
                                   and what was taken down
    POST /api/chat/image           hand the server a link; get a picture back
    POST /api/chat/upload          send a picture straight from the player's
         /api/chat/image/upload    machine (the old path, still served)
    GET  /api/chat/image/<id>      a picture's bytes, to somebody who may see it
    POST /api/chat/delete          take a line back down (staff)
    GET  /api/ignores              the players you have chosen not to hear
    POST /api/ignores              stop hearing one
    POST /api/ignores/remove       hear them again

THE CLIENT POLLS IT exactly the way it polls broadcasts, with the same
"everything after id N" cursor, so the two feeds merge into one window on the
client without either one having to know about the other.

IGNORE is the player's own: somebody's lines stop reaching you, in every
channel, and their whispers, friend requests and trades to you are refused.
Filtered in the read's own WHERE clause, like everything else in chat, so there
is no second pass to forget. STAFF CANNOT BE IGNORED: a mod telling you to stop,
and the owner announcing a restart in chat, have to arrive.

Built like guilds/ and parties/ (ARCHITECTURE.md): routes.py (the edge),
services.py (the rules, pure - Pillow's decoding included), data.py (the SQL).
It never imports app.py; register() is handed what it needs, and HANDS BACK
WHAT OTHER AREAS ASK OF CHAT - post_chat() (the events and the suites post
lines), can_read_row() (a report is only for a line you were shown),
may_see_image() and ignores() (trades, friends, parties and the players menu
refuse somebody who ignores you). app.py keeps them under the names it always
called them by.

WHAT STAYS IN app.py: the broadcasts (the server's own voice, which cannot be
muted or ignored), clean_player_text() (every name and line a player types),
reporting and muting (moderation's), the friends table's read of who accepted
whom, and the one call out to fetch a linked picture (_fetch_image_bytes(),
every hop checked against the server's own network). The tunables a suite
turns down - CHAT_KEEP_PER_CHANNEL and IGNORE_LIMIT - stay there too, asked for
on every use.
"""
from .data import IGNORED_AUTHORS_CLAUSE, ignores  # noqa: F401
from .routes import register  # noqa: F401
from .services import (CHAT_BUCKET_CAPACITY, CHAT_DEFAULT_CHANNEL, PILLOW_AVAILABLE,  # noqa: F401
                       CHAT_DELETION_WINDOW_SECONDS, CHAT_PAGE_LIMIT, CHAT_RETENTION_SECONDS,
                       CHAT_TOKENS_PER_SECOND, IMAGE_MAX_BYTES, IMAGE_MAX_SIDE, MAX_CHAT_LENGTH)
