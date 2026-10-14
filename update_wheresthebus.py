#!/usr/bin/env python3
# Last updated: 2026-10-14 07:46 AM EDT (America/New_York)
"""Safely download and install the public Where's the Bus sensor and learner."""

import os
import shutil
import tempfile
from pathlib import Path
from urllib.request import urlopen

BASE_URL = (
    "https://raw.githubusercontent.com/xtrezpasor/"
    "wheresthebus_sensor/main/"
)
FILES = {
    "wheresthebus_sensor.py": "if __name__ == \"__main__\":",
    "wheresthebus_route_learner.py": "if __name__ == \"__main__\":",
}
CONFIG_DIR = Path("/config")


def download_and_validate(filename, required_text):
    url = BASE_URL + filename
    with urlopen(url, timeout=30) as response:
        data = response.read()
    text = data.decode("utf-8")
    compile(text, str(CONFIG_DIR / filename), "exec")
    if "# Last updated:" not in text[:300]:
        raise RuntimeError(f"{filename} is missing its update timestamp")
    if required_text not in text:
        raise RuntimeError(f"{filename} is missing its expected entry point")
    return data, text


def atomic_install(target, data):
    fd, temporary_path = tempfile.mkstemp(
        prefix=f".{target.stem}-", suffix=".py", dir=target.parent
    )
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(data)
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, target)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def main():
    print("Downloading and validating the bus sensor and route learner...")
    downloaded = {
        name: download_and_validate(name, expected)
        for name, expected in FILES.items()
    }

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    targets = {name: CONFIG_DIR / name for name in FILES}
    for target in targets.values():
        if target.exists():
            backup = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(target, backup)
            print(f"Saved previous {target.name} as {backup.name}")

    for name, target in targets.items():
        data, text = downloaded[name]
        atomic_install(target, data)
        print(f"Installed and syntax-checked {target} ({len(data)} bytes).")
        print(text.splitlines()[1])


if __name__ == "__main__":
    main()
