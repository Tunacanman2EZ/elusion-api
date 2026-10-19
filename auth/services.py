"""auth/services.py - the rules: what a staff login's second step is called
and how often it may send a code, how long a computer stays trusted, and how a
typed code is read. Pure: no database, no request. (Whether an account's logins
take a code at all - staff_login_protected(), and the ELUSION_STAFF_LOGIN_CODES
switch - is app.py's: the staff powers page asks it too.)"""
import re

# THE STAFF LOGIN CODE'S PURPOSE in auth_codes, beside the reset and the
# verify codes, and at most one new code a minute, so a leaked password cannot
# flood the inbox (STAFF LOGIN CODES, in app.py, says the rest).
STAFF_LOGIN_CODE_PURPOSE = "staff-login"
STAFF_LOGIN_CODE_RESEND_SECONDS = 60


# TRUSTED DEVICES: THE CODE ONCE PER COMPUTER, NOT ONCE PER LOGIN.
#
# A code on every login was the owner's own complaint on the first day he had
# it: every sign-in meant a trip to the inbox. What the code is FOR is a
# password used from somewhere that is not the staff member's own computer, and
# that question only needs asking once per computer. So a login that got in
# with a code is answered with a `device_token`, the game keeps it, and a later
# login from the same computer that sends it back needs no code.
#
# - Only the hash is stored, like a session token.
# - TRUSTED_DEVICE_DAYS from the code, then a new code. Not sliding: a computer
#   stays trusted for a month from when it proved itself, not for ever by use.
# - At the rank it was trusted at. A promotion (or a demotion) is a new code,
#   so somebody made a mod proves their computer as a mod. Compared against
#   role_for(), which is also what set_role.py and ELUSION_OWNER feed, so no
#   route that changes a rank has to remember to revoke anything.
# - Gone on a password change, a recovery reset or "log out everywhere" - the
#   three things somebody does when they think the account is not only theirs.
# - The newest TRUSTED_DEVICES_KEPT per account.
# - A device token is never enough on its own: it is checked only after the
#   password and the ban check have passed.
TRUSTED_DEVICE_DAYS = 30
TRUSTED_DEVICES_KEPT = 5


def typed_code(raw):
    """A code as typed. Spaces are how people copy "183 774" out of a mail;
    anything longer than a code is cut before it reaches the hash."""
    return re.sub(r"\s", "", str(raw or ""))[:16]
