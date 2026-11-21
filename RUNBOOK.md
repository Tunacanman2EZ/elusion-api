# Elusion runbook

How Elusion is shipped, watched, backed up and fixed when it breaks, and the
incidents behind each habit. It is the short version: every section points to
the full detail in `DEPLOY.md`. How the parts fit together is
`ARCHITECTURE.md`; the security model is `SECURITY.md`.

Elusion is a free online action RPG I build and run alone: a Godot 4 game,
this Flask API with a WebSocket presence server, and a static website. It
runs on one $6 DigitalOcean droplet.

---

## The system at a glance

| Piece | What it is | Where |
|---|---|---|
| Website | Static pages | elusionrpg.com (Netlify) |
| Browser build | The game, exported to the web | play.elusionrpg.com (the droplet, behind Caddy) |
| API | Flask under gunicorn (`--preload`): accounts, loot, economy, chat, minigames | systemd `elusion-api`, behind Caddy at api.elusionrpg.com |
| Presence | `presence.py`, WebSockets: players seeing each other, parties, shared monsters | systemd `elusion-presence`, 127.0.0.1:5001 behind Caddy |
| Database | SQLite in WAL mode | `/var/lib/elusion/elusion.db` |
| Settings | The only place secrets live (root-owned, mode 600) | `/etc/elusion/elusion.env` |
| Backups | Nightly, self-verifying | `/var/backups/elusion` (mode 700) |
| Machine | 1 vCPU, 1 GB RAM, 25 GB disk, 1,000 GiB outbound a month | DigitalOcean `elusion-1`, Ubuntu 24.04 |

The firewall allows SSH (keys only), 80 and 443. Caddy fetches and renews the
TLS certificates itself.

---

## 1. Ship a change

Everything goes out through one script, from the game's folder on the PC:

```
.\tools\ship.ps1
```

