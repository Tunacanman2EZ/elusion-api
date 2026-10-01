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

# Linux - --preload is not optional, see below
gunicorn --preload -w 4 -b 127.0.0.1:5000 wsgi:application

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
| `ELUSION_SMTP_HOST`, `ELUSION_SMTP_PORT`, `ELUSION_SMTP_USER`, `ELUSION_SMTP_PASSWORD`, `ELUSION_MAIL_FROM` | your mail provider's | Recovery codes **and staff login codes**. Without them a staff login has no second step |
| `ELUSION_STAFF_LOGIN_CODES` | **unset** (on) | `off` only to get back in when mail has broken - see below |

`wsgi.py` checks all of these at boot and says which one is wrong.

**`--preload`, or the first boot after an update can take the server down.**
Without it every gunicorn worker imports app.py at the same moment, and each one
runs the migrations in `init_db()` against the same database file. Two workers
both see a column missing, both add it, and the second fails with `duplicate
column name` - its worker dies, and gunicorn shuts the whole server down with
"Worker failed to boot". It happened on day 1 against a fresh database, the
very situation a first deploy is in. `--preload` imports the app once, in the
parent, before the workers start, so the migrations run once. waitress is one
process and never had this.

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
- [ ] **Every suite green against the deployed code**, not against a working
      copy. Run `run_tests.ps1`, which DISCOVERS them — `Get-ChildItem test_*.py`
      — so a suite added after this line was written is still run.

      This checkbox used to name four: `test_api.py`, `test_security.py`,
      `test_throttle.py`, `test_gathering.py`. That was true when it was written
      and there are twenty-seven now, so following it would have meant going
      public having run four of them — and not the four that matter most here.
      `test_revocation.py`, `test_refusals.py`, `test_ownership.py` and
      `test_maintenance.py` all cover behaviour that only has consequences once
      somebody else can connect, and none of them was on the list.

      A written-down list of suites is the same failure as a written-down count,
      which CLAUDE.md already has a section about. Name the runner, not its
      contents.
- [ ] **Nothing to do about the client build gate, and that is the point.**
      Every request now carries `X-Elusion-Build`, and the server can refuse a
      build older than a minimum it holds in `server_settings`. **It ships
      disarmed** — `min_client_build` is 0, so nothing is refused — because the
      mechanism is what could not be added later and the enforcement can be
      switched on at any moment:

      ```
      POST /api/server/minbuild  {"build": 3}     # refuse builds below 3
      POST /api/server/minbuild  {"build": 0}     # let everybody in again
      ```

      Owner only, 404 to anyone else. The current minimum and the newest known
      build are on `/api/status`, which is the one route the gate never
      refuses — so a client turned away can still find out why.

      **It stops an honest old build and nothing else.** The build is a header
      and headers are client-controlled, so anyone who can type a curl command
      can claim any build. It is protocol hygiene, not a security control, and
      that limitation is also the way back in if you ever lock yourself out:
      send the header by hand and set the minimum to 0. `set_min_build()`
      refuses a minimum above the newest build that exists, so getting into
      that state takes deliberate effort rather than a typo.

- [ ] **Staff logins take a code, and you have seen one arrive.** A mod, dev
      or the owner with a confirmed recovery address gets a six-digit code by
      email after the password, and only the code gets a token (STAFF LOGIN
      CODES in app.py). The boot log says `staff login codes: on` when the
      server can send mail. Log in as the owner once on the new server before
      anyone else connects: the code arriving is the test that mail works from
      that box.

      **If mail breaks after launch, you are still the owner of the machine.**
      Put `ELUSION_STAFF_LOGIN_CODES=off` in `.env`, restart, log in on the
      password, fix the mail settings, take the line out and restart again.
      Staff sessions that are already open keep working the whole time; the
      step only gates new logins.

- [ ] **The Godot client points at the deployed URL.** `Api.BASE_URL` now
      resolves at startup from, in order: `--server=https://host` on the command
      line, `ELUSION_SERVER` in the environment, a one-line `user://server.cfg`,
      then the local default. The file override is the one that matters for a
      shipped build — it repoints an already-installed client without a rebuild.
      The boot log prints the address whenever it is not the default, because a
      client aimed at the wrong server looks exactly like a server that is down.
      The browser build needs none of this: it always uses the address it was
      loaded from (see **The browser build** below).

---

## The browser build

The game can also be played in a browser. The export (Godot, Project > Export >
Web, preset in the game repo) is a folder of static files: `index.html` beside
`index.js`, `index.wasm` and `index.pck`. It needs two things from the site, and
both fit on the same box as the API, behind the same proxy.

**One address.** A browser build sends its API calls to the address the page
came from. This API sends no cross-site headers, so the browser refuses any other
address. This was measured, not assumed: a page on one port calling the API on
another had every request blocked. So the site that serves the game also passes
`/api/` to the API.

