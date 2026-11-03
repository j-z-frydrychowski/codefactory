"""Opakowanie komendy testów: przepuszcza jej wynik, ale kończy się błędem, gdy żaden test się nie uruchomił.
Użycie: python nonempty.py node --test   (node --test bez testów kończy się kodem 0, więc sama niczego nie dowodzi)"""
import re
import shutil
import subprocess
import sys

HOLLOW = re.compile(r"(?im)^\s*\S?\s*(tests 0|ran 0 tests)\b|no tests (found|ran)")  # \S? = symbol reportera (np. "ℹ" w node)

argv = sys.argv[1:]
if not argv:
    sys.exit("podaj komendę testów")
argv[0] = shutil.which(argv[0]) or argv[0]
p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
out = p.stdout + p.stderr
sys.stdout.write(out)
if p.returncode == 0 and HOLLOW.search(out):
    sys.exit("nonempty: komenda przeszła, ale nie uruchomiła żadnego testu")
sys.exit(p.returncode)
