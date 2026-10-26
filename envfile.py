"""
envfile.py - read a .env beside the code into the environment, FIRST.

    import envfile
    envfile.load()          # before anything that reads os.environ

WHY THIS IS ITS OWN FILE
------------------------
app.py used to read its .env with a helper defined 400 lines down the file. By
the time it ran, three settings had already been read: `import gamedata` had
taken ELUSION_GAMEDATA (line 8), DB_PATH had taken ELUSION_DB, and
TRUSTED_PROXY_HOPS had taken ELUSION_TRUSTED_PROXIES. A .env naming any of
those three was read, put into the environment, and ignored - silently, with
the server booting fine against elusion.db beside the code and a proxy count
of 0. ELUSION_OWNER and the mail settings worked only because they happen to
be read further down than the helper.

wsgi.py was worse off: its preflight reads every setting BEFORE it imports
app, so under gunicorn or waitress nothing in .env existed while the checks ran.
It warned "ELUSION_OWNER is not set" about an owner the .env named.

A module of its own is the fix that cannot drift. An import is the first thing
in app.py and wsgi.py, so there is no line above it to read a setting early,
and moving a block around inside app.py cannot quietly put one there again.
test_deploy.py boots both with nothing but a .env and checks every setting
landed.

THE RULES (the same ones the old helper had)
--------------------------------------------
- A REAL ENVIRONMENT VARIABLE ALWAYS WINS. This only fills in what the shell
  or the service manager did not set, so `$env:ELUSION_OWNER = "..."` still
  overrides the file, and a stale .env can never take precedence over what
  someone just typed. On the live server systemd sets everything from
  /etc/elusion/elusion.env and there is no .env beside the code at all.
- KEY=value, one per line. Blank lines and lines starting with # are skipped,
  as is anything without an "=". Spaces around the key and the value go.
- A key named twice takes the LAST value, as a shell or a systemd
  EnvironmentFile would. (The old helper kept the first.)
- One pair of matching quotes around a value is removed, so
  ELUSION_MAIL_FROM="Elusion RPG <you@example.com>" works here and in a
  systemd EnvironmentFile alike. A quote that is not one of a matching pair is
  kept: it is part of the value.
- A byte-order mark is not part of the first key. PowerShell 5's
  `Set-Content -Encoding UTF8` writes one, and the old helper read the first
  line's key as "\ufeffELUSION_OWNER" - which no code ever asks for.
- No dependency. python-dotenv would do this with more polish, but it is one
  more thing to install for twenty lines.

.env is gitignored, which is the point - ELUSION_OWNER must not live in the
repository any more than it lives in the database, and the mail password must
not live in either.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def parse(text):
    """The (key, value) pairs in a .env's text, in order. Pure; no I/O."""
    pairs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            pairs.append((key, value))
    return pairs


def load(directory=None, environ=None, name=".env"):
    """
    Fill `environ` (os.environ by default) from <directory>/.env.

    Returns the keys it set, which leaves out every key that was already set.
    A missing file is normal - the live server has none - and sets nothing. An
    unreadable one says so and sets nothing, because a server that refuses to
    boot over a permissions slip on an optional file is its own outage.
    """
    environ = os.environ if environ is None else environ
    path = os.path.join(directory or HERE, name)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as handle:   # -sig: Notepad's BOM
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        print("[BOOT] could not read %s (%s); using the environment only" % (path, exc))
        return []

    from_file = {}
    for key, value in parse(text):
        from_file[key] = value              # named twice: the last one wins
    taken = [key for key in from_file if key not in environ]
    for key in taken:
        environ[key] = from_file[key]
    if taken:
        # Names only, never values: this line lands in the service log.
        print("[BOOT] .env set %s (the environment wins over the file)"
              % ", ".join(taken))
    return taken


# =============================================================================
# THE CHECK (0.86.1, UPGRADES.md item 12): every setting, before the server
# starts. wsgi.py calls check_env() first; it is pure - a dict in, two lists
# out - so test_deploy.py hands it a dict per case instead of booting wsgi.py.
#
# REFUSE WHAT IS UNSAFE, WARN ABOUT WHAT ONLY SWITCHES A FEATURE OFF. A
# refusal during an update is the server down, so it is kept for a setting
# that leaks something (a login code in the log, a player's address) or one
# the server would crash on anyway, said in words instead of a traceback.
#
# NO VALUE IS EVER PRINTED for a setting that can hold a secret: these lines
# land in the service log. A port, a path or a count may be named back.
# =============================================================================


