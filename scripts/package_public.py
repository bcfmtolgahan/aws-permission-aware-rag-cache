#!/usr/bin/env python3
"""Create an allowlisted source archive; exclude all live cloud state."""
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP = ["README.md", "ARTICLE.html", "SECURITY.md", "LICENSE", "NOTICE", ".gitignore",
       ".python-version", "pyproject.toml", "uv.lock"]
DIRECTORIES = ["src", "tests", "scripts", "docs", "results", ".github"]
PATTERNS = [
    re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
    re.compile(rb"arn:aws:[^\s\"']*:\d{12}:"),
    re.compile(rb"https://[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com"),
]


def public_files():
    paths = [ROOT/name for name in TOP]
    paths += sorted((ROOT/"infra").glob("*.tf"))
    paths += [ROOT/"infra/.terraform.lock.hcl"]
    for directory in DIRECTORIES:
        paths += sorted(p for p in (ROOT/directory).rglob("*") if p.is_file()
                        and "__pycache__" not in p.parts and p.suffix != ".pyc")
    return [p for p in paths if p.exists()]


def main():
    known_secrets = []
    pin = ROOT/".local/account.json"
    if pin.exists():
        config = json.loads(pin.read_text())
        known_secrets += [config["account"].encode(), config["profile"].encode()]
    users = ROOT/".local/users.json"
    if users.exists():
        for user in json.loads(users.read_text()).values():
            known_secrets += [user["password"].encode(), user["sub"].encode()]
    resources = ROOT/".local/resources.json"
    if resources.exists():
        r = json.loads(resources.read_text())
        for key, value in r.items():
            if key in {"region", "model_id"}:
                continue
            known_secrets += [v.encode() for v in (value if isinstance(value, list) else [value])
                              if isinstance(v, str) and len(v) >= 6]
    paths = public_files()
    from audit_public import audit_paths
    issues = audit_paths(paths, known_secrets)
    if issues:
        print(json.dumps({"findings": issues}, indent=2))
        raise SystemExit("Public-file audit failed; matching values are not printed")
    for path in paths:
        raw = path.read_bytes()
        if any(secret in raw for secret in known_secrets) or any(p.search(raw) for p in PATTERNS):
            raise SystemExit(f"Private value or secret-like content in {path.relative_to(ROOT)}; refusing archive")
    output = ROOT/"build/permission-aware-rag-cache-source.zip"
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            info = zipfile.ZipInfo(str(path.relative_to(ROOT)), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    report = {"files": len(paths), "archive": str(output.relative_to(ROOT)),
              "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "excluded": [".local", "Terraform state/plans/auto variables", ".venv", "build", ".git"],
              "secret_pattern_scan_passed": True}
    (ROOT/".local").mkdir(exist_ok=True)
    (ROOT/".local/public-package.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
