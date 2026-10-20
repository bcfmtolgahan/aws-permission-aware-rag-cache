# Measured results

Recorded in a dedicated sandbox in `eu-central-1`. Local account and profile identifiers are omitted. All business records and questions were synthetic.

Local tests: **44 passed**. AWS scenarios: **23/23 passed**.

Cleanup: **all checked resources absent**, verified at `2026-10-19T14:54:43.036816+00:00`. See [cleanup.json](../results/cleanup.json).

## Three-arm run

16 sequential requests per arm; two products; four admitted paraphrases each, repeated twice. Each arm starts with an empty answer cache; the Lambda/integrations are warmed first.

| Mode | Correct | Generate | Exact hit | Semantic hit | Median server ms | p95 server ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| none | 16/16 | 16 | 0 | 0 | 540.7 | 656.2 |
| exact | 16/16 | 8 | 8 | 0 | 258.9 | 619.8 |
| semantic | 16/16 | 3 | 3 | 10 | 85.3 | 577.9 |

| Mode | LLM input tokens | LLM output tokens | Query embedding tokens | DynamoDB RCU | S3 document reads | Cache errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| none | 6400 | 188 | 144 | 160.0 | 64 | 0 |
| exact | 3200 | 94 | 72 | 160.0 | 32 | 0 |
| semantic | 1200 | 37 | 117 | 160.0 | 12 | 0 |

The semantic arm avoided **81.2%** of generation calls in this workload. This percentage does not describe arbitrary user traffic.

`server_ms` measures the query service inside Lambda. It excludes Lambda initialization, the public API/JWT path and the operator's network. Raw `client_ms` includes IAM Lambda Invoke transport. The correctness checks exercise real Cognito/API Gateway separately; the benchmark invokes the IAM-only test Lambda. p95 uses nearest rank on 16 samples and is descriptive, not a production tail-latency estimate.

The arms ran sequentially in fixed order, without parallel load or repeated randomized trials. Model infrastructure variability and workload selection can affect the numbers. Correctness means the expected synthetic retention duration appeared; this is not a general RAG evaluation. See [per-request measurements](../results/benchmark-requests.json).

## Similarity diagnostic

With Titan V2 (256 dimensions), `How long does Atlas keep records?` scored 0.780 against the canonical retention question. Adding `NOT` to the canonical question scored 0.960, and adding `in 2024` scored 0.893. The admission grammar rejects both qualified questions despite their higher similarity.

The initial threshold of 0.88 would miss the chosen paraphrase. We selected 0.75 after inspecting the fixture probe, before running the benchmark. This is transparent fixture tuning, not a held-out threshold evaluation. The small intent grammar itself could support a direct intent-key cache; it does not establish that vector search is required for this dataset. [Full diagnostic](../results/admission-probe.json).

The first integration run also caught an unsupported `SORTBY` option in ElastiCache 8.2 `FT.SEARCH`. The final adapter uses native KNN ordering and `__embedding_score`. The [compatibility probe](../results/vector-compatibility.json) records the actual rejected command form and the working one; the cloud suite was rerun after this correction.

## What the AWS tests establish

- PASS: public API requires authentication.
- PASS: modified JWT signature is rejected.
- PASS: client cannot select tenant or inject test hooks.
- PASS: authenticated HTTP API generates and disables downstream caching.
- PASS: exact cache hit avoids generation.
- PASS: admitted paraphrase uses real Valkey vector search.
- PASS: same-tenant equivalent entitlements can share an answer.
- PASS: same-tenant restricted user cannot reuse internal answer.
- PASS: different tenant cannot reuse identical-question answer.
- PASS: revocation works with old JWT and old cache entries still present.
- PASS: new grant changes the authorized answer view.
- PASS: disabled principal cannot read a warm cache.
- PASS: new document invalidates answers whose old sources did not change.
- PASS: source replacement invalidates old answer.
- PASS: controlled revocation race at after_lookup.
- PASS: controlled revocation race at after_generate.
- PASS: controlled revocation race at before_final_authorize.
- PASS: controlled corpus publication race rejects obsolete generation.
- PASS: post-authorization boundary and delayed cache fill are explicit.
- PASS: authority fault fails closed in all cache modes.
- PASS: Valkey authentication rejection falls back to authorized generation.
- PASS: date negation and audience qualifiers bypass semantic reuse.
- PASS: changed S3 object bytes fail the authoritative source hash.

During this run's initial teardown, Terraform deleted the execution role before Lambda released its ENIs. We temporarily restored a role limited to deleting those owned interfaces, then removed it after reclamation. The final Terraform configuration retains the role until the application security group disappears and allows 30-minute subnet/group deletion. This ordering-only change passed Terraform validation; it was not followed by a second full stack deployment. The absence checks in cleanup.json cover the temporary role as well as the original resources.

The authority outage case is an injected exception in an IAM-only handler. It is not a real DynamoDB outage. The S3 integrity case changes actual fixture object bytes and restores them. The concurrency cases coordinate an actual DynamoDB permission/manifest update with an in-flight Lambda using explicit barriers, not sleep-based guesses.

The post-final-snapshot case deliberately returns the earlier authorized answer, then blocks its reuse by the revoked user. This documents the protocol boundary; it is not a failed test or a claim of atomic authorization through network delivery.

## Cost interpretation

Reported token counts, document reads and consumed capacity come from this run. They are not an AWS invoice. Query token totals exclude fixture ingestion, preflight, admission probing, warmup, security tests and debugging. The ingestion report records its embedding tokens separately. Node hours, endpoint hours, Lambda duration, logs, storage and request charges also matter.

For a workload, token cost can be calculated as `(input_tokens × input_rate + output_tokens × output_rate + embedding_tokens × embedding_rate) / 1_000_000`, using current regional/model prices per million tokens. Add node-hours and the other service costs separately. A lower token count can still cost more overall at low request volume because the cache has a fixed hourly cost. No dollar savings or production break-even traffic is claimed here.

All three arms retain the same authority protocol, so cache hits still pay for current authorization. This is an explicit correctness/performance tradeoff.

## Reproduce

Run the commands in [README](../README.md). `scripts/lab.py` is the fixture and benchmark source. `scripts/report.py` renders this file from the public JSON evidence. The AWS runtime and dependency versions are captured in [runtime.json](../results/runtime.json). Changing model behavior, engine/client versions, region, workload or execution order can change the measurements.