**Refresh means update.** Nothing in the export is versioned by name, so
nothing may be cached blind. `Cache-Control: no-cache` makes the browser ask
about every file on every visit. Unchanged files get a 304, and a new upload is
picked up on the next visit or refresh. A raised `min_client_build` then tells an
open tab to refresh, in the game's own words.

A DNS record `A play -> YOUR_SERVER_IP`, then one site block. Both of these
were tested unchanged against a real export (apart from the port and the name).

**Caddy**, beside the API's own site in `/etc/caddy/Caddyfile`. It fetches the
certificate itself:

```caddy
play.elusionrpg.com {
	encode zstd gzip

	# THE API ON THE PAGE'S OWN ADDRESS. The browser refuses the game's calls
	# to any other one - the API sends no cross-site headers.
	handle /api/* {
		reverse_proxy 127.0.0.1:5000
	}

	# The export: index.html and the files beside it, nowhere near elusion.db.
	# Compressed as it goes by encode, above. Not "precompressed": Caddy 2.10.2
	# answered a plain request for a precompressed file with 206 Partial Content.
	# REFRESH MEANS UPDATE: the browser asks about every file on every visit
	# and gets a 304 when nothing changed, so an upload is picked up on the
	# next one. Nothing here is versioned by name, so nothing may be cached blind.
	handle {
		root * /srv/elusion-web
		header Cache-Control "no-cache"
		file_server
	}
}
```

**nginx**, if the box runs that instead. Put this in
`/etc/nginx/sites-available/elusion-play`:

```nginx
server {
    listen 80;
    server_name play.elusionrpg.com;

    # The export: index.html and the files beside it. Outside the API's folder,
    # and nowhere near elusion.db.
    root /srv/elusion-web;
    index index.html;

    # REFRESH MEANS UPDATE. The browser asks about every file on every load and
    # gets a 304 when nothing changed, so a new upload is picked up by a
    # refresh. Nothing here is versioned by name, so nothing may be cached blind.
    location / {
        add_header Cache-Control "no-cache" always;
        try_files $uri $uri/ =404;
    }

    # THE API ON THE PAGE'S OWN ADDRESS. The browser refuses the game's calls
    # to any other one - the API sends no cross-site headers.
    location /api/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
        client_max_body_size 12M;
    }

    # 51 MB as exported, 21 MB compressed. gzip_static sends the .gz beside a
    # file when there is one (gzip -k9 index.wasm index.pck index.js after an
    # upload); anything else is compressed as it goes.
    gzip on;
    gzip_static on;
    gzip_types application/wasm application/octet-stream text/javascript application/javascript;
    gzip_min_length 1024;
}
```

```
sudo ln -s /etc/nginx/sites-available/elusion-play /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d play.elusionrpg.com --redirect
```

**Uploading a build**, from the game folder, to a folder the service user owns
(`sudo mkdir -p /srv/elusion-web && sudo chown elusion:elusion /srv/elusion-web`
once):

```
rsync -rv --checksum --delete builds/web/ elusion@YOUR_SERVER_IP:/srv/elusion-web/
```

- **`--checksum`** leaves a file alone when its bytes did not change, so its
  date and ETag stay the same. A returning player then downloads only what is
  new, usually the 14 MB `.pck` and not the 38 MB `.wasm`.
- **Behind nginx,** run `gzip -k9 builds/web/index.wasm builds/web/index.pck
  builds/web/index.js` first. `gzip_static` then sends the `.gz` files and does
  no work per player.
- **Caddy** compresses the `.wasm` and `.js` as it sends them. It leaves the
  `.pck` alone (octet-stream is not on its list), which costs about 2 MB a
  first visit.

**What this changes elsewhere: nothing.**

- It is the same proxy, one hop, so `ELUSION_TRUSTED_PROXIES` stays `1`.
- The `/api/` block is the same proxy as the API's own site. The browser build
  has no `server.cfg` and needs none.
- The desktop client keeps `https://api.elusionrpg.com`.

Tested in the sandbox against a real export:

- The setup was Caddy 2.10.2, nginx 1.24 (the version Ubuntu 24.04 ships) and
  headless Chromium.
- Both blocks above were used unchanged, apart from the port and the name.
- Compression took the export from 51.8 MB to about 21 MB, and the loader showed
  progress the whole way.
- A login, the character list, the town and a save all went through `/api/`.
- A second visit transferred nothing but 304s, and an upload was running on the
  next visit.

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

**E-2 — closed: all six skills are server-owned.** Fishing, cooking and attack
ride their own server events; defense, agility and magic report activity to
`/api/skill/train`, which clamps each to a per-second ceiling times the time
elapsed and applies class proficiency itself. A client's claimed level earns
nothing. (This paragraph used to say three skills were still client-claimed;
`SECURITY_NOTES.md` has the full record.)

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
