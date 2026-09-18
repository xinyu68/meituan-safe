#!/usr/bin/env python3
"""Create the isolated Python and pinned Meituan CLI runtime."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = SKILL_DIR / ".runtime"
PYTHON_ENV = RUNTIME_DIR / "python"
UPSTREAM_DIR = RUNTIME_DIR / "meituan-cli"
REQUIREMENTS = SKILL_DIR / "requirements.txt"
PATCH_FILE = SKILL_DIR / "patches" / "meituan-cli-safe.patch"
UPSTREAM_URL = "https://github.com/juntaochi/meituan-cli.git"
UPSTREAM_COMMIT = "251522f157e14c444e5afe311f2e9562d2f71011"


def runtime_python() -> Path:
    if os.name == "nt":
        return PYTHON_ENV / "Scripts" / "python.exe"
    return PYTHON_ENV / "bin" / "python"


def run(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def tool(name: str) -> str:
    if os.name == "nt" and name in {"npm", "npx"}:
        return f"{name}.cmd"
    return name


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade", action="store_true", help="rebuild the pinned runtime")
    args = parser.parse_args()

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    python = runtime_python()
    if not python.exists():
        print(f"Creating Python runtime: {PYTHON_ENV}", file=sys.stderr)
        venv.EnvBuilder(with_pip=True).create(PYTHON_ENV)
    pip_command = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    if args.upgrade:
        pip_command.append("--upgrade")
    run([*pip_command, "-r", str(REQUIREMENTS)])

    if not UPSTREAM_DIR.exists():
        print(f"Cloning pinned Meituan CLI: {UPSTREAM_COMMIT}", file=sys.stderr)
        run(["git", "clone", UPSTREAM_URL, str(UPSTREAM_DIR)])
        run(["git", "checkout", UPSTREAM_COMMIT], cwd=UPSTREAM_DIR)
        run(["git", "apply", str(PATCH_FILE)], cwd=UPSTREAM_DIR)
    else:
        current = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=UPSTREAM_DIR, text=True
        ).strip()
        if current != UPSTREAM_COMMIT:
            raise RuntimeError(
                f"Runtime checkout is {current}; expected {UPSTREAM_COMMIT}. "
                "Remove .runtime or use a clean installation."
            )

    run([tool("npm"), "ci", "--ignore-scripts=false"], cwd=UPSTREAM_DIR)
    run([tool("npm"), "run", "build"], cwd=UPSTREAM_DIR)
    # Remove development-only packages and apply compatible production security fixes.
    run([tool("npm"), "audit", "fix", "--omit=dev"], cwd=UPSTREAM_DIR)
    run([tool("npm"), "audit", "--omit=dev", "--audit-level=high"], cwd=UPSTREAM_DIR)
    run([tool("npx"), "playwright", "install", "chromium"], cwd=UPSTREAM_DIR)
    print(python)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
