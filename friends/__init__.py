"""friends - who is whose friend: ask, answer, list, end (a package since
16 Oct; the routes are as they were in app.py).

    GET  /api/friends            your friends, who is online, requests waiting
    POST /api/friends/request    ask somebody - or accept, if they asked first
    POST /api/friends/respond    accept or decline a request sent to you
    POST /api/friends/remove     end it, or withdraw a request you sent

A request has to be accepted before either side appears on the other's
list. The alternative - adding someone the moment you type their name -
means anyone who knows your name can watch when you are online, and there is
no point in a friends list you cannot refuse entry to. ONE ROW PER PAIR, in
the friends table (app.py's init_db() says why it is one table, not two),
stored in the direction it was asked; a decline deletes the row, and re-asking
is policed by how many can be outstanding at once.

Built like killrecord/ (ARCHITECTURE.md): routes.py (the edge), services.py
(the rules, pure), data.py (the SQL). It never imports app.py; register() is
handed what it needs - how a person is shown and who is online are app.py's,
shared with guilds, parties and trade.
"""
from .routes import register  # noqa: F401
