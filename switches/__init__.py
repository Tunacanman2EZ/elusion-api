"""switches - the server's switches and the owner's voice: closing the server
and opening it again, the oldest game allowed in, PvP, trading, the
co-owners, an announcement to everyone playing - and /api/status, which tells
every client how the switches stand (a package since 18 Oct; the routes are as
they were in app.py).

    GET  /api/status              no login: is the server up, is it closing,
                                  is PvP on, may players trade, which builds
                                  may connect
    POST /api/server/maintenance  close the server, with a grace window to
                                  save in, or open it again
    POST /api/server/minbuild     the oldest client build allowed in (0 lets
                                  every build in)
    POST /api/server/pvp          player-versus-player on or off
    POST /api/server/trade        new trades allowed, or not (open ones may
                                  finish)
    GET  /api/server/coowners     whether the co-owners are in, and who they are
    POST /api/server/coowners     let them in or take them out - the owner alone
    POST /api/server/broadcast    say something to everyone playing

EACH SWITCH IS ONE ROW IN server_settings, written with who threw it and when -
even when it does not change, so the table keeps that record. The owner's:
every route here but the status read is behind an owner check, which answers
a 404 rather than a 403; the four that change the world (maintenance,
the build, PvP, trading) ask for the password again; and the co-owners'
switch is THE owner's (require_the_owner), since a co-owner who could throw it
could keep it on.

SAID OUT LOUD, or not, on purpose. Closing and opening, PvP and trading are
announced in the world (post_broadcast()), because a rule nobody was told is
discovered the hard way; the build gate is not, because the people it refuses
cannot hear the world - they read it off /api/status, the one route the gate
never refuses.

Built like players/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. WHAT STAYS IN app.py: what each switch READS as - the keys, the
rows' readers and the way each fails (maintenance_state(), pvp_enabled(),
trades_enabled(), min_client_build(), co_owners_on()) - because the gates that
ask them run on every request: the maintenance window inside require_auth,
the build gate before it, the co-owners inside role_for(). And the broadcast
poll, GET /api/server/broadcasts: it is the heartbeat, and it reads every
area's news, so it is the composition root's own page.
"""
from .routes import register  # noqa: F401
from .services import MAINTENANCE_DEFAULT_GRACE_SECONDS, MAINTENANCE_MAX_GRACE_SECONDS  # noqa: F401
