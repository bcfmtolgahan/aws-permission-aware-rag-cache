#!/usr/bin/env python3
"""Run offline checks and retain a small public test-count report."""
from datetime import datetime, timezone
import json
import platform
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def main():
    (ROOT/".local").mkdir(exist_ok=True)
    subprocess.run([sys.executable, "-m", "ruff", "check", "."], cwd=ROOT, check=True)
    subprocess.run(["terraform", "fmt", "-check", "infra"], cwd=ROOT, check=True)
    subprocess.run([sys.executable, "-m", "pytest", "-q", "--junitxml=.local/pytest.xml"], cwd=ROOT, check=True)
    cases = ET.parse(ROOT/".local/pytest.xml").findall(".//testcase")
    assert all(case.find("failure") is None and case.find("error") is None for case in cases)
    report = {"passed": sum(case.find("skipped") is None for case in cases), "failed": 0, "python": platform.python_version(),
              "generated_at": datetime.now(timezone.utc).isoformat(), "ruff": "passed", "terraform_fmt": "passed"}
    (ROOT/"results").mkdir(exist_ok=True)
    (ROOT/"results/local-tests.json").write_text(json.dumps(report, indent=2)+"\n")


if __name__ == "__main__":
    main()
