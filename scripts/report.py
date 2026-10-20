#!/usr/bin/env python3
"""Render checked-in evidence as a concise, reproducible results document."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def main():
    benchmark = json.loads((ROOT/"results/benchmark-summary.json").read_text())
    cloud = json.loads((ROOT/"results/cloud-tests.json").read_text())
    cleanup_file = ROOT/"results/cleanup.json"
    cleanup = json.loads(cleanup_file.read_text()) if cleanup_file.exists() else {
        "all_absent": False, "verified_at": "pending",
    }
    local = json.loads((ROOT/"results/local-tests.json").read_text())
    probe = json.loads((ROOT/"results/admission-probe.json").read_text())
    similarities = {r["question"]: r["similarity_to_atlas"] for r in probe["rows"]}
    para = similarities["How long does Atlas keep records?"]
    negation = similarities["What is NOT the retention period for Atlas?"]
    dated = similarities["What is the retention period for Atlas in 2024?"]
    arms = benchmark["arms"]
    lines = ["# Measured results", "", f"Recorded in a dedicated sandbox in `{cloud['region']}`. Local account and profile identifiers are omitted. All business records and questions were synthetic.", "",
             f"Local tests: **{local['passed']} passed**. AWS scenarios: **{sum(t['passed'] for t in cloud['tests'])}/{len(cloud['tests'])} passed**.", "",
             f"Cleanup: **{'all checked resources absent' if cleanup['all_absent'] else 'NOT COMPLETE'}**, verified at `{cleanup['verified_at']}`. See [cleanup.json](../results/cleanup.json).", "",
             "## Three-arm run", "", benchmark["workload"] + ". Each arm starts with an empty answer cache; the Lambda/integrations are warmed first.", "",
             "| Mode | Correct | Generate | Exact hit | Semantic hit | Median server ms | p95 server ms |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for mode, a in arms.items():
        lines.append(f"| {mode} | {a['correct']}/{a['requests']} | {a['generation_calls']} | {a['exact_hits']} | {a['semantic_hits']} | {a['median_server_ms']:.1f} | {a['p95_server_ms']:.1f} |")
    lines += ["", "| Mode | LLM input tokens | LLM output tokens | Query embedding tokens | DynamoDB RCU | S3 document reads | Cache errors |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for mode, a in arms.items():
        lines.append(f"| {mode} | {a['input_tokens']} | {a['output_tokens']} | {a['embedding_tokens']} | {a['authority_rcu']} | {a['document_reads']} | {a['cache_errors']} |")
    avoided = 100*(1-arms['semantic']['generation_calls']/arms['none']['generation_calls'])
    lines += ["", f"The semantic arm avoided **{avoided:.1f}%** of generation calls in this workload. This percentage does not describe arbitrary user traffic.", "",
              "`server_ms` measures the query service inside Lambda. It excludes Lambda initialization, the public API/JWT path and the operator's network. Raw `client_ms` includes IAM Lambda Invoke transport. The correctness checks exercise real Cognito/API Gateway separately; the benchmark invokes the IAM-only test Lambda. p95 uses nearest rank on 16 samples and is descriptive, not a production tail-latency estimate.", "",
              "The arms ran sequentially in fixed order, without parallel load or repeated randomized trials. Model infrastructure variability and workload selection can affect the numbers. Correctness means the expected synthetic retention duration appeared; this is not a general RAG evaluation. See [per-request measurements](../results/benchmark-requests.json).", "",
              "## Similarity diagnostic", "",
              f"With Titan V2 (256 dimensions), `How long does Atlas keep records?` scored {para:.3f} against the canonical retention question. Adding `NOT` to the canonical question scored {negation:.3f}, and adding `in 2024` scored {dated:.3f}. The admission grammar rejects both qualified questions despite their higher similarity.", "",
              "The initial threshold of 0.88 would miss the chosen paraphrase. We selected 0.75 after inspecting the fixture probe, before running the benchmark. This is transparent fixture tuning, not a held-out threshold evaluation. The small intent grammar itself could support a direct intent-key cache; it does not establish that vector search is required for this dataset. [Full diagnostic](../results/admission-probe.json).", "",
              "The first integration run also caught an unsupported `SORTBY` option in ElastiCache 8.2 `FT.SEARCH`. The final adapter uses native KNN ordering and `__embedding_score`. The [compatibility probe](../results/vector-compatibility.json) records the actual rejected command form and the working one; the cloud suite was rerun after this correction.", "",
              "## What the AWS tests establish", ""]
    for test in cloud['tests']:
        lines.append(f"- {'PASS' if test['passed'] else 'FAIL'}: {test['name']}.")
    if cleanup.get("execution_role_recovered_during_teardown"):
        lines += ["", "During this run's initial teardown, Terraform deleted the execution role before Lambda released its ENIs. We temporarily restored a role limited to deleting those owned interfaces, then removed it after reclamation. The final Terraform configuration retains the role until the application security group disappears and allows 30-minute subnet/group deletion. This ordering-only change passed Terraform validation; it was not followed by a second full stack deployment. The absence checks in cleanup.json cover the temporary role as well as the original resources."]
    lines += ["", "The authority outage case is an injected exception in an IAM-only handler. It is not a real DynamoDB outage. The S3 integrity case changes actual fixture object bytes and restores them. The concurrency cases coordinate an actual DynamoDB permission/manifest update with an in-flight Lambda using explicit barriers, not sleep-based guesses.", "",
              "The post-final-snapshot case deliberately returns the earlier authorized answer, then blocks its reuse by the revoked user. This documents the protocol boundary; it is not a failed test or a claim of atomic authorization through network delivery.", "",
              "## Cost interpretation", "",
              "Reported token counts, document reads and consumed capacity come from this run. They are not an AWS invoice. Query token totals exclude fixture ingestion, preflight, admission probing, warmup, security tests and debugging. The ingestion report records its embedding tokens separately. Node hours, endpoint hours, Lambda duration, logs, storage and request charges also matter.", "",
              "For a workload, token cost can be calculated as `(input_tokens × input_rate + output_tokens × output_rate + embedding_tokens × embedding_rate) / 1_000_000`, using current regional/model prices per million tokens. Add node-hours and the other service costs separately. A lower token count can still cost more overall at low request volume because the cache has a fixed hourly cost. No dollar savings or production break-even traffic is claimed here.", "",
              "All three arms retain the same authority protocol, so cache hits still pay for current authorization. This is an explicit correctness/performance tradeoff.", "",
              "## Reproduce", "", "Run the commands in [README](../README.md). `scripts/lab.py` is the fixture and benchmark source. `scripts/report.py` renders this file from the public JSON evidence. The AWS runtime and dependency versions are captured in [runtime.json](../results/runtime.json). Changing model behavior, engine/client versions, region, workload or execution order can change the measurements.", ""]
    (ROOT/"docs/results.md").write_text('\n'.join(lines))
    print("Rendered docs/results.md from measured evidence")


if __name__ == "__main__":
    main()
