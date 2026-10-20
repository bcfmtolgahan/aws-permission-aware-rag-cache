#!/usr/bin/env python3
"""Package Linux Lambda dependencies from the locked uv environment."""
import importlib.metadata
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "build" / "package"
if target.exists():
    shutil.rmtree(target)
target.mkdir(parents=True)
shutil.copytree(ROOT / "src", target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
# valkey 6.x is pure Python. AWS supplies boto3 in the Python runtime.
for name in ["valkey"]:
    dist = importlib.metadata.distribution(name)
    for file in dist.files or []:
        if file.parts[0].startswith(".") or "__pycache__" in file.parts:
            continue
        src = Path(dist.locate_file(file))
        dst = target / file
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
with zipfile.ZipFile(ROOT / "build" / "lambda.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(target.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(target))
print("Built build/lambda.zip")
