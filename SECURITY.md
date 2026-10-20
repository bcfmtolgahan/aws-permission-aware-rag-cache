# Security model

The protected boundary is accidental answer reuse across tenants, permissions, source versions and pipeline contracts. Valid clients cannot supply a subject, tenant, grants or test hooks to the public API. A still-valid JWT identifies a user; current DynamoDB records authorize that user.

The policy model is a set of document-scope strings, not a generic IAM/ABAC evaluator. Time-dependent, row-level or request-context policies require additional authoritative checks and a review of the sharing fingerprint. No conversational history or principal-specific prompt context enters this lab's model calls.

The authority, corpus publisher, application code, IAM administrator and cache writer are trusted. This is not a defense against an administrator or a compromised Lambda role forging an answer with plausible provenance. Cache entries are not cryptographically signed. Prompt injection and the model's factual correctness are separate risks; provenance records what the model saw, not proof that its answer is true. Semantic admission tests cover the fixture grammar, not arbitrary language.

Only scoped sources enter retrieval. Unauthorized chunks are not fetched and then filtered after generation. Every source passed to the model is a dependency, including a source the model did not cite. Cache hits are revalidated using current authority snapshots. The service sends `no-store`; it cannot force a malicious client to forget previous answers.

Valkey is private, TLS-authenticated and encrypted at rest. S3 blocks public access and denies non-TLS requests. Private endpoints connect Lambda to S3, DynamoDB and Bedrock. The cache password is generated and resides in sensitive local Terraform state and Lambda environment configuration. For production, use an appropriate managed secret or IAM/RBAC design and rotation; do not publish state or environment dumps. This lab shares one execution role between its two functions; it is not a production privilege-separation design. DynamoDB updates from that role are limited to `TEST#*` barrier records, but an IAM principal authorized to invoke the test function can act as any fixture subject.

The public API has JWT validation and rate throttling, but no per-tenant quotas, WAF, spend cap, request deduplication or single-flight generation. Cache stampedes, unbounded tenants, long conversation histories, personalized answers, multi-region authority, streaming delivery, document deletion obligations, and cross-deployment contract migration are outside this experiment.

Metrics exclude prompts, generated text, identities and tokens. Generated passwords, resource outputs, raw deployment logs and state stay under ignored local paths. Public reports contain synthetic questions and aggregate measurements. Inspect the public archive before publishing; pattern scans cannot guarantee the absence of every possible secret.

Cleanup deletes this run's resources only. Preserve local Terraform state until destruction and absence verification succeed.

Cloud operations require explicit local profile and expected-account configuration. They do not inherit an ambient AWS profile, credentials or endpoint override. The first remote request checks STS identity; a mismatch stops the command. Local tests and CI require no AWS access.

For a suspected vulnerability, use this repository's private GitHub vulnerability reporting feature when available. Do not place credentials, customer data or live account identifiers in a public issue.
