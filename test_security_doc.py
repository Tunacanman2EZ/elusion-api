"""SECURITY.md is checked, not trusted.

SECURITY.md ends every non-structural promise with a line like

    -> Held by: `test_economy.py` - "injected gold is reported as drift"

and says, in bold, that if the page and the tests disagree the page is wrong.
THIS SUITE IS WHAT MAKES THAT SENTENCE TRUE RATHER THAN A NICE INTENTION. It
reads the page, pulls every citation out of it, and fails if a suite named there
does not exist or a check quoted there is not in it.

WHY THIS EXISTS AT ALL, and it is not a style preference. Two days of work in
this repository came out of exactly one bug, twice:

  - api.gd said "characterhud.gd answers it with an immediate heartbeat()".
    Nothing was connected. The sentence described a wire nobody had run.
  - MAX_MOD_BAN_DAYS carried a comment about how long a mod may MUTE somebody
    for. There is no mute in this server. That comment was one draft away from
    putting a moderator power that does not exist into SECURITY.md's actors
    table, which is the one document a stranger would read and believe.

A security page is the highest-consequence place in the repository for that
failure, because it is read by people who cannot check it themselves - a
teammate, a publisher, a player deciding whether to trust the server with a
password. So the page does not get to claim a test exists. It has to name one,
and this suite resolves the name.

WHAT IT DELIBERATELY DOES NOT DO: judge whether a check is any good, or whether
it really proves the promise it is cited under. No parser can do that. It
resolves references, which is the failure that actually happened here twice.

Run: python test_security_doc.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DOC = os.path.join(HERE, "SECURITY.md")

passed = 0
failed = 0
skipped = 0
failures = []


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  pass  %s" % label)
    else:
        failed += 1
        failures.append(label)
        print("  FAIL  %s   %s" % (label, detail))


def skip(label, why):
    global skipped
    skipped += 1
    print("  skip  %s   (%s)" % (label, why))


print("\n=== SECURITY.md  -  the page names its tests, and they resolve ===\n")

check("SECURITY.md exists", os.path.exists(DOC),
      "the page the repo's security policy points at")
if not os.path.exists(DOC):
    print("\n  0 passed, 1 failed")
    sys.exit(1)

doc = open(DOC, "r", encoding="utf-8").read()

# The promise the rest of this suite enforces. If somebody softens this line the
# page stops being checkable and starts being marketing.
check("the page says the tests win when they disagree",
      "the page is wrong" in doc.lower() and "trust the tests" in doc.lower(),
      "that sentence is the whole contract between this page and this suite")
check("and it says this suite is what enforces that",
      "test_security_doc.py" in doc,
      "a reader has to be able to find the thing that checks the page")

# -----------------------------------------------------------------------------
# THE CITATIONS
# -----------------------------------------------------------------------------
# A citation line names one or more files in backticks and quotes zero or more
# check labels. Zero is legal: the Godot entry names a function in another
# repository, which this suite can see the name of and nothing more.

CITATION = re.compile(r"Held by:(.*)")
cited_files = set()
claims = []
for line in doc.splitlines():
    m = CITATION.search(line)
    if not m:
        continue
    tail = m.group(1)
    files = re.findall(r"`([A-Za-z0-9_./]+\.(?:py|gd))`", tail)
    # Curly quotes are what a word processor leaves behind; accept them so a
    # copy-edit cannot silently orphan a citation.
    labels = re.findall(r'["“]([^"”]{8,120})["”]', tail)
    cited_files.update(files)
    if files or labels:
        claims.append((files, labels, line.strip()))

# Continuation lines: a claim may add a second "-> and in the game:" line.
for line in doc.splitlines():
    if "Held by:" in line:
        continue
    if "and in the game:" not in line:
        continue
    files = re.findall(r"`([A-Za-z0-9_./]+\.(?:py|gd))`", line)
    labels = re.findall(r"`(_test_[A-Za-z0-9_]+)\(\)`", line)
    cited_files.update(files)
    if files:
        claims.append((files, [], line.strip()))

check("the page cites some tests", len(claims) >= 8, len(claims))
check("and several distinct suites", len(cited_files) >= 4, sorted(cited_files))

# -----------------------------------------------------------------------------
# EVERY CITED FILE RESOLVES
# -----------------------------------------------------------------------------
readable = {}
for name in sorted(cited_files):
    path = os.path.join(HERE, name)
    if os.path.exists(path):
        readable[name] = open(path, "r", encoding="utf-8").read()
        check("%s exists" % name, True)
    elif name.endswith(".gd"):
        # THE GODOT SUITE LIVES IN THE OTHER REPOSITORY, and where the two
        # checkouts sit relative to each other is up to whoever cloned them -
        # CLAUDE.md refuses to guess that path for the same reason. A skip that
        # says so beats a failure nobody can fix from here, and beats a pass
        # that pretends to have looked.
        skip("%s is reachable" % name, "game repo, path not knowable from here")
    else:
        check("%s exists" % name, False,
              "SECURITY.md names a suite that is not in this folder")

# -----------------------------------------------------------------------------
# EVERY QUOTED LABEL IS REALLY IN ONE OF THE FILES ON ITS OWN LINE
# -----------------------------------------------------------------------------
# Matched as a whole quoted string, the way check() is called with it, so a
# paraphrase fails. That is the point: the page may not describe a check, it has
# to name one.
checked_labels = 0
for files, labels, line in claims:
    for label in labels:
        here = [f for f in files if f in readable]
        if not here:
            skip("a label on an unreachable suite", line[:70])
            continue
        found = any(('"%s"' % label) in readable[f] for f in here)
        checked_labels += 1
        check('"%s" is a real check in %s' % (label[:58], "/".join(here)), found,
              "the page quotes a check that is not in the suite it credits")

check("labels were actually resolved, not silently skipped", checked_labels >= 10,
      checked_labels)

# -----------------------------------------------------------------------------
# THE PAGE'S OWN SHAPE
# -----------------------------------------------------------------------------
# Three sections carry the weight. An actors table with no "not trusted" column
# is a feature list; an invariants list with no limits section is marketing.
for heading in ("## The premise", "## Actors and trust", "## Invariants",
                "## Honest limits", "## Where the detail lives"):
    check("the page still has %s" % heading, heading in doc,
          "one of the five sections that make this a model rather than a summary")

check("the premise is stated in one sentence, plainly",
      "The player's machine is hostile" in doc,
      "everything else on the page is downstream of that line")
check("the actors table has a 'not trusted with' column",
      re.search(r"not\*?\*? trusted with", doc, re.IGNORECASE) is not None,
      "a table of what each actor CAN do is a feature list")

# The limits section is the one a reader cannot verify and most needs to be
# honest, so pin the entries that are known-open rather than counting bullets.
# An entry leaves this list only with the fix that closed it: the backpack
# ledger went when the bag became the server's (invariant 5, test_bagmoves.py),
# and the VPN became "a new computer" when bans followed the install id.
for open_item, why in [
    ("E-3", "the kill event is asserted, not proven"),
    ("new computer", "defeats ban evasion"),
    ("patched client", "can ignore a 401 and keep drawing the world"),
]:
    check("the limits still name %s (%s)" % (open_item, why),
          open_item in doc,
          "an open gap dropped from this list reads as a gap that was closed")

# And the one that would be easiest to quietly drop, because it is the least
# flattering: the page has to say the server does NOT own kills.
check("the page does not claim the server owns kill events",
      not re.search(r"server\b[^.]{0,80}\bowns\b[^.]{0,40}\bkills?\b", doc, re.IGNORECASE),
      "E-3 is open; an actors table saying the server owns kills contradicts it")

# =============================================================================
# DEPLOY.md NAMES THE RUNNER, NOT A LIST OF SUITES
# =============================================================================
# THE SAME FAILURE AS EVERY OTHER ENTRY IN THIS SUITE, in the one document that
# gates going public. The go-live checklist used to read:
#
#     - [ ] All four suites green against the deployed code, not against a
#           working copy: test_api.py, test_security.py, test_throttle.py,
#           test_gathering.py.
#
# True when it was written. By the time anybody followed it there were
# twenty-seven suites, so following it meant going public having run four of
# them - and not the four that matter most at that moment, because
# test_revocation.py, test_refusals.py, test_ownership.py and
# test_maintenance.py all cover behaviour that only HAS consequences once
# somebody else can connect, and none of them was on the list.
#
# run_tests.ps1 discovers suites with Get-ChildItem test_*.py, so it has been
# correct the whole time. The checklist was the only thing that went stale, and
# it is the thing a person reads before opening the firewall.
#
# CLAUDE.md already carries a section explaining that a written-down COUNT
# cannot fail and therefore stays wrong - and that section had itself gone stale
# by one, saying "twenty-six". A written-down LIST is the same defect with more
# words. So this checks the instruction rather than the number: name the runner,
# and let it count.

print("\n=== DEPLOY.md  -  the go-live checklist names the runner ===\n")

DEPLOY = os.path.join(HERE, "DEPLOY.md")
check("DEPLOY.md exists", os.path.exists(DEPLOY),
      "the checklist somebody follows before the first stranger connects")

if os.path.exists(DEPLOY):
    dep = open(DEPLOY, "r", encoding="utf-8").read()

    GATE = "## Before the first stranger connects"
    check("it still has %s" % GATE, GATE in dep,
          "the section this whole file exists to produce")

    section = ""
    if GATE in dep:
        after = dep.split(GATE, 1)[1]
        # BOUNDED TO THE SECTION, because "run_tests.ps1" appears elsewhere in
        # the file and an unscoped search would pass on a checklist that never
        # mentions it. Same rule the Godot suite needed five times over.
        section = after.split("\n---", 1)[0]

    check("the checklist points at run_tests.ps1",
          "run_tests.ps1" in section,
          "a checklist that names suites instead of the runner goes stale the "
          "next time one is added, and nobody re-reads a checklist")

    # A COUNT BOUND TO THE WORD "suites" ANYWHERE IN THE FILE. This is the exact
    # shape that rotted - "All four suites" - and it rots silently because the
    # sentence stays grammatical forever.
    COUNTED = re.compile(
        r"\b(?:all|the|these|those)\s+"
        r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"twenty[- ]?\w*|thirty[- ]?\w*)\s+suites\b",
        re.IGNORECASE)
    hit = COUNTED.search(dep)
    check("no line binds a number to the word 'suites'",
          hit is None,
          ("'%s' - run_tests.ps1 prints the count; this file must not"
           % hit.group(0)) if hit else "")

    # EVERY SUITE IT DOES NAME HAS TO RESOLVE, the same discipline SECURITY.md
    # is held to above. Naming one is fine - as an example, as a pointer - but a
    # name that no longer exists is a reader sent to a file that is not there.
    named = sorted(set(re.findall(r"\btest_[a-z_]+\.py\b", dep)))
    check("DEPLOY.md names at least one suite to point at", len(named) >= 1,
          named)
    for name in named:
        check("%s named in DEPLOY.md exists" % name,
              os.path.exists(os.path.join(HERE, name)),
              "renamed or deleted, and the checklist still sends you to it")

print("\n" + "=" * 70)
print("  %d passed, %d failed, %d skipped" % (passed, failed, skipped))
if failures:
    print("\n  failed:")
    for f in failures:
        print("    - %s" % f)
print("=" * 70)
sys.exit(1 if failed else 0)
