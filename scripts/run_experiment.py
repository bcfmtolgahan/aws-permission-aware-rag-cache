#!/usr/bin/env python3
"""One-shot create/test/measure/destroy; cleanup also runs after a test failure."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(script, *args):
    subprocess.run([sys.executable, str(ROOT/"scripts"/script), *args], cwd=ROOT, check=True)


def main():
    run("check_local.py")
    run("build.py")
    run("cloud.py", "preflight")
    run("cloud.py", "init")
    run("cloud.py", "plan")
    run("cloud.py", "review")
    try:
        run("cloud.py", "apply")
        run("verify_deployment.py")
        run("lab.py", "seed")
        run("probe_admission.py")
        run("lab.py", "diagnostics")
        run("lab.py", "test")
        run("lab.py", "benchmark")
    finally:
        # Retain local state if destruction fails; rerun cloud.py destroy.
        run("cloud.py", "destroy")
        if (ROOT/".local/resources.json").exists():
            run("verify_cleanup.py")
    run("report.py")
    run("package_public.py")


if __name__ == "__main__":
    main()
