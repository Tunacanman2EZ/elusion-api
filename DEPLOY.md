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
| `ELUSION_RATE_LIMIT` | e.g. `"600 per minute"` (optional) | Per-IP request ceiling; **unset = off** |

`wsgi.py` checks all of these at boot and says which one is wrong.

### Rate limiting (`ELUSION_RATE_LIMIT`)

Off unless you name a limit, because the right number depends on how chatty your
client is — measure before you tighten. Accepts `"600 per minute"`, `"600/60"`,
`"10 per second"`, or a bare number (per minute). Login and registration already
have their own tighter throttles; this is the blanket ceiling over everything
else.

It counts **in memory, per worker**, so under `gunicorn -w 4` the real ceiling
is four times what you set — fine as a DoS ceiling, not a precise fair-use
limit. Start generous (a value no real player hits in a burst), watch the logs,
and tighten with data. For a precise limit shared across all four workers, move
the counter to Redis or the `login_attempts` SQLite pattern.

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
- [ ] **Backups of `elusion.db`**, somewhere off the box — losing it loses every
      account. Run `backup_db.py` on a schedule (see **Backups** below); it takes
      a consistent snapshot while the server is live and verifies it before
      trusting it.
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

## Backups

`backup_db.py` makes a consistent, self-verifying snapshot — safe to run while
players are online (it uses SQLite's online-backup API, not a file copy) — and
keeps the newest N:

```
python3 backup_db.py --db /path/to/elusion.db --out /var/backups/elusion --keep 30
```

It reopens the copy it just wrote, runs `integrity_check`, and reads a real row
before trusting it, and it **exits non-zero** if anything fails — so a scheduler
knows the night it matters. Send the backups somewhere **off the box**: a backup
on the same disk as the database dies with it.

**Linux (cron)** — nightly at 03:15:

```
15 3 * * * cd /srv/elusion && /usr/bin/python3 backup_db.py --db elusion.db --out /var/backups/elusion --keep 30 >> /var/log/elusion-backup.log 2>&1
```

**Windows (Task Scheduler)** — a daily task that runs:

```
python C:\path\to\backup_db.py --db C:\path\to\elusion.db --out D:\backups\elusion --keep 30
```

**Test the restore, once.** A backup you have never restored is a rumour: copy a
backup file to a scratch path, point a throwaway server at it, and log in.

---

## Watching the live economy

`canary.py` reads the live database — read-only, so it can never be what broke a
number — and checks the invariants the ledger exists to hold: gold and lusions
conserved, no negative balances, no skill past the cap. Run it on a schedule; it
**exits non-zero** the moment one drifts, which is a bug or a tamper and either
way something to see the same day.

```
python3 canary.py --db /path/to/elusion.db --quiet
```

`--quiet` prints nothing while all is well, so a cron line only speaks up when it
should. Turn the non-zero exit into however you already get paged — mail, a
channel webhook, whatever:

```
15 * * * * cd /srv/elusion && python3 canary.py --db elusion.db --quiet || mail -s "ELUSION CANARY FAILED" you@example.com < /dev/null
```

(Hourly above; the check is cheap. Point it at the same database the server writes.)

---

## Watching what players claim to kill

`canary.py` watches the economy's arithmetic; `killwatch.py` watches player
behaviour. It reads `kill_reports` — read-only, same discipline — and sorts
accounts into two piles: **IMPOSSIBLE** (a claim the server's own rules should
have refused — the spawn ceiling breached, a reward-less enemy paid — which means
a defence was not running) and **SUSPICIOUS** (legal but far outside honest play
— a sustained rate, a boss farmed fast, the ceiling blind spot, an under-levelled
boss). The first is an alarm; the second is a review list. It bans nothing.

```
python3 killwatch.py --db /path/to/elusion.db          # full review dossier
python3 killwatch.py --db /path/to/elusion.db --quiet   # cron: speak only on an alarm
```

The exit code is **non-zero only on an IMPOSSIBLE finding** — a wall that came
down, page it — and `--quiet` prints nothing unless one exists. A server full of
merely-SUSPICIOUS accounts exits 0: those are for you to read, not for a pager to
scream about. So it splits into two schedules, an alarm and a digest:

```
# hourly alarm: silent unless a defence stopped running
7 * * * * cd /srv/elusion && python3 killwatch.py --db elusion.db --quiet || mail -s "ELUSION KILLWATCH ALARM" you@example.com < /dev/null

# weekly digest: the review list, whatever it holds
0 9 * * 1 cd /srv/elusion && python3 killwatch.py --db elusion.db | mail -s "Elusion weekly kill review" you@example.com
```

`--window` and `--respawn` must match the server's spawn-ceiling constants; the
defaults already do. This is the standing watch over **E-3** (below) until the
day combat is server-observed.

---

## What is still open when it goes live

These are real and named rather than hidden. Full detail in `SECURITY_NOTES.md`.

**E-3 — the kill event is asserted, not verified.** The server rolls the
rewards and rate-limits the reports, but never confirms a fight happened. A
modified client can report kills it did not make, at the sustained token rate.
This caps the *speed* of the fraud, not its existence, and it is the one that
matters most with strangers connected. Not closed, but now **watched**:
`killwatch.py` (above) turns an invisible claim into a flagged, bannable account,
and alarms outright if the rate limit or spawn ceiling ever stops running.

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

SQLite in **WAL mode with `synchronous=NORMAL`** handles this comfortably at
small-to-medium scale — it is one file and the write volume is a handful of rows
per kill. Both PRAGMAs are set in `get_db()` on every connection, alongside
`busy_timeout=5000` (a contended writer waits for the lock instead of erroring).

**Measured** (`loadtest.py`, `gunicorn -w 4`, a 2-core box — real hardware does
more):

| path | throughput | note |
|---|---|---|
| `GET /api/player/status` (read) | ~1,000 req/s | scales with cores, sub-2ms uncontended |
| `POST /api/combat/kill` (write) | ~560 committed/s | heaviest transaction; ~2.2× the pre-WAL number |

The write path is the ceiling, because SQLite serialises writers on one lock —
WAL makes each commit cheaper and stops writers blocking readers, but it does
**not** give you multiple concurrent writers. So write throughput is bounded by
how fast one writer can commit, not by worker count. If a kill ever needs to be
faster, shorten its transaction (it writes `saves`, `gold_ledger`,
`kill_reports`, `skills` and a loot roll in one) before reaching for Postgres.
Postgres is the endgame for true multi-writer concurrency, and nothing before it
is needed to launch.

**WAL writes two sidecar files** next to the database, `elusion.db-wal` and
`elusion.db-shm`. Keep them on the same filesystem as the database (they are, by
construction) and never back up the `.db` alone by copying it — `backup_db.py`
uses SQLite's online-backup API, which captures the WAL correctly; a bare `cp`
would not. `canary.py`, `backup_db.py` and `security_bot.py` all read the live
WAL database `mode=ro` without trouble.

The `login_attempts` table prunes itself on the login path
(`LOGIN_LOG_RETENTION_SECONDS`, 14 days), so it will not grow without bound.
Nothing else in the schema self-prunes: `staff_actions` is append-only by
design, because an audit trail that deletes itself is not one.

## Benchmarking

`loadtest.py` is the repeatable proof that a change made the server faster, not
quietly slower — run it before and after. Point it at a server you started
(any platform), or let it launch a throwaway one on Linux:

```
# benchmark your running server (start it however you deploy — waitress/gunicorn):
python3 loadtest.py --url http://127.0.0.1:5000

# or, on Linux, let it spin up a scratch gunicorn and clean up after:
python3 loadtest.py --launch --workers 4
```

It seeds throwaway accounts and sweeps concurrency against the read and write
paths, reporting req/s and p50/p95/p99. Read the write ceiling at the lowest
concurrency where 2xx is still ~100% — above that the spawn ceiling starts
(correctly) returning 429 as a handful of test accounts out-kill the world.
**It mutates what it points at, so never aim `--url` at production.**

## Watching latency (live)

`loadtest.py` proves speed *before* you ship; **`GET /api/metrics`** watches it
*after*. The server times every request in memory and serves per-endpoint
percentiles to the owner:

```
curl -s -H "Authorization: Bearer <owner-token>" https://host/api/metrics
```

Each endpoint reports `count`, `error_rate`, `p50_ms/p95_ms/p99_ms/max_ms` and a
recent-sample count, plus process `uptime_seconds` and `total_requests`. It is
how a latency regression becomes a number the day it happens instead of a player
complaint next week — the live counterpart to the benchmark. The heavy endpoint
is `register` (scrypt hashing, ~100ms by design); everything else should sit in
single-digit milliseconds.

**Per worker, like the rate limiter.** Under `gunicorn -w 4` each worker keeps
its own window, so `/api/metrics` reports whichever worker answered — enough to
spot a slow endpoint or a creep, not a fleet-wide total. A shared view would need
the counters in Redis or a table; deliberately left as a later step. The memory
is bounded (`METRICS_SAMPLES` recent timings per endpoint, oldest dropped).

Any request slower than `SLOW_REQUEST_MS` (1s) is also logged as
`[SLOW] <method> <endpoint> took <n> ms`, so a stall shows up in the log even if
nobody is watching the metrics endpoint at that moment.
