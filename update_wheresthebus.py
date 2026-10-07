#!/usr/bin/env python3
# Last updated: 2026-10-07 04:48 PM EDT (America/New_York)
"""Safely download and install the public Where's the Bus sensor from GitHub."""

import os
import shutil
import tempfile
from pathlib import Path
from urllib.request import urlopen

SOURCE_URL = (
    "https://raw.githubusercontent.com/xtrezpasor/"
    "wheresthebus_sensor/main/wheresthebus_sensor.py"
)
TARGET = Path("/config/wheresthebus_sensor.py")
BACKUP = Path("/config/wheresthebus_sensor.py.bak")


def main():
    print("Downloading the latest bus sensor from GitHub...")
    with urlopen(SOURCE_URL, timeout=30) as response:
        data = response.read()

    text = data.decode("utf-8")
    compile(text, str(TARGET), "exec")

    if "# Last updated:" not in text[:300]:
        raise RuntimeError("Downloaded file is missing its update timestamp.")
    if 'if __name__ == "__main__":' not in text:
        raise RuntimeError("Downloaded file does not contain its main entry point.")

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if TARGET.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Saved previous script as {BACKUP}")

    fd, temporary_path = tempfile.mkstemp(
        prefix=".wheresthebus-", suffix=".py", dir=TARGET.parent
    )
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(data)
        os.replace(temporary_path, TARGET)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)

    first_lines = text.splitlines()[:2]
    print(f"Installed and syntax-checked {TARGET} ({len(data)} bytes).")
    print(first_lines[1])


if __name__ == "__main__":
    main()
