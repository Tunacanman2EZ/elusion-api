# Upgrades noticed along the way

18 Oct. The owner, while app.py is being taken apart: "if anything needs
upgraded along the way keep track so i know". This is that list. Each item was
checked before it was written here, and each says where it is, why it matters,
and how big the fix is. The architecture moves themselves are not listed:
those are ARCHITECTURE.md's plan.

When one is fixed, it moves to **Done** at the bottom with the date. New ones
go at the end of **Open**.

## Open

### 1. The API docs page is blank

- **Where:** the docstring of `POST /api/staff/skill` (`staff_skill()` in
  app.py), the line `description: "One skill, or \"all\""`.
- **What happens:** in a Python string `\"` is just `"`, so the docs builder
  reads `"One skill, or "all""`, which is broken YAML. `/apispec_1.json` then
  answers 500, so `/apidocs` loads with nothing in it. Every route's
  documentation is missing, not just this one.
- **Why it matters:** ARCHITECTURE.md calls each docstring "its page in
  /apidocs", and none of them can be read there today. Nothing tests that the
  spec builds, which is how this went unnoticed.
- **Fix:** small. Write the line as `description: 'One skill, or "all"'`, and
  add a check to test_routes.py that `/apispec_1.json` answers 200.

### 2. The test suites cannot run side by side

- **Where:** 52 of the 61 API suites put their test database at a fixed name
  in the shared temp folder (`tempfile.gettempdir()`).
- **What happens:** two copies of a suite running at once write over each
  other's database and fail for no real reason. So every run is one suite at a
  time, about ten minutes for all of them.
- **Why it matters:** a private temp folder per run would let CI and local runs
  go in parallel, several times faster, and stop the false failures.
- **Fix:** medium, but mechanical: one line per suite (`tempfile.mkdtemp()`
  instead of the fixed path), checked suite by suite.

### 3. `parse_equipment()` is called by nothing

- **Where:** app.py, under SAVES.
- **What happens:** since equipment became server-owned (the equip routes),
  no route calls it. Its comment says the equip route's checks are made of it,
  but equipment/ asks `gamedata.equip_check()` directly. wsgi.py,
  SECURITY_NOTES.md and two test comments still describe what it does.
- **Why it matters:** a function that looks like a guard but guards nothing
  invites the next route to trust it without checking it is still wired in.
- **Fix:** small. Remove it and correct the four places that describe it.

### 4. A leftover in the save route

- **Where:** `PUT /api/save` (`write_save()`, saves/routes.py since 18 Oct),
  the line `stored_level = 1 if is_new else int(existing["level"])`.
- **What happens:** the value is never used. The comment above it says
  equipment is gated on it, which stopped being true when the save stopped
  writing equipment.
- **Fix:** tiny. Drop the variable, keep the read (it still decides whether the
  character is new), and correct the comment.

### 5. Small tidy-ups

- test_ownership.py, `sql_statements()`: a regex named `pattern` that is never
  used (the scan uses the one written inline below it).
- test_gifts.py: `json` is imported and never used.
- `GET /api/character` (saves/data.py, `identity()`) reads `saves.bank_gold`
  and never uses it. That column is a leftover of the per-character bank: its
  gold was moved to `accounts` once, at startup, and nothing writes it now.
  Drop it from the read; the column itself can go with the table moves
  (ARCHITECTURE.md step 11).
- **Fix:** tiny.

## Done

Nothing yet.
