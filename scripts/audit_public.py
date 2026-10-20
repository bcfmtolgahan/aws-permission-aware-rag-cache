#!/usr/bin/env python3
"""Check tracked files and optional Git history without printing matching values."""
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "aws-access-key": re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "jwt": re.compile(rb"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
    "github-token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "account-arn": re.compile(rb"arn:aws:[^\s\"']*:\d{12}:"),
    "live-api-url": re.compile(rb"https://[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com"),
    "home-directory": re.compile(rb"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
    "cloud-resource-id": re.compile(rb"\b(?:vpc|subnet|eni|sg|vpce)-[a-f0-9]{8,17}\b"),
}
EMAIL = re.compile(rb"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PRIVATE_DIRS = {".local", ".aws", ".venv", ".terraform", "build", "__pycache__"}


def findings(raw, known_values=()):
    found = [name for name, pattern in RULES.items() if pattern.search(raw)]
    for address in EMAIL.findall(raw):
        if not address.endswith((b"@users.noreply.github.com", b"@example.com", b"@example.invalid")):
            found.append("personal-email")
            break
    if any(value and value in raw for value in known_values):
        found.append("known-private-value")
    return found


def private_path(path):
    p = Path(path)
    return (bool(PRIVATE_DIRS & set(p.parts)) or p.name.startswith(".env")
            or p.suffix in {".pem", ".key", ".p12", ".pfx", ".tfplan"}
            or ".tfstate" in p.name or p.name.endswith((".tfvars", ".tfvars.json")))


def audit_paths(paths, known_values=()):
    issues = []
    for path in paths:
        if private_path(path.relative_to(ROOT)):
            issues.append({"file": str(path.relative_to(ROOT)), "rule": "private-path"})
        for rule in findings(path.read_bytes(), known_values):
            issues.append({"file": str(path.relative_to(ROOT)), "rule": rule})
    return issues


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    names = git("ls-files", "-z").decode().split("\0")
    paths = [ROOT / name for name in names if name]
    issues = audit_paths(paths)
    commits, blobs = 0, 0
    if args.history:
        revisions = git("rev-list", "--all").decode().splitlines()
        commits = len(revisions)
        for revision in revisions:
            metadata = git("show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", revision)
            for rule in findings(metadata):
                issues.append({"commit": revision[:12], "rule": rule})
        objects = git("rev-list", "--objects", "--all").decode().splitlines()
        for item in objects:
            oid, _, name = item.partition(" ")
            if git("cat-file", "-t", oid).strip() != b"blob":
                continue
            blobs += 1
            if name and private_path(name):
                issues.append({"file": name, "rule": "private-path-in-history"})
            for rule in findings(git("cat-file", "blob", oid)):
                issues.append({"object": oid[:12], "rule": rule})
    print(json.dumps({"files_checked": len(paths), "commits_checked": commits,
                      "historical_blobs_checked": blobs, "findings": issues}, indent=2))
    if issues:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
