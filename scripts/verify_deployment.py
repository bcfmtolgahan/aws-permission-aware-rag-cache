#!/usr/bin/env python3
"""Ensure the deployed functions contain the exact built application bytes."""
import base64
import hashlib
import json
from cloud import LOCAL, ROOT, guarded_session


def main():
    session, _ = guarded_session()
    resources = json.loads((LOCAL/"resources.json").read_text())
    run = json.loads((ROOT/"infra/sandbox.auto.tfvars.json").read_text())["run_id"]
    assert resources["name"] == f"parc-{run}"
    artifact = hashlib.sha256((ROOT/"build/lambda.zip").read_bytes()).digest()
    results = []
    for key in ["api_function", "test_function"]:
        deployed = session.client("lambda").get_function_configuration(FunctionName=resources[key])
        assert deployed["CodeSha256"] == base64.b64encode(artifact).decode(), "Deployed code differs from local bundle"
        assert deployed["State"] == "Active" and deployed["LastUpdateStatus"] == "Successful"
        results.append({"function": key, "code_matches_bundle": True, "runtime": deployed["Runtime"],
                        "architecture": deployed["Architectures"], "memory_mb": deployed["MemorySize"],
                        "similarity_threshold": deployed["Environment"]["Variables"]["SIMILARITY_THRESHOLD"]})
    report = {"artifact_sha256": artifact.hex(), "functions": results,
              "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted((ROOT/"src").rglob("*.py"))}}
    (ROOT/"results").mkdir(exist_ok=True)
    (ROOT/"results/deployment.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps({"deployed_bundle_verified": True, "functions": len(results)}))


if __name__ == "__main__":
    main()
