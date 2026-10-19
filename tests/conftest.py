import os
from pathlib import Path

# Automatically bind local dataset fixtures if present so all real-data e2e tests run
if "BUGSINPY_ROOT" not in os.environ and Path("data/BugsInPy").is_dir():
    os.environ["BUGSINPY_ROOT"] = "data/BugsInPy"

if "PYSNOOPER_REPO" not in os.environ and Path("data/repos/PySnooper").is_dir():
    os.environ["PYSNOOPER_REPO"] = "data/repos/PySnooper"