# EVERY SETTING THIS SERVER READS. A new os.environ.get("ELUSION_...") goes
# here and in .env.example, or test_deploy.py fails naming it. A key in the
# environment that is not here is a warning - a typo in a setting's name is a
# setting that silently does nothing.
KNOWN = (
    "ELUSION_OWNER", "ELUSION_CO_OWNERS", "ELUSION_DB", "ELUSION_GAMEDATA",
    "ELUSION_TRUSTED_PROXIES", "ELUSION_DEBUG", "ELUSION_RATE_LIMIT",
    "ELUSION_SMTP_HOST", "ELUSION_SMTP_PORT", "ELUSION_SMTP_USER",
    "ELUSION_SMTP_PASSWORD", "ELUSION_MAIL_FROM", "ELUSION_MAIL_CONSOLE",
    "ELUSION_STAFF_LOGIN_CODES", "ELUSION_PRESENCE_HOST", "ELUSION_PRESENCE_PORT",
    "ELUSION_PRESENCE_URL", "ELUSION_BOOKS", "ELUSION_TRANSFER_GIB",
    "ELUSION_TURN_KEY_ID", "ELUSION_TURN_KEY_TOKEN", "ELUSION_VOICE_DIRECT",
    "ELUSION_DB_FRESH", "ELUSION_BACKUP_DIR",
)
_TRUE = ("1", "true", "yes", "on")
_WEB_ROOTS = ("static", "public", "www", "htdocs", "wwwroot")
_RATE = re.compile(r"^(\d+)\s*(?:per|/)\s*(\d+)?\s*(second|sec|minute|min|hour)?s?$")
_VOICE_KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")   # app.py VOICE_KEY_PATTERN


def _get(environ, key):
    return str(environ.get(key, "") or "").strip()


def _port_problem(environ, key):
    text = _get(environ, key)
    if not text:
        return None
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        return "%s must be a port number from 1 to 65535, got %r" % (key, text)
    return None


