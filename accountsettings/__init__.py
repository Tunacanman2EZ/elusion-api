"""accountsettings - what a player sets on their own account: the address
that can recover it, and the colour their name is drawn in (a package since
19 Oct; the routes are as they were in app.py).

    GET  /api/account/email          the recovery address, masked, and whether
                                     it has been confirmed
    POST /api/account/email          set or change it - the current password
                                     asked, a confirmation code mailed to it
    POST /api/account/email/verify   confirm it with that code
    PUT  /api/account/name-colour    the hue the player's name is drawn in

THE RECOVERY ADDRESS IS A KEY TO THE ACCOUNT, so changing it needs the
current password - the one thing a stolen session does not carry - and a new
address is UNVERIFIED until a code mailed to it comes back: until then it
cannot receive a reset, so a typo costs a re-entry, not an account. With no
mail set up the change is refused (503) rather than answered with "check that
inbox" for a mail that can never come.

A HUE, NOT A COLOUR. The game fixes saturation and brightness so every choice
reads against the world; storing a whole colour would let a patched game
paint black on black. Everybody chooses, staff included: rank is the badge
over the name, which nobody chooses.

Built like kill/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. WHAT STAYS IN app.py: the mail (the suites stand in for it),
the codes (_new_code(), _store_code(), _consume_code() - a reset and a staff
login use them too), the address's cleaning and masking (the recovery routes
and the staff login code use them), the login log, the client's address, and
the hue's ceiling with how a hue is shown (name_hue_of(), which every list of
names reads).
"""
from .routes import register  # noqa: F401
