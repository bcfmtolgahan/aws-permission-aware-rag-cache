#!/usr/bin/env python3
"""Render checked-in evidence as a concise, reproducible results document."""
from pathlib import Path
from html import escape
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
    table = ''.join(
        f"<tr><td>{escape(mode)}</td><td>{a['correct']}/{a['requests']}</td><td>{a['generation_calls']}</td>"
        f"<td>{a['exact_hits']}</td><td>{a['semantic_hits']}</td><td>{a['median_server_ms']:.1f}</td>"
        f"<td>{a['p95_server_ms']:.1f}</td></tr>" for mode, a in arms.items())
    core = (ROOT/"src/ragcache/core.py").read_text()
    code = core[core.index('        final = read_authority()'):core.index('        hook("after_final_authorize")')]
    html = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Permission-Aware RAG Cache · Lab Report</title>
<style>
body{margin:0;background:#f4f6f8;color:#232f3e;font:17px/1.65 system-ui,sans-serif}
main{max-width:1120px;margin:32px auto;padding:40px;background:white;border:1px solid #d8dee5}
img{width:100%;height:auto}h1{font-size:35px;line-height:1.2}h2{margin-top:44px;font-size:25px}
.eyebrow{color:#596575;font-size:13px;letter-spacing:.12em;text-transform:uppercase}.facts{display:flex;gap:18px;flex-wrap:wrap}
.facts div{padding:18px 24px;background:#f4f6f8;flex:1;min-width:190px}.facts b{display:block;font-size:30px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:15px}td,th{padding:12px;text-align:right;border-bottom:1px solid #d8dee5}td:first-child,th:first-child{text-align:left}
th{background:#f4f6f8}.scroll{overflow-x:auto}pre{background:#192330;color:#f3f6fa;padding:24px;overflow:auto;font:14px/1.65 ui-monospace,monospace}code{font-family:ui-monospace,monospace}
a{color:#146eb4}.note{border-left:3px solid #8c4fff;padding-left:20px;color:#465363}footer{border-top:1px solid #d8dee5;margin-top:40px;padding-top:18px;font-size:14px}
@media(max-width:720px){main{margin:0;padding:22px}h1{font-size:28px}}
</style><main><div class="eyebrow">AWS sandbox experiment · measured evidence</div>
<h1>Permission-Aware RAG Caching</h1>
<p>Reusing a Bedrock answer without reusing an old authorization decision.</p>
<img src="diagrams/cover.svg" alt="Amazon Bedrock, Amazon ElastiCache, Amazon DynamoDB and AWS Lambda">
<div class="facts"><div><b>__LOCAL__</b>local tests passed</div><div><b>__CLOUD__</b>AWS scenarios passed</div><div><b>__AVOIDED__%</b>fewer generation calls in this fixture</div></div>
<h2>Measured comparison</h2><p>16 sequential requests per arm. Two synthetic products, four admitted phrasings per product, repeated twice. Every arm performs current authorization checks.</p>
<div class="scroll"><table><thead><tr><th>Cache mode</th><th>Correct</th><th>Generate</th><th>Exact hit</th><th>Semantic hit</th><th>Median ms</th><th>p95 ms</th></tr></thead><tbody>__TABLE__</tbody></table></div>
<p class="note">Times cover the service code inside a warm Lambda, not end-to-end HTTP latency. The sample is small, sequential and deliberately repetitive. Token savings are not a dollar-cost claim.</p>
<h2>High similarity can still mean a different question</h2>
<p>The genuine paraphrase “How long does Atlas keep records?” scored <b>__PARA__</b>. Adding “NOT” to the original question scored <b>__NEGATION__</b>. A deterministic admission grammar rejects negation, dates and audience qualifiers before semantic reuse.</p>
<p>The 0.75 threshold was chosen from fixture diagnostics. This is not a general semantic-equivalence model; the tiny grammar could also support a direct intent-key cache.</p>
<h2>Architecture</h2><img src="diagrams/architecture.svg" alt="AWS deployment architecture">
<h2>The final authorization check</h2><p>Both generated and cached answers cross the same boundary. The following is extracted directly from the deployed application source:</p>
<pre><code>__CODE__</code></pre><img src="diagrams/authorization-flow.svg" alt="Authority snapshots around cache lookup or generation">
<p class="note">The final authority snapshot is not atomic with HTTP delivery. A revocation after that snapshot may race with the current response; the next request uses current permissions. The AWS test suite explicitly exercises this boundary.</p>
<h2>Cleanup and source</h2><p>__CLEANUP__ Verified at <code>__VERIFIED__</code>.</p>
<p><a href="results.md">Full results and limitations</a> · <a href="protocol.md">Protocol</a> · <a href="../README.md">Runbook</a> · <a href="../results/benchmark-requests.json">Per-request data</a> · <a href="../results/cleanup.json">Cleanup evidence</a></p>
<footer>The run used a dedicated sandbox. Local account and profile identifiers are omitted. Business records are synthetic. AWS Architecture Icons are unmodified official assets; this independent experiment is not an AWS reference implementation.</footer></main></html>'''
    for key, value in {"__LOCAL__": str(local['passed']), "__CLOUD__": str(sum(t['passed'] for t in cloud['tests'])),
                       "__AVOIDED__": f"{avoided:.2f}", "__TABLE__": table, "__CODE__": escape(code),
                       "__PARA__": f"{para:.3f}", "__NEGATION__": f"{negation:.3f}",
                       "__CLEANUP__": "All checked resources created by this run are absent." if cleanup['all_absent'] else "Cleanup is incomplete.",
                       "__VERIFIED__": escape(cleanup['verified_at'])}.items():
        html = html.replace(key, value)
    (ROOT/"docs/report.html").write_text(html)
    print("Rendered docs/results.md and docs/report.html from measured evidence")


if __name__ == "__main__":
    main()