def check_env(environ):
    """(problems, warnings) for the settings in `environ`. A problem stops the
    server (wsgi.py); a warning is printed and the server starts. Pure."""
    problems = []
    warnings = []

    # THE ONE THAT MUST NEVER SHIP. The Werkzeug debugger turns any unhandled
    # exception into code execution. Under a WSGI server it would do nothing,
    # which is why it is refused: someone who set it believes it does.
    if _get(environ, "ELUSION_DEBUG").lower() in _TRUE:
        problems.append(
            "ELUSION_DEBUG is set. It does nothing under a WSGI server, and it means "
            "this config was copied from a dev machine - check what else came with it.")

    # THIS COMPUTER ONLY, both of them. A printed code is a working key to an
    # account sitting in a log file; direct voice tells every player in a call
    # where the others are. app.py honours exactly "1" for the first.
    if _get(environ, "ELUSION_MAIL_CONSOLE") == "1":
        problems.append(
            "ELUSION_MAIL_CONSOLE=1 writes login and reset codes into the server log. "
            "It is for one computer's testing; on a server, remove it and set up mail.")
    if _get(environ, "ELUSION_VOICE_DIRECT").lower() in _TRUE:
        problems.append(
            "ELUSION_VOICE_DIRECT is on, which lets voice calls connect directly and "
            "shows players each other's addresses. Remove it; voice uses the relay.")

    # A NUMBER THE SERVER WOULD CRASH ON, named instead of a bare ValueError.
    for key in ("ELUSION_SMTP_PORT", "ELUSION_PRESENCE_PORT"):
        problem = _port_problem(environ, key)
        if problem:
            problems.append(problem)
    hops = _get(environ, "ELUSION_TRUSTED_PROXIES") or "0"
    if not hops.isdigit():
        problems.append("ELUSION_TRUSTED_PROXIES must be a whole number, got %r" % hops)
    elif int(hops) == 0:
        # Left at 0 behind a proxy, every player shares one throttle bucket;
        # above 0 with none, X-Forwarded-For is the attacker's. Nothing here
        # can tell which is true. See client_ip() in app.py.
        warnings.append(
            "ELUSION_TRUSTED_PROXIES is 0 - the per-IP throttle will read the socket "
            "address. Correct ONLY if nothing proxies this app. Behind nginx or a "
            "load balancer, set it to the number of proxies you run.")

    # MAIL, HALF SET. A host with no From address cannot send, and with mail
    # unable to send a staff login takes no code - the second step quietly
    # gone. That is a refusal. The other halves only switch mail off.
    host, sender = _get(environ, "ELUSION_SMTP_HOST"), _get(environ, "ELUSION_MAIL_FROM")
    if host and not sender:
        problems.append(
            "ELUSION_SMTP_HOST is set and ELUSION_MAIL_FROM is not, so no mail can "
            "send and staff logins would take no code. Set ELUSION_MAIL_FROM.")
    elif sender and not host:
        warnings.append("ELUSION_MAIL_FROM is set and ELUSION_SMTP_HOST is not: no mail "
                        "sends, and staff logins take no code.")
    user, password = _get(environ, "ELUSION_SMTP_USER"), str(environ.get("ELUSION_SMTP_PASSWORD", "") or "")
    if bool(user) != bool(password):
        warnings.append("only one of ELUSION_SMTP_USER and ELUSION_SMTP_PASSWORD is set: "
                        "a provider that needs a login will refuse every mail.")
    if _get(environ, "ELUSION_STAFF_LOGIN_CODES").lower() in ("off", "0", "false", "no"):
        warnings.append("ELUSION_STAFF_LOGIN_CODES is off: a staff password alone logs in. "
                        "Turn it back on once mail works (DEPLOY.md).")

    # VOICE, HALF SET: voice is simply off.
    key_id, token = _get(environ, "ELUSION_TURN_KEY_ID"), _get(environ, "ELUSION_TURN_KEY_TOKEN")
    if bool(key_id) != bool(token):
        warnings.append("only one of ELUSION_TURN_KEY_ID and ELUSION_TURN_KEY_TOKEN is set: "
                        "party voice is off.")
    elif key_id and not _VOICE_KEY.match(key_id):
        warnings.append("ELUSION_TURN_KEY_ID does not look like a key id (8-128 letters, "
                        "digits, - and _): party voice is off.")

    # SETTINGS THAT FALL BACK QUIETLY when they cannot be read.
    rate = _get(environ, "ELUSION_RATE_LIMIT").lower()
    if rate and rate not in ("0", "off", "none", "false") and not (_RATE.match(rate) or rate.isdigit()):
        warnings.append("ELUSION_RATE_LIMIT %r cannot be read, so there is no rate limit. "
                        "Write it like \"600 per minute\"." % rate)
    gib = _get(environ, "ELUSION_TRANSFER_GIB").lower()
    if gib and gib not in ("off", "0", "no", "false"):
        try:
            float(gib)
        except ValueError:
            warnings.append("ELUSION_TRANSFER_GIB %r is not a number; presence.py uses 1000." % gib)

    # WITHOUT AN OWNER THERE IS NO OWNER. app.py fails closed already; finding
    # out from a staff route at 3am is worse than finding out at boot.
    if not _get(environ, "ELUSION_OWNER"):
        warnings.append("ELUSION_OWNER is not set - no account will hold the top rank. "
                        "Set it in the environment or in a .env beside app.py.")

    # The database must not live anywhere a web server might serve it.
    db = _get(environ, "ELUSION_DB")
    if any(part in db.replace("\\", "/").lower().split("/") for part in _WEB_ROOTS):
        problems.append(
            "ELUSION_DB (%s) is inside a directory that looks web-served. It holds "
            "real password hashes and must never be fetchable over HTTP." % db)

    # A NAME NOBODY READS: almost always a typo, and a typo is a setting
    # that silently does nothing.
    for key in sorted(k for k in environ if str(k).startswith("ELUSION_") and k not in KNOWN):
        warnings.append("%s is not a setting this server reads - a typo? (envfile.KNOWN "
                        "lists every one)" % key)
    return problems, warnings
