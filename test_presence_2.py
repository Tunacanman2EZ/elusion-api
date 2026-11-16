"""
Players seeing each other, part 2. Run: python3 test_presence_2.py

test_presence.py holds every check and every helper of the presence suite, and
runs part 1 itself. This runs that same file's PART 2 - every section with a
server of its own (P-12 to P-30), then the attacks riding the tick and the
crowd walking in (P-9b, P-9c) - in a process of its own, with a throwaway
database of its own (test_presence.py's code makes it in testdir.PATH), so
run_tests.ps1 plays the two halves side by side.

Why (Nov 2026): one file was 206 s of a 274 s run, every other suite finished
and waiting on it. Nothing else lives here: a check is added to
test_presence.py, in part_one() or part_two().
"""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_presence.py"),
               init_globals={"PRESENCE_PART": 2}, run_name="__main__")
