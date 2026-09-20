# Deploying the Elusion API

Everything here is about one transition: **from "only I can reach it" to
"strangers can reach it."** That change is what all the server-authority work
was for, and it is also what turns several currently-harmless settings into
real problems.

Nothing in this file is done yet. It is the checklist, not a record.

---

## The short version

```
pip install waitress            # Windows
pip install gunicorn            # Linux

# Linux
gunicorn -w 4 -b 127.0.0.1:5000 wsgi:application

# Windows
waitress-serve --listen=127.0.0.1:5000 wsgi:application
```

…behind a reverse proxy that terminates TLS, with these set:

| Variable | Value | Why |
|---|---|---|
| `ELUSION_OWNER` | your username | The top rank comes from here, never from the database |
| `ELUSION_TRUSTED_PROXIES` | number of proxies you run | See **The proxy setting** below — wrong in either direction is bad |
| `ELUSION_DB` | a path outside any web root | It holds real password hashes |
| `ELUSION_DEBUG` | **unset** | `wsgi.py` refuses to start if it is set |

`wsgi.py` checks all of these at boot and says which one is wrong.

---

## Why not just `python app.py`

That runs Flask's **development** server. It is single-threaded by default, has
no request limits, and is not written to face a hostile network. It is correct
for a local dev loop and wrong the moment anyone else can connect.

`wsgi.py` exists so a real server can import the app without running that block.
It also runs a preflight **before** importing `app` — deliberately, because
`app.py` opens the database at import time, so a bad `ELUSION_DB` otherwise
surfaces as `sqlite3.OperationalError: unable to open database file`: a true
message about the wrong layer.

---

## TLS is not optional here

Every authenticated request carries `Authorization: Bearer <token>`, and
`TOKEN_TTL` is thirty days. Over plain HTTP, anyone between the player and the
server reads that token off the wire and has the account for a month.

Registration and login carry the password itself.

So: TLS terminates at the reverse proxy, the app binds to **loopback only**, and
nothing reaches the app except through the proxy. Binding the app to `0.0.0.0`
means people can skip the proxy, and skipping the proxy means skipping HTTPS.

---

## The proxy setting

`ELUSION_TRUSTED_PROXIES` decides how `client_ip()` identifies a request, which
is what the per-IP login throttle counts against. **It is wrong in two opposite
directions and both are bad.**

**Left at 0 behind a proxy** — `request.remote_addr` is the proxy's address, the
same value for every player alive. The whole player base shares one throttle
bucket, and the first brute-force run locks everyone out. The defence becomes
the outage.

**Set above 0 with no proxy** — `X-Forwarded-For` is then attacker-controlled. A
spray sets a fresh fake address on every request, every bucket holds one
failure, nothing ever trips, and the log fills with invented addresses
implicating people who did nothing.

Nothing inside the app can tell which is true, which is why it is a required
decision rather than a guess. Count the hops you actually control — one nginx in
front is `1`; nginx behind Cloudflare is `2`.

**Verify it after deploying.** Fail a login from two different machines and read
the table:

```sql
SELECT ip, COUNT(*) FROM login_attempts WHERE ok = 0 GROUP BY ip;
```

Two distinct addresses means it is right. One address covering both, or an
address that changes every request, means it is not.

---

## Before the first stranger connects

- [ ] **A real WSGI server**, bound to loopback, behind a TLS-terminating proxy.
- [ ] **`ELUSION_TRUSTED_PROXIES` matches reality**, verified with the query above.
- [ ] **`elusion.db` is outside every web root** and not world-readable. It holds
      scrypt hashes of real passwords. `wsgi.py` catches the obvious paths, not
      every arrangement.
- [ ] **`.env` is not in the repo** and not readable by other users on the box.
- [ ] **Backups of `elusion.db`**, somewhere `*.db*` in `.gitignore` cannot help
      you — losing it loses every account. Copy it while the server is stopped,
      or use `sqlite3 elusion.db ".backup"`, which is safe on a live database.
- [ ] **All four suites green against the deployed code**, not against a working
      copy: `test_api.py`, `test_security.py`, `test_throttle.py`,
      `test_gathering.py`.
- [ ] **The Godot client points at the deployed URL.** `Api.BASE_URL` now
      resolves at startup from, in order: `--server=https://host` on the command
      line, `ELUSION_SERVER` in the environment, a one-line `user://server.cfg`,
      then the local default. The file override is the one that matters for a
      shipped build — it repoints an already-installed client without a rebuild.
      The boot log prints the address whenever it is not the default, because a
      client aimed at the wrong server looks exactly like a server that is down.

---

## What is still open when it goes live

These are real and named rather than hidden. Full detail in `SECURITY_NOTES.md`.

**E-3 — the kill event is asserted, not verified.** The server rolls the
rewards and rate-limits the reports, but never confirms a fight happened. A
modified client can report kills it did not make, at the sustained token rate.
This caps the *speed* of the fraud, not its existence, and it is the one that
matters most with strangers connected.

**E-2 — three skills are still client-claimed.** `defense`, `agility` and
`magic` have no server-observed event to grant against, so a client can claim
any level up to `MAX_SKILL_LEVEL`. `attack`, `fishing` and `cooking` are
server-owned.

None of these let someone take another player's account, which is the line that
matters most for going public. They let a determined player cheat their own
character, in a single-player game, which is a different and smaller problem —
but it is worth knowing which is which before anyone asks.

---

## Sizing

SQLite with WAL handles this comfortably at small scale — it is one file and the
write volume is a handful of rows per kill. The thing that will hurt first is
not the database but `-w 4` gunicorn workers each holding their own connection;
watch for `database is locked` under concurrency before assuming you need
Postgres.

The `login_attempts` table prunes itself on the login path
(`LOGIN_LOG_RETENTION_SECONDS`, 14 days), so it will not grow without bound.
Nothing else in the schema self-prunes: `staff_actions` is append-only by
design, because an audit trail that deletes itself is not one.