It reads the `SHIP_*.txt` files waiting in the commit folder (one per repo,
each holding that commit's message), then for each repo in order commits,
pushes and renames the file to `SHIPPED_<stamp>_...`:

1. **The API** first, so a new game never meets an old server. It then
   offers to update the server (below).
2. **The art pack** (a private submodule), then **the game**. For the game it
   offers to export and deploy the browser build.
3. **The website.**

Before running it: close Godot without saving. The web export needs the
editor closed; if it was open, the export is skipped and runs later with
`.\tools\deploy_web.ps1 -Export`.

**Every game change raises the version** (PATCH for a fix, MINOR for a
feature): `DISPLAY_VERSION` in `src/systems/api.gd` and `export_presets.cfg`,
which a test holds together, plus a row on the website's update pages. A
docs-only change ships without a new version.

### Update the server

```
ssh root@api.elusionrpg.com bash /opt/elusion/api/deploy/update_server.sh
```

`deploy/update_server.sh` pulls, checks the settings file has every name the
new code needs (names only, never values), installs requirements only when
they changed, **backs up the database**, restarts both services, and asks
`/api/status`. It stops before the restart if a setting is missing, so the
old process keeps serving.

**A version that does not come up is taken back by itself.** If `/api/status`
does not answer, or presence never logs `[PRESENCE] listening`, the script
puts the last healthy commit back, restarts, checks again, and ends with one
of:

- `TAKEN BACK: <new> did not come up, so <old> is running again.` Players are
  on the old version. Read the two `journalctl` lines it prints, fix, push,
  run it again.
- `TAKEN BACK TO <old>, AND IT DID NOT COME UP EITHER.` The trouble is outside
  the code: the settings, the disk or the database. The backup it took a
  minute ago is the newest file in `/var/backups/elusion`.

The database is never put back automatically, because every save since the
backup would go with it. Full detail: `DEPLOY.md`, "Updating the server".

---

## 2. Is it healthy?

| Where | What normal looks like |
|---|---|
| `curl -s https://api.elusionrpg.com/api/status` | Answers at once, with the game data's item and enemy counts |
| The GM panel's **Health** tab (owner only, no SSH) | Presence ticks ten times a second with no late beats; routes in single-digit milliseconds at p99 (sign-up is ~100 ms, on purpose, for the password hash); this month's data on track under the plan |
| `sudo journalctl -u elusion-api -n 30 --no-pager` | `[BOOT]` and `[DEPLOY]` lines, no tracebacks |
| `GET /api/metrics` (owner token) | Per-route count, error rate, p50/p95/p99 |

**The data guard.** The plan includes 1,000 GiB of outbound data a month.
`presence.py` counts what it sends each minute and, if the month is heading
over 95% of that, steps down one level at a time: other players at half rate,
then at most 16 players seeing each other in one area, then nobody seeing
anybody. It steps back up when there is room. The Health tab's Data page says
which level it is at and why. DigitalOcean's spend alerts only warn, which
is why the guard exists.

---

## 3. When something breaks

| Symptom | First move |
|---|---|
| "Could not reach the server" for everyone | `curl` `/api/status`. No answer: `sudo systemctl status elusion-api`, then `sudo journalctl -u elusion-api -n 50 --no-pager` |
| Players can log in but don't see each other | Presence. The Health tab saying "presence.py has not written a minute" after an update means the old process is still running: `sudo systemctl restart elusion-presence`. Games reconnect by themselves within seconds |
| An update did not come up | It was rolled back already: read the `TAKEN BACK` line (section 1) |
| Requests fail after running a tool on the server | The tool was run as root and created WAL files (`-wal`, `-shm`) the service cannot write. Run every tool as the service user: `sudo -u elusion env ELUSION_DB=/var/lib/elusion/elusion.db /opt/elusion/venv/bin/python /opt/elusion/api/<tool>.py` |
| A route that clearly exists answers 404 (on the PC) | An old `python app.py` is still on port 5000; Windows lets a second process share it. Stop the old one |
| The month's data is running low | The data guard is already stepping down. Check the Health tab's Data page; a bigger droplet includes more (`ELUSION_TRANSFER_GIB`) |
| The disk or the database itself | Stop, don't improvise: the newest backup is in `/var/backups/elusion`, and section 4 says how to bring one back |

---

## 4. Backups and restore

**Nightly**, `backup_db.py` takes a snapshot with SQLite's online-backup API
(safe while players are on), reopens it, runs `integrity_check`, reads a real
row, and exits non-zero if anything fails, so the scheduler knows. The live
server keeps the newest 14, and every update takes one more before the
restart.

**Off the machine.** A backup on the same disk dies with it, so there are two
more layers: DigitalOcean's own droplet backups (a whole-disk image, kept
apart from the droplet), and a copy pulled to a machine I own from time to
time:

```
ssh root@api.elusionrpg.com ls -t /var/backups/elusion
scp root@api.elusionrpg.com:/var/backups/elusion/elusion-YYYYMMDD-HHMMSS.db .
```

Fetch `elusion-deleted-accounts.txt` with it: restored on a new machine as
`elusion.db.deleted`, it makes sure an account a player deleted stays deleted
in an older backup. A backup holds real password hashes, so it never goes in a
repository or a shared folder.

**A backup never restored is a rumour.** `restore_drill.py` does the whole
round trip on a copy (opens it cold, serves it with a real server, registers,
logs in, checks the accounts survived) and never touches the live file:

```
sudo -u elusion /opt/elusion/venv/bin/python /opt/elusion/api/restore_drill.py --dir /var/backups/elusion
```

Full detail: `DEPLOY.md`, "Backups".

---

## 5. Load testing

`swarm.py` plays the game with a crowd of bots the way real games use the
server: a socket held open, ten steps a second each relayed to everyone in
the area, monsters streamed and booked, an HTTP request about every second
besides. It starts a **scratch copy of the whole server**, pinned to one core
like the live box, and is never pointed at the live one.

| Crowd | What it cost on one container core |
|---|---|
| 128 players in one area, all fighting | presence 13% of a core, the API 12%; a step's p99 104 ms |
| 256 in one area | presence 25%, the API 24%; p99 118 ms |
| Data out | 13 MB/s at 128 in one place, 48 at 256; 4 MB/s for 128 spread over the Big Field |

