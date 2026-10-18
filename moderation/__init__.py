"""moderation - what staff do about a player: the sanctions, the reports, the
mutes (a package since 18 Oct; the routes are as they were in app.py).

    POST /api/staff/ban              ban: a mod for up to MAX_MOD_BAN_DAYS, dev
                                     and the owner for longer or for good
    POST /api/staff/kick             sign them out everywhere, without a ban
    POST /api/staff/unban            lift a ban
    PUT  /api/staff/role             change a rank - never to your own or above
    POST /api/staff/mute             stop them talking in chat for a while
    POST /api/staff/unmute           let them talk again
    POST /api/chat/report            a player hands a chat line to staff
    POST /api/report/player          ... or a player (game 0.43.0)
    GET  /api/staff/reports          the Reports tab: a card per reported
                                     player, worst first
    POST /api/staff/reports/resolve  close a line's reports, or a player's

THE SANCTION LADDER: a kick, a mute, a bounded ban, a permanent one. Every one
is require_role("mod") at the door and within the caller's reach inside -
app.py's can_act_on(), strictly above, through _moderation_target() - and a
refusal is the same 404 whether the account does not exist or outranks you.
Every one is logged (staff_actions) in the transaction that does it, and a
kick, mute or ban closes the open reports about that account, so the Reports
tab does not have to be tidied afterwards.

A REPORT COPIES THE LINE, so it survives the line being deleted - which is
usually what a report leads to - and only a line the reporter could have read
can be reported (chat/'s can_read_row()), so the route cannot find out which
ids are somebody's whispers. Closed reports are kept REPORT_KEEP_SECONDS and
pruned on write; open ones never are.

Built like chat/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs, and hands back the two counts the broadcast poll shows staff.
WHAT STAYS IN app.py: the rank ladder (ROLES, role_for(), can_act_on()) and
_moderation_target(), which every staff area shares; ban_state(), which signing
in asks; chat_mute_state() and describe_mute(), which chat asks; the
moderation log's writer (log_staff_action()), which every staff action calls;
and the three tunables a suite turns down, REPORTS_PER_HOUR,
REPORT_LINES_PER_PLAYER and MUTE_MAX_MINUTES_MOD, asked for on every use.
"""
from .routes import register  # noqa: F401
from .services import (MAX_MOD_BAN_DAYS, MUTE_MAX_MINUTES, REPORT_KEEP_SECONDS,  # noqa: F401
                       REPORT_PLAYERS_SHOWN, REPORT_REASONS)
