#!/usr/bin/env python3
"""Draw editable SVGs with unmodified official AWS Architecture Icons."""
import argparse
import base64
from html import escape
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "diagrams"
ICON_URL = "https://d1.awsstatic.com/onedam/marketing-channels/website/public/shared/architecture-icon-release/Icon-package_07312026.5846e92413caa21490223536cc97f1269e44fa92.zip"
FILES = {
    "lambda": "Arch_AWS-Lambda_64.svg", "bedrock": "Arch_Amazon-Bedrock_64.svg",
    "ddb": "Arch_Amazon-DynamoDB_64.svg", "cache": "Arch_Amazon-ElastiCache_64.svg",
    "s3": "Arch_Amazon-Simple-Storage-Service_64.svg", "cognito": "Arch_Amazon-Cognito_64.svg",
    "api": "Arch_Amazon-API-Gateway_64.svg", "logs": "Arch_Amazon-CloudWatch_64.svg",
    "cloud": "AWS-Cloud_32.svg", "region": "Region_32.svg", "vpc": "Virtual-private-cloud-VPC_32.svg",
    "client": "Res_Client_48_Light.svg",
}
ICONS = {}


def text(x, y, value, size=22, color="#232F3E", anchor="middle", weight=400):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" text-anchor="{anchor}" font-weight="{weight}">{escape(value)}</text>'


def icon(key, x, y, size=64):
    return f'<image href="{ICONS[key]}" x="{x}" y="{y}" width="{size}" height="{size}"/>'


def node(key, x, y, label, subtitle=None, size=64):
    return icon(key, x-size/2, y, size) + text(x, y+size+30, label) + (
        text(x, y+size+56, subtitle, 17, "#545B64") if subtitle else "")


def edge(path, dashed=False, color="#545B64"):
    return (f'<path d="{path}" stroke="white" stroke-width="8" fill="none"/>'
            f'<path d="{path}" stroke="{color}" stroke-width="2" fill="none" marker-end="url(#arrow)"'
            + (' stroke-dasharray="7 5"' if dashed else '') + '/>')


def box(x, y, w, h, label, key, color, dashed=False):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" stroke="{color}" stroke-width="1.5" fill="none"'
            + (' stroke-dasharray="7 5"' if dashed else '') + '/>' + icon(key, x, y, 32)
            + text(x+44, y+24, label, 20, color, "start"))


def write(name, title, width, height, parts):
    body = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">'
            f'<title>{escape(title)}</title><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            '<path d="M0 0 L10 5 L0 10" fill="#545B64"/></marker></defs>'
            '<style>text{font-family:Arial,Helvetica,sans-serif}</style>'
            f'<rect width="{width}" height="{height}" fill="white"/>' + ''.join(parts) + '</svg>')
    path = OUT / f"{name}.svg"
    path.write_text(body)
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", str(path), "-o", str(OUT/f"{name}.png")], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--icon-archive", type=Path)
    args = parser.parse_args()
    directory = ROOT / "docs" / "icons"
    directory.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    if args.icon_archive:
        with zipfile.ZipFile(args.icon_archive) as archive:
            for name in FILES.values():
                matches = [n for n in archive.namelist() if n.rsplit('/', 1)[-1] == name and not n.startswith('__MACOSX/')]
                assert len(matches) == 1, (name, matches)
                (directory/name).write_bytes(archive.read(matches[0]))
        (directory / "provenance.json").write_text(json.dumps({
            "source": "https://aws.amazon.com/architecture/icons/", "download": ICON_URL,
            "release": "2026-07-31", "modified": False, "files": FILES,
        }, indent=2) + '\n')
    for key, name in FILES.items():
        ICONS[key] = 'data:image/svg+xml;base64,' + base64.b64encode((directory/name).read_bytes()).decode()

    write("architecture", "Permission-aware RAG cache on AWS", 1600, 1000, [
        box(200, 35, 1360, 920, "AWS Cloud", "cloud", "#232F3E"),
        box(240, 100, 1045, 815, "Region · eu-central-1", "region", "#00A4A6", True),
        box(565, 185, 670, 430, "Amazon VPC · private subnets", "vpc", "#8C4FFF"),
        node("client", 95, 405, "Client"),
        node("cognito", 385, 200, "Amazon Cognito", "JWT"),
        node("api", 385, 405, "Amazon API Gateway", "HTTP API"),
        node("lambda", 720, 405, "AWS Lambda", "Authorize · retrieve · answer"),
        node("cache", 1065, 265, "Amazon ElastiCache", "Valkey 8.2 · TLS"),
        node("ddb", 675, 725, "Amazon DynamoDB", "Permissions + corpus manifest"),
        node("s3", 1050, 725, "Amazon S3", "Immutable source objects"),
        node("bedrock", 1420, 405, "Amazon Bedrock", "Titan V2 + Nova Micro"),
        text(1420, 552, "EU inference profile", 17, "#545B64"),
        edge("M130 437 H350"), edge("M385 405 V320", True),
        edge("M420 437 H682"),
        edge("M752 420 H875 V297 H1028"), text(877, 277, "Exact / vector", 17),
        edge("M688 455 H610 V675 H675 V720"), text(692, 656, "TransactGetItems × 2", 17, anchor="start"),
        edge("M752 460 H970 V757 H1013"), text(989, 650, "Gateway", 17, anchor="start"),
        edge("M752 440 H1382"), text(1150, 421, "PrivateLink", 17),
        text(385, 730, "IAM-only test runner", 18, weight=600),
        text(385, 760, "Separate Lambda entrypoint", 16, "#545B64"),
        text(385, 785, "Controlled race barriers", 16, "#545B64"),
        text(800, 983, "Authorization is rechecked before every answer. Cache expiry is storage hygiene.", 18),
    ])

    parts = [box(45, 35, 1510, 670, "AWS Cloud", "cloud", "#232F3E")]
    columns = [155, 455, 755, 1055, 1405]
    labels = [("lambda", "AWS Lambda"), ("ddb", "Amazon DynamoDB"), ("cache", "Amazon ElastiCache"),
              ("bedrock", "Amazon Bedrock"), ("ddb", "Amazon DynamoDB")]
    for x, (key, label) in zip(columns, labels):
        parts += [node(key, x, 110, label), f'<path d="M{x} 220 V610" stroke="#D5DBDB" stroke-dasharray="4 5"/>']
    parts += [edge("M155 270 H455"), text(305, 255, "1 · Snapshot", 18),
              edge("M155 345 H755"), text(560, 328, "2 · Scope + provenance", 18),
              edge("M155 420 H1055", True), text(860, 403, "3 · Generate on miss", 18),
              edge("M155 495 H1405"), text(1225, 478, "4 · Final snapshot", 18),
              edge("M1405 570 H155"), text(760, 552, "Valid snapshots: unchanged → answer     |     changed → 409", 20),
              text(800, 657, "Revocation after step 4 can race with delivery. The next request uses updated grants.", 20)]
    write("authorization-flow", "Authorization and cache read protocol", 1600, 750, parts)

    print("Wrote architecture and authorization flow (SVG; PNG when rsvg-convert is installed)")


if __name__ == "__main__":
    main()
