"""The project is general now: none of the original event's specifics may
appear in the tracked files. Fixtures are real captured responses and are
exempt."""
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANNED = re.compile("|".join([
    "wed" + "ding", "cere" + "mony", "red" + r"[\s_-]*" + "bank", "molly" + r"\s*" + "pitcher",
    "nave" + "sink", "cock" + "tail", "river" + "side", "golden" + r"\s+" + "hour",
    "river" + r"\s+" + "portraits", r"weather\." + "mhp" + "webserver",
]), re.I)


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines()
            if not f.startswith("tests/fixtures/") and f != "tests/test_wording.py"]


def test_no_original_event_wording_anywhere():
    hits = []
    for rel in tracked_files():
        path = os.path.join(ROOT, rel)
        try:
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if BANNED.search(line):
                        hits.append(f"{rel}:{n}: {line.strip()[:100]}")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
    assert not hits, "\n".join(hits)
