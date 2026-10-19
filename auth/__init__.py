"""auth - an account's way in and out: making one, logging in (with a staff
member's second step), the heartbeat, a remembered login carried into a new
game, the password typed again, changing it, and getting back in by email (a
package since 19 Oct; the routes are as they were in app.py).

    POST /api/auth/register    a new account, and its first session
    POST /api/auth/login       a session for the right password - and, for
                               staff with a confirmed address, the right code
    GET  /api/auth/session     the heartbeat: is this token still good
    POST /api/auth/resume      a remembered login, moved onto a new token
    POST /api/auth/confirm     the password typed again, for the routes that ask
    POST /api/auth/logout      this session ends
    POST /api/auth/logout-all  every session on the account ends
    POST /api/auth/password    a new password; every session ends, one is issued
    POST /api/auth/recover     a reset code by email - always the same answer
    POST /api/auth/reset       a new password with that code; every session ends

THE FRONT DOOR'S RULES ARE ALL STILL HERE, in the order that makes them work:
a closed server refuses before any lookup or hash; the address throttle comes
before the account lookup and any hashing, so a spray costs one indexed count;
the account lockout before the password check; the password is always checked
against something, a dummy when there is no account, so a missing name and a
wrong password cost the same; the ban after the password, so the route cannot
be asked who is banned; the staff step after the ban. Recovery never says
whether an address is on an account - same words, same work, same time.

WHAT IS NOT HERE, ON PURPOSE. What every request passes through is app.py's
and is handed in: the token check (require_auth) and the session it reads,
issue_token() and the token lifecycle (one login at a time, theft, the
step-up), the throttles and the login log, bans and the evasion checks, the
install binding, the staff login code's switch, the code helpers and the
mail - which the recovery-address routes use as well.

Built like kill/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs.
"""
from .routes import register  # noqa: F401
from .services import (STAFF_LOGIN_CODE_PURPOSE, STAFF_LOGIN_CODE_RESEND_SECONDS,  # noqa: F401
                       TRUSTED_DEVICE_DAYS, TRUSTED_DEVICES_KEPT)
