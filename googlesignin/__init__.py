"""googlesignin - signing in with Google (game 0.101.0).

    POST /api/auth/google/start      a sign-in begun: a code for this computer
    GET  /api/auth/google/page       our page with Google's button, for a code
    POST /api/auth/google/finish     the page's: Google's signed token, checked here
    POST /api/auth/google/poll       the game's: is the code done yet
    POST /api/auth/google/register   a new account for a new Google account
    GET  /api/account/google         is this account linked
    POST /api/account/google/link    link Google to this account (password once)
    POST /api/account/google/unlink  and take it off again

A player said "Google account to play or I am not registering". The owner:
"lets do it because i agree i can still use my key for admin stuff". The plan
is the commit folder's GOOGLE_SIGNIN_PLAN.txt.

ONE WAY FOR EVERY GAME. Google's sign-in cannot run inside Godot, so the game
asks for a code (start), opens our page in the web browser (page), the player
picks their Google account there, and the page hands Google's token to us
(finish). The game asks every two seconds (poll) and, once the code is done,
is signed in exactly as a password login would sign it in. The browser build
and the downloaded game take the same road.

WHAT IS CHECKED, AND WHERE. Google's token is checked HERE, not trusted from
the page: its signature against Google's own published keys, that it was made
for this server's Client ID (ELUSION_GOOGLE_CLIENT_ID), that it has not run
out, that Google made it, and that Google has verified the email. The code is
long and random, kept as its hash, good for GOOGLE_CODE_SECONDS, used once,
and only the computer that asked for it (its install id) can collect it. The
page shows a short check code the game shows too, against somebody sending a
player a link of their own.

EVERY RULE THE PASSWORD LOGIN HAS: bans (the ban and its reason), the closed
server, one login at a time, the session tied to the computer, the address
and computer recorded, and for a new account the throttles and both
ban-evasion checks. And two more:
- ONE GOOGLE ACCOUNT, ONE GAME ACCOUNT (google_accounts.sub is UNIQUE): a
  banned player cannot make another account with the same Google account.
- STAFF NEVER SIGN IN WITH GOOGLE: a mod, dev, co-owner or the owner signs in
  with the password and the email code, as before. Google is refused for
  them, cannot be linked to them, and no Google sign-up may take a staff name.

A NEW ACCOUNT MADE WITH GOOGLE has no password anybody knows (the process's
timing stand-in hash, whose password was never kept), and Google's verified
email as its recovery address - so "Forgot password" can give it one later.

Built like minigames/: routes.py (the edge), services.py (pure, and the one
place Google's keys are fetched), data.py (the SQL), schema.py (the tables).
It never imports app.py: register() is handed what it needs.
"""
from .routes import register  # noqa: F401
