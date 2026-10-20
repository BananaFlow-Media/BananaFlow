"""Package the stdlib-only updater independently of the live application's DLLs."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent


def build(destination: Path) -> Path:
    if sys.platform not in {"win32", "darwin"}:
        raise RuntimeError("The application updater is packaged only for Windows/macOS")
    destination.mkdir(parents=True, exist_ok=True)
    name = "bananaflow-updater"
    output = ROOT / "build" / "updater-dist"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--name", name,
        "--distpath", str(output),
        "--workpath", str(ROOT / "build" / "updater-work"),
        "--specpath", str(ROOT / "build" / "updater-spec"),
    ]
    # A background replacement must not flash a Windows console. On macOS
    # --windowed would create another .app instead of the required executable.
    if sys.platform == "win32":
        command.append("--noconsole")
    command.append(str(ROOT / "core" / "app_update_helper.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    executable = name + (".exe" if sys.platform == "win32" else "")
    result = destination / executable
    shutil.copy2(output / executable, result)
    if not result.is_file() or result.stat().st_size == 0:
        raise RuntimeError("The standalone updater executable is missing")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    print(build(parser.parse_args().destination))
