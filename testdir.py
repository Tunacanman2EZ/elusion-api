"""testdir.py - a private folder for one run of one suite's databases.

UPGRADES.md item 2 (18 Oct). Every suite used to put its database at a fixed
name in the shared temp folder (tempfile.gettempdir()), so two runs of the
same suite at once - two terminals, two CI jobs, a sabotage copy beside the
real tree - wrote over each other's data and failed for no reason of their
own. Each suite now asks this module for its folder instead:

    import testdir
    DB_PATH = os.path.join(testdir.PATH, "elusion_api_test.db")

PATH is made fresh for each process (tempfile.mkdtemp), so nothing is shared,
and removed when the process ends, so a run leaves nothing behind. A file the
suite still holds open at exit (Windows will not delete one) is left for the
system's temp cleaning rather than failing the run.

Not a suite: run_tests.ps1 and the code map look for test_*.py.
"""
import atexit
import os
import shutil
import tempfile

PATH = tempfile.mkdtemp(prefix="elusion_test_")

atexit.register(shutil.rmtree, PATH, True)

# A NEW CHARACTER STARTS WITH AN EMPTY BAG IN THE SUITES. Game 0.92.0 gave a
# new character starter potions on its first keys; dozens of checks across
# the suites count a new character's cells, written when it had none. Off
# here, before any suite loads app.py; test_chardelete's CD-8 turns it on
# and checks the kit.
os.environ.setdefault("ELUSION_STARTER_KIT", "off")
