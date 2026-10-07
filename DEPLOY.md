# Deploying the Elusion API

Everything here is about one transition: **from "only I can reach it" to
"strangers can reach it."** That change is what all the server-authority work
was for, and it is also what turns several currently-harmless settings into
real problems.

**The live server** below is the record of what runs at elusionrpg.com, set up
on 4 October 2026. Everything after it is the checklist and the reasons behind
it, and it still applies: a change to the server is checked against it.

---

## The short version

```
pip install waitress            # Windows
pip install gunicorn            # Linux

# Linux - --preload is not optional, see below
gunicorn --preload -w 4 -b 127.0.0.1:5000 wsgi:application

# Windows
waitress-serve --listen=127.0.0.1:5000 wsgi:application

# Both - players seeing each other, a second process beside the API
python presence.py              # 127.0.0.1:5001, path /ws/presence
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

**Where they go.** On a server, in the environment the service manager gives
the process - the live box uses systemd's `EnvironmentFile` (below). On a PC, a
`.env` beside `app.py` works: `envfile.py` reads it before anything else, and a
real environment variable always wins over the file. Until October 2026 the
`.env` was read too late for `ELUSION_DB`, `ELUSION_TRUSTED_PROXIES` and
`ELUSION_GAMEDATA`, which were silently ignored there, and `wsgi.py`'s
preflight saw none of it; `test_deploy.py` now boots both files with nothing
but a `.env`.

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

## The live server

One small droplet holds the API, the proxy and the browser build.

| Piece | Where |
|---|---|
| Machine | DigitalOcean droplet `elusion-1`, region SFO2, Ubuntu 24.04: 1 vCPU, 1 GB RAM, 25 GB disk, plus a 1 GB swap file |
| Names | `api.elusionrpg.com` and `play.elusionrpg.com`: A records at Namecheap pointing at the droplet. The bare domain and `www` stay on Netlify, which serves the website |
| Firewall | ufw allows OpenSSH, 80 and 443, nothing else. SSH takes keys only (`PasswordAuthentication no`) |
| Proxy and TLS | Caddy, from Caddy's own apt repository. It fetches and renews the certificates itself |
| Service | systemd unit `elusion-api`, run as the system user `elusion` |
| Presence | systemd unit `elusion-presence`: `presence.py` on 127.0.0.1:5001, same user, folder and settings |
| Code | `/opt/elusion/api`, a git clone of this repository, owned by `elusion` |
| Python | `/opt/elusion/venv`: `requirements.txt` plus gunicorn |
| Settings | `/etc/elusion/elusion.env`, owned by root, `chmod 600` |
| Database | `/var/lib/elusion/elusion.db`; the folder is `elusion`'s, mode 750 |
| Backups | `/var/backups/elusion` (mode 700), nightly; log in `/var/log/elusion-backup.log` |
| Browser build | `/srv/elusion-web`, a placeholder page until the first upload |

**The settings file** is the only place the secrets live. systemd reads it as
root and hands the values to the service, so the `elusion` user cannot read the
file itself, and nothing in it is in the repository. Names only here:

```
ELUSION_OWNER=<the owner's username>
ELUSION_DB=/var/lib/elusion/elusion.db
ELUSION_TRUSTED_PROXIES=1
ELUSION_SMTP_HOST=<mail server>
ELUSION_SMTP_PORT=587
ELUSION_SMTP_USER=<the sending address>
ELUSION_SMTP_PASSWORD=<typed on the server, never pasted anywhere else>
ELUSION_MAIL_FROM="Elusion RPG <the sending address>"
```

The proxy count is 1 because Caddy is the one hop. Edit the file with
`sudo nano /etc/elusion/elusion.env`, then `sudo systemctl restart elusion-api`;
nothing reads it until the restart.

**The unit**, `/etc/systemd/system/elusion-api.service`, the lines that matter:

```ini
[Service]
User=elusion
WorkingDirectory=/opt/elusion/api
EnvironmentFile=/etc/elusion/elusion.env
ExecStart=/opt/elusion/venv/bin/gunicorn --preload -w 2 --threads 4 -b 127.0.0.1:5000 wsgi:application
Restart=on-failure
```

Two workers on one core, four threads each: a request mostly waits on SQLite,
and SQLite has one writer whatever the worker count (**Sizing**, below). The
boot log line to look for is `[DEPLOY] preflight passed (trusted proxy hops: 1)`
(`journalctl -u elusion-api -n 30 --no-pager`).

**Caddy**, `/etc/caddy/Caddyfile`:

```caddy
api.elusionrpg.com {
    encode zstd gzip
    handle /ws/* {
        reverse_proxy 127.0.0.1:5001
    }
    reverse_proxy 127.0.0.1:5000
}

play.elusionrpg.com {
    encode zstd gzip
    handle /ws/* {
        reverse_proxy 127.0.0.1:5001
    }
    handle /api/* {
        reverse_proxy 127.0.0.1:5000
    }
    handle {
        root * /srv/elusion-web
        header Cache-Control "no-cache"
        file_server
    }
}
```

`sudo caddy validate --config /etc/caddy/Caddyfile` before
`sudo systemctl reload caddy`. **The browser build**, below, says why the play
site carries the API too.

**The nightly backup**, `/etc/cron.d/elusion-backup` (one line; the droplet's
clock is UTC, so 10:15 is 4:15 in the morning in New Mexico, 3:15 in winter):

```
15 10 * * * elusion /usr/bin/python3 /opt/elusion/api/backup_db.py --db /var/lib/elusion/elusion.db --out /var/backups/elusion --keep 14 >> /var/log/elusion-backup.log 2>&1
```

It runs the copy of `backup_db.py` in the clone, so a `git pull` updates it.
These backups are on the same disk as the database; **Backups**, below, covers
getting copies off the machine.

### Seeing each other: the presence server

`presence.py` shows players to each other (api/CLAUDE.md, "Seeing each
other"). It is its own service because it holds a socket open per player,
which a gunicorn thread should not. Set up once, 6 October 2026:

```
sudo -u elusion /opt/elusion/venv/bin/pip install -r /opt/elusion/api/requirements.txt
sudo nano /etc/systemd/system/elusion-presence.service
sudo systemctl daemon-reload
sudo systemctl enable --now elusion-presence
sudo journalctl -u elusion-presence -n 20 --no-pager
```

The unit:

```ini
[Unit]
Description=Elusion presence (players seeing each other)
After=network.target

[Service]
User=elusion
WorkingDirectory=/opt/elusion/api
EnvironmentFile=/etc/elusion/elusion.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/elusion/venv/bin/python presence.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

The log line to look for is `[PRESENCE] listening on ws://127.0.0.1:5001/ws/presence
(database /var/lib/elusion/elusion.db)` - the same database the API uses, read
from the same settings file. Then the `handle /ws/*` lines in both Caddy blocks
above, `caddy validate` and `reload`. Caddy passes the WebSocket upgrade
through `reverse_proxy` with no further settings; `encode` leaves it alone
(checked against Caddy 2.10.2 with the blocks exactly as above).

**Nothing else changes.** The game asks the API for a ticket and the API's
answer says where the socket is: `wss://` the same host the game reached the
API on, so the browser build and the desktop game both find it with no new
setting. No presence server running is not an error anywhere - the game plays
as before and nobody else is drawn.

**The books on the monsters (0.10.0).** The same process also keeps the
server's own count of every monster's health and writes what it finds into
`combat_kills` and `combat_flags` - two tables `app.py` creates, so restart
`elusion-api` before `elusion-presence` the first time (the presence log says
so once if it starts first, and writes nothing until it can). Its second boot
line says whether they are on: `[PRESENCE] books on the monsters: on, 6 areas
mapped`. They need the `areas` and `combat` blocks of a gamedata.json exported
by 0.10.0's exporter; with an older one the line says `off` and nothing else
changes. To switch them off without a deploy, add `ELUSION_BOOKS=off` to the
settings file and restart `elusion-presence`. **Nothing a player sees depends
on them** - a kill is paid as before - so off is always safe. Read what they
found with `killwatch.py` (below), tier WATCHED.

### Updating the server

After pushing to GitHub, from an SSH session on the droplet:

```
cd /opt/elusion/api
sudo -u elusion git pull
sudo systemctl restart elusion-api
sudo systemctl restart elusion-presence
sudo journalctl -u elusion-api -n 30 --no-pager
curl -s https://api.elusionrpg.com/api/status
```

- **Restarting the presence server** drops every connection; the games
  reconnect by themselves within a few seconds and draw everybody again.

- **`sudo -u elusion`** because the clone is that user's. git refuses to work in
  a folder another user owns ("dubious ownership"), and a pull as root would
  leave root-owned files the service then cannot replace.
- **When `requirements.txt` changed**, before the restart:
  `sudo -u elusion /opt/elusion/venv/bin/pip install -r requirements.txt`.
- **When the game's catalogue changed**, the new `gamedata.json` has to be
  committed here first (the exporter writes into the game repo, not this one);
  the pull then brings it. `/api/status` shows the item and enemy counts.
- `--preload` runs the migrations once, in the parent, so a restart onto a
  new schema is safe while the database is live. The restart itself takes a
  few seconds, and a request landing in them fails, so pick a quiet moment.

### Running a tool against the live database

`set_role.py`, `canary.py`, `killwatch.py` and `deathwatch.py` read
`ELUSION_DB` or take `--db`. Give it to them, and run them **as `elusion`**:

```
sudo -u elusion env ELUSION_DB=/var/lib/elusion/elusion.db /opt/elusion/venv/bin/python /opt/elusion/api/set_role.py --list
sudo -u elusion /opt/elusion/venv/bin/python /opt/elusion/api/canary.py --db /var/lib/elusion/elusion.db
```

- **Without the path**, a tool looks for `elusion.db` beside itself, in
  `/opt/elusion/api`, finds nothing and says so. None of them creates a
  database, so the mistake is loud rather than a second, empty database.
- **Not as root.** The database runs in WAL mode, and a writer can create
  `elusion.db-wal` and `elusion.db-shm` beside it. Created by root, they are
  files the service cannot write, and its requests fail until they are gone.
  Run as `elusion`, anything created is the service's own.

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
      On the live server there is no `.env`: the settings are in
      `/etc/elusion/elusion.env`, root's, mode 600.
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
      Put `ELUSION_STAFF_LOGIN_CODES=off` in `.env` (on the live server,
      `/etc/elusion/elusion.env`), restart, log in on the password, fix the
      mail settings, take the line out and restart again.
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

	# The presence socket, on the same address for the same reason.
	handle /ws/* {
		reverse_proxy 127.0.0.1:5001
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

    # THE PRESENCE SOCKET. Unlike Caddy, nginx passes a WebSocket upgrade only
    # when told to: HTTP/1.1 and the two headers. The server pings every 20
    # seconds, well inside the read timeout.
    location /ws/ {
        proxy_pass http://127.0.0.1:5001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;
        proxy_read_timeout 120s;
    }

    # 46 MB as exported, 16 MB compressed. gzip_static sends the .gz beside a
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
  new, usually the 8 MB `.pck` and not the 38 MB `.wasm`.
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
  progress the whole way. (It is 46.2 MB and 15.7 MB now: the game's emoji font
  ships at chat size since 1 October. See the game's CLAUDE.md, "Speed on day 1".)
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
knows the night it matters.

**Each backup is one file.** The live database runs in WAL mode and the backup
API copies that mode along with the pages, so until October 2026 every copy was
a WAL database too - and opening one at all, even read-only to verify it, makes
SQLite create a `-wal` and a `-shm` beside it. The first night on the live
server left three files where one was meant, and pruning only knew about the
`.db`. Each copy is now switched to `journal_mode=DELETE` before it is closed,
`verify()` refuses one still in WAL mode, and every run settles an older copy
that has sidecars (keeping its date) and removes sidecars whose backup is gone.
It only ever touches names it writes, `<db name>-<YYYYMMDD>-<HHMMSS>.db`, so a
folder shared with anything else is safe. The live database is never touched
and stays in WAL. `test_deploy.py`.

**Linux (cron)** — the live server's line is under **The live server**, above.
The shape, for another box:

```
15 3 * * * elusion /usr/bin/python3 /path/to/backup_db.py --db /path/to/elusion.db --out /var/backups/elusion --keep 30 >> /var/log/elusion-backup.log 2>&1
```

(That is the `/etc/cron.d/` form, with the user to run as after the time. In a
personal `crontab -e` the user field is left out.)

**Windows (Task Scheduler)** — a daily task that runs:

```
python C:\path\to\backup_db.py --db C:\path\to\elusion.db --out D:\backups\elusion --keep 30
```

### Off the machine

**A backup on the same disk as the database dies with it.** A deleted droplet,
a botched resize, a compromised box: the nightly copies in
`/var/backups/elusion` go too. Two layers, cheapest first:

1. **DigitalOcean's droplet backups** (the droplet's **Backups & Snapshots**
   tab). An image of the whole disk - system, settings, code, database and the
   nightly copies - kept by DigitalOcean apart from the droplet, weekly or
   daily, for a share of the droplet's price. A restore brings back the whole
   machine as it was. It is a disk image taken while the server runs, so the
   database inside it is crash-consistent rather than a clean snapshot; the
   nightly `backup_db.py` copies inside the same image are the clean ones.
2. **A copy outside DigitalOcean.** The disk images live in the same account,
   so a lost or locked account takes them with it. Pull the newest nightly copy
   to a machine you own from time to time:

   ```
   ssh root@YOUR_SERVER_IP ls -t /var/backups/elusion
   scp root@YOUR_SERVER_IP:/var/backups/elusion/elusion-YYYYMMDD-HHMMSS.db .
   ```

   The first line lists the copies newest first; the second fetches one into
   the folder you are in (Windows has both commands built in). The folder is
   mode 700 and `elusion`'s, so root can read it and no other user can. Treat the
   copy like the database it is: real password hashes, never in a repo, never
   in a synced folder you share.

**Test the restore.** A backup you have never restored is a rumour.
`restore_drill.py` does the whole round trip on a copy - reads it cold, serves
it with a real server, registers, logs in, checks the accounts survived - and
never touches the live file:

```
sudo -u elusion /opt/elusion/venv/bin/python /opt/elusion/api/restore_drill.py --dir /var/backups/elusion
```

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

Since 0.10.0 there is a third pile, **WATCHED**: paid kills the presence
server's own count of the fight does not back up (SHORT, NOT DUE, or a kill the
books never saw), and checks a player tripped (a hit too big or too fast, a
walk too quick), each with its numbers - and a `books:` line in the header
counting every verdict. It ranks below every review and never alarms: the first
week is for learning whether honest play ever lands there. A kill from a game
older than 0.10.0, or one whose presence link was down, is "never seen" by
design; a teleporter is a `jumped`.

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
defaults already do. This is the standing watch over **E-3** (below): combat is
server-observed since 0.10.0, and the weekly digest's WATCHED pile is what
decides when the server starts refusing kills its count does not back up.

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
And now **contained**: the owner's trade switch ("Trading" in the GM panel's Testing tab, or
`POST /api/server/trade`) stops new trades in one request, and a fresh mythic
or Perfect find cannot be traded for 48 hours, so what a cheat mints stays in
the account that minted it while it is looked at. Switch trading off the moment
`killwatch.py` alarms, and decide before a public link whether it starts off.

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
would not. Its copies are single files in the ordinary journal mode (**Backups**,
above), so they carry no sidecars of their own. `canary.py`, `backup_db.py` and
`security_bot.py` all read the live WAL database `mode=ro` without trouble.

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
