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