**What it taught:** the processor is not the limit; data is. What goes out
grows with the square of a crowd in one place, so the fixes were sending each
player only the players near them, and the data guard. Full detail:
`DEPLOY.md`, "Playing it: the bot swarm".

---

## 6. Incidents

Short write-ups of real problems: what was seen, how the cause was found, the
fix, and the proof.

### Backups that came in threes (October 2026)

- **Seen:** the first night on the live server left three files where one
  backup was meant, and pruning only knew about the `.db`.
- **Cause:** the live database runs in WAL mode, and the backup API copies
  the mode with the pages, so every backup was a WAL database too. Opening one
  at all, even to verify it, made SQLite create `-wal` and `-shm` beside it.
- **Fix:** each copy is switched to `journal_mode=DELETE` before it is closed,
  verification refuses a copy still in WAL mode, and every run settles older
  copies that have sidecars. The live database stays in WAL.
- **Proof:** `test_deploy.py`.

### A minigame you couldn't leave (0.168.8)

- **Seen:** leaving Crownfall showed "Could not reach the server", and Leave
  did nothing. The player was trapped in the minigame.
- **Cause:** leaving waited for the server to pay the run's gold. A server that
  did not answer, or refused (a run it would never pay), kept the screen open
  forever. Four minigame screens shared the pattern.
- **Fix:** one shared reading of the server's answer (`payanswer.gd` in the
  game). A refusal, or a second failure, lets the player leave without that
  run's gold and says so plainly; a run worth nothing closes at once.
- **Proof:** a test that leaving never traps, across the four screens and
  the server's possible answers.

### Frame spikes nobody could reproduce (0.168.8)

- **Seen:** the readout on my PC showed drawing spikes, which I suspected came
  from going through teleporters.
- **How it was chased:** the sandbox's software renderer draws every frame
  slowly, which hides a spike entirely, so it could not be reproduced there.
- **Response:** instead of guessing, the game now names its own events beside a
  spike in the readout (a teleport, a Crownfall portal, a map opening), so the
  next spike on real hardware says what caused it.
- **Lesson:** when you can't reproduce a problem, make the system report it.

### A thousand draws for a still picture (0.168.10)

- **Seen:** nothing yet; this was a measuring pass. Draw calls per frame on
  the same renderer my PC uses: the Healer's Run 40, the Mage's Stand 163,
  Cut the Cloth about 1,000.
- **Cause:** Cut the Cloth's cloth (a weave of about 470 lines, the map's
  ink, the frayed edges) was sent to the graphics card again every frame,
  though it only changes when the player cuts.
- **Fix:** the cloth is drawn once into an off-screen picture each time it
  changes, and the game shows that picture: one draw.
- **Proof:** 971 to 35 draws a frame on the same run; the cloth compared old
  against new, pixel by pixel, through a whole cut on a fixed seed: identical.

### Bricks flickering out of place (0.168.11)

- **Seen:** "when I hit a brick with the ball, random bricks flicker for a
  split second."
- **How it was found:** a first check (is the cached brick picture ever
  stale?) came back clean over nine maps. So the game was recorded frame by
  frame on the PC's renderer, reading three points of every standing brick
  off the screen. Every flicker landed on a frame where a new highlight copy
  of a brick had just been made.
- **Cause:** the project smooths movement between physics ticks, but
  Crownfall moves everything itself every frame. A brick's new copy was added
  at (0, 0) and then placed, so for one frame it was drawn partway from the
  corner.
- **Fix:** smoothing off for Crownfall, as the pinball table already had it.
- **Proof:** the same seed, 1,000 frames: 4 flickers before, 0 after; a test
  holds the setting.

---

## Rules that hold everywhere

- Secrets live only in `/etc/elusion/elusion.env` on the server and in a
  local `.env` that is never committed. `.env.example` holds names and
  made-up values only.
- The database and every copy of it (`.db`, `-wal`, `-shm`, `.before-*`,
  backups) never go in a repository.
- Tools on the server run as `elusion`, never as root.
- Load tests run against a copy, never the live server.
- Ranks are set on the machine holding the database (`set_role.py`), never
  over the network.
