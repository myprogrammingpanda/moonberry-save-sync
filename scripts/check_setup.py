"""Quick pre-launch check run by run.bat on every start.

Exit codes (run.bat branches on these; anything else, e.g. 1 from a
crash in this script, just launches the app as before):
    0  ready to launch
    10 one or more packages from requirements.txt aren't installed
    11 this Python is too old
    12 the app is running out of a temp folder (i.e. straight out of the
       zip without extracting it first)

Deliberately cheap: it only looks up installed package metadata via
importlib.metadata instead of importing PySide6/boto3, so a normal
launch stays instant. Keep this file free of newer-Python syntax (no
f-strings or annotations) -- it has to get far enough on an old
interpreter to report exit code 11.
"""
import os
import re
import sys
import tempfile

MIN_PYTHON = (3, 11)
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_under_temp():
    temp = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
    here = os.path.normcase(os.path.realpath(APP_DIR))
    return here == temp or here.startswith(temp + os.sep)


def missing_requirements():
    from importlib import metadata

    missing = []
    with open(os.path.join(APP_DIR, "requirements.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            # Package name only -- drop extras, version specifiers, markers.
            name = re.split(r"[\s\[<>=!~;@]", line, maxsplit=1)[0]
            try:
                metadata.distribution(name)
            except metadata.PackageNotFoundError:
                missing.append(name)
    return missing


def main():
    if sys.version_info < MIN_PYTHON:
        print("Python %d.%d or newer is required (this is %d.%d)."
              % (MIN_PYTHON + sys.version_info[:2]))
        return 11
    if is_under_temp():
        print("Running from a temp folder: %s" % APP_DIR)
        return 12
    missing = missing_requirements()
    if missing:
        print("Missing packages: " + ", ".join(missing))
        return 10
    return 0


if __name__ == "__main__":
    sys.exit(main())
