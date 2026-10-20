#!/usr/bin/env python3
"""One explicitly configured profile/account and one disposable Terraform stack."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
REGION = "eu-central-1"


def validate_settings(settings):
    profile = settings.get("profile")
    account = settings.get("account")
    if not isinstance(profile, str) or not profile.strip() or any(ord(c) < 32 for c in profile):
        raise SystemExit("An explicit non-empty AWS profile is required")
    if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
        raise SystemExit("An expected 12-digit sandbox account ID is required")
    if settings.get("region") != REGION:
        raise SystemExit(f"This lab is restricted to {REGION}")
    return settings


def settings():
    path = LOCAL / "account.json"
    if not path.exists():
        raise SystemExit("Run cloud.py configure --profile YOUR_PROFILE --account-id YOUR_ACCOUNT first")
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        raise SystemExit("Cannot read local sandbox configuration") from None
    if not isinstance(config, dict):
        raise SystemExit("Invalid local sandbox configuration")
    return validate_settings(config)


def configure(profile, account):
    config = validate_settings({"profile": profile, "account": account, "region": REGION})
    LOCAL.mkdir(exist_ok=True, mode=0o700)
    path = LOCAL / "account.json"
    if path.exists() and settings() != config:
        raise SystemExit("Sandbox identity is already pinned; use a separate checkout for another sandbox")
    path.write_text(json.dumps(config) + "\n")
    path.chmod(0o600)
    print("Sandbox profile and expected account saved in ignored .local/account.json; no AWS calls made")


def clean_environment():
    config = settings()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "TF_"))}
    env.update(AWS_PROFILE=config["profile"], AWS_DEFAULT_REGION=REGION, AWS_REGION=REGION, AWS_PAGER="",
               AWS_EC2_METADATA_DISABLED="true", TF_IN_AUTOMATION="true")
    return env


def guarded_session():
    config = settings()
    for key in list(os.environ):
        if key.startswith(("AWS_", "TF_")):
            del os.environ[key]
    os.environ.update(AWS_PROFILE=config["profile"], AWS_DEFAULT_REGION=REGION, AWS_EC2_METADATA_DISABLED="true")
    import boto3
    session = boto3.Session(profile_name=config["profile"], region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    if config["account"] != account:
        raise SystemExit("AWS identity does not match the configured sandbox account; refusing to continue")
    return session, account


def terraform(*args):
    log = LOCAL / f"terraform-{args[0]}.log"
    with log.open("w") as output:
        result = subprocess.run(["terraform", args[0], "-no-color", *args[1:]] if args[0] in {"plan", "apply", "destroy"}
                                else ["terraform", *args], cwd=ROOT / "infra", env=clean_environment(),
                                stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        account = json.loads((LOCAL / "account.json").read_text())["account"]
        print(log.read_text()[-6000:].replace(account, "[ACCOUNT]"))
        raise SystemExit(result.returncode)
    print(f"terraform {args[0]} completed; raw output remains in ignored .local/", flush=True)


def capture_outputs():
    p = subprocess.run(["terraform", "output", "-json", "resources"], cwd=ROOT / "infra",
                       env=clean_environment(), check=True, capture_output=True, text=True)
    path = LOCAL / "resources.json"
    path.write_text(p.stdout)
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["configure", "preflight", "init", "validate", "plan", "review", "apply", "outputs", "destroy"])
    parser.add_argument("--profile")
    parser.add_argument("--account-id")
    args = parser.parse_args()
    if args.action == "configure":
        configure(args.profile, args.account_id)
        return
    if args.profile is not None or args.account_id is not None:
        parser.error("--profile and --account-id are only accepted by configure")
    session, account = guarded_session()
    config = settings()
    print(json.dumps({"region": REGION, "configured_identity_verified": True}), flush=True)
    if args.action == "preflight":
        versions = session.client("elasticache").describe_cache_engine_versions(Engine="valkey")
        print(json.dumps({"valkey_versions": sorted({x["EngineVersion"] for x in versions["CacheEngineVersions"]})}))
        br = session.client("bedrock-runtime")
        e = br.invoke_model(modelId="amazon.titan-embed-text-v2:0",
            body=json.dumps({"inputText": "Synthetic sandbox connectivity check", "dimensions": 256}),
            contentType="application/json")
        print(json.dumps({"embedding_dimensions": len(json.loads(e["body"].read())["embedding"])}))
        g = br.converse(modelId="eu.amazon.nova-micro-v1:0",
            messages=[{"role": "user", "content": [{"text": "Reply with OK."}]}],
            inferenceConfig={"maxTokens": 8, "temperature": 0})
        print(json.dumps({"generation_available": bool(g["output"])}))
        return
    variables = ROOT / "infra" / "sandbox.auto.tfvars.json"
    if not variables.exists():
        variables.write_text(json.dumps({"expected_account_id": account, "aws_profile": config["profile"],
                                         "run_id": secrets.token_hex(4),
                                         "region": REGION}, indent=2))
        variables.chmod(0o600)
    values = json.loads(variables.read_text())
    if values["expected_account_id"] != account or values.get("aws_profile") != config["profile"]:
        raise SystemExit("Terraform account/profile pin does not match local configuration")
    if args.action == "init":
        terraform("init", "-input=false")
    elif args.action == "validate":
        terraform("validate", "-no-color")
    elif args.action == "plan":
        terraform("plan", "-input=false", "-out=sandbox.tfplan")
    elif args.action == "review":
        output = subprocess.run(["terraform", "show", "-json", "sandbox.tfplan"], cwd=ROOT / "infra",
                                env=clean_environment(), check=True, capture_output=True, text=True)
        changes = json.loads(output.stdout)["resource_changes"]
        print(json.dumps([{ "resource": x["address"], "actions": x["change"]["actions"]} for x in changes], indent=2))
    elif args.action == "apply":
        stamp = LOCAL / "lifecycle.json"
        state_path = ROOT / "infra/terraform.tfstate"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        has_resources = any(r.get("mode") == "managed" and r.get("instances") for r in state.get("resources", []))
        if not stamp.exists() or not has_resources:
            stamp.write_text(json.dumps({"started_at": datetime.now(timezone.utc).isoformat()}))
            (LOCAL / "resources.json").unlink(missing_ok=True)
        terraform("apply", "-input=false", "sandbox.tfplan")
        capture_outputs()
    elif args.action == "outputs":
        capture_outputs()
    elif args.action == "destroy":
        if not (LOCAL / "resources.json").exists():
            try:
                capture_outputs()
            except subprocess.CalledProcessError:
                pass  # Partial applies still have Terraform state to destroy.
        terraform("destroy", "-input=false", "-auto-approve")


if __name__ == "__main__":
    main()
