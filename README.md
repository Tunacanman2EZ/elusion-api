# Elusion API

The server behind [Elusion RPG](https://elusionrpg.com): accounts, characters,
loot, the economy, chat, parties and minigames (a Flask app, `app.py`), and
the live socket that lets players see each other (`presence.py`). The game
itself is a separate repository; `docs/apicontract.md` there is the agreement
between the two.

## Run it on your own computer

Windows PowerShell, from wherever you keep your code:

```
git clone https://github.com/Tunacanman2EZ/elusion-api.git
cd elusion-api
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

(On macOS or Linux: `source venv/bin/activate` instead of the Activate line.)

Then, in two windows, both with the venv active:

```
python app.py         # the API, on http://127.0.0.1:5000
python presence.py    # players seeing each other, on ws://127.0.0.1:5001
```

The game talks to `http://127.0.0.1:5000` by default, so a game run from the
Godot editor finds this server with nothing to set. `presence.py` is only
needed for other players on screen, parties and minigames together.

**Stop the old process before starting a new one.** Windows lets a second
process take port 5000 without an error, and the old one keeps answering with
old code - a 404 on a route that plainly exists is usually this.

## Settings: `.env`

```
Copy-Item .env.example .env
```

`.env.example` lists every setting the server reads, all commented out, with
made-up values. Uncomment only what you need in your `.env`. For a local
server that is usually just:

- `ELUSION_OWNER=your_username` - your account gets the owner's rank (the top
  rank comes from here, never from the database).
- `ELUSION_MAIL_CONSOLE=1` - recovery and login codes are printed in the
  window instead of emailed. Local only; the real server refuses it.

**Never commit `.env`**, and never put a real password or key in
`.env.example` - that file is in the repository. `.env` is in `.gitignore`.
Other ranks are set on the machine holding the database, never over the
network: `python set_role.py Someone mod` (`--list` shows everyone).

## The database

`elusion.db` is made beside `app.py` the first time the server starts
(`ELUSION_DB` puts it elsewhere). **It holds real accounts and password
hashes: it is gitignored and must never be committed or shared** - and the
same goes for any copy of it (`elusion.db.before-...`, `-wal`, `-shm`), which
`.gitignore` also covers.

### Changing the schema

There is no migration command: the server migrates itself every time it
starts. `init_db()` runs when `app.py` is imported, creates any missing
tables (the core's, then each package's `schema.py`), then runs each
`_migrate_...` step and each package's `upgrade()`. To change a table:

1. Add the column to the table's `CREATE TABLE` (for new databases) **and**
   to its `upgrade()` or a `_migrate_...` step (for databases that already
   exist - `CREATE TABLE IF NOT EXISTS` changes nothing on an existing table).
2. Add a migration test that starts from the **old** shape. Every suite
   builds a fresh database, so without one every test passes while the live
   server is broken.
3. Run `python test_schema.py --write-fixture` and read the diff of
   `test_schema.json` before committing it. That diff is the schema change.

## Tests

```
.\run_tests.ps1                         # every test_*.py, several at once
.\run_tests.ps1 -Jobs 1                 # one at a time
.\venv\Scripts\python.exe test_economy.py   # one suite
```

The exit code is the result: non-zero if any suite failed. Every suite builds
its own throwaway database in your temp folder, so none of them touches
`elusion.db`. Run them before and after every change.

## Where to read more

- `CLAUDE.md` - how to work in here, and the traps that cost real hours.
- `SECURITY.md` - the security model, each promise with the test that holds it.
- `ARCHITECTURE.md` and `CODEMAP.md` - how the parts fit, and where each lives.
- `DEPLOY.md` - the live server: setup, settings and how it is updated.
- `RUNBOOK.md` - running it: shipping, health, what to do when it breaks,
  backups, load tests, and the incidents behind each habit.

## Licence

MIT - see `LICENSE`.
