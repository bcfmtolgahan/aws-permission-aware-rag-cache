"""Public JWT-authenticated handler and separate IAM-only test entrypoint."""

import base64
import json
import importlib.metadata
import os
import platform
import struct
import time

import boto3
from botocore.config import Config

from ragcache.aws_adapters import BedrockModel, DynamoAuthority, S3Documents, ValkeyCache
from ragcache.core import Changed, Denied, Service, Unavailable, digest

CONFIG = Config(connect_timeout=3, read_timeout=30, retries={"mode": "standard", "max_attempts": 2})
_service = None


def service():
    global _service
    if _service is None:
        model_id = os.environ["MODEL_ID"]
        threshold = float(os.getenv("SIMILARITY_THRESHOLD", "0.75"))
        pipeline = digest({"prompt": "authorized-retention-v1", "model": model_id,
                           "embedding": "amazon.titan-embed-text-v2:0", "dimensions": 256,
                           "contract": "bounded-retention-v1", "top_k": 8,
                           "threshold": threshold, "temperature": 0, "max_tokens": 160})
        _service = Service(
            DynamoAuthority(boto3.client("dynamodb", config=CONFIG), os.environ["TABLE_NAME"]),
            ValkeyCache(os.environ["CACHE_HOST"], os.environ["CACHE_PASSWORD"]),
            S3Documents(boto3.client("s3", config=CONFIG), os.environ["BUCKET_NAME"]),
            BedrockModel(boto3.client("bedrock-runtime", config=CONFIG), model_id),
            pipeline=pipeline, threshold=threshold)
    return _service


def response(status, body):
    return {"statusCode": status, "headers": {"content-type": "application/json",
            "cache-control": "no-store", "pragma": "no-cache"},
            "body": json.dumps(body, separators=(",", ":"))}


def run(subject, body, hook=lambda phase: None):
    try:
        svc = service()
        svc.authority.rcu = 0
        result = svc.query(subject, body.get("question"), body.get("mode", "semantic"), hook)
        result["metrics"]["authority_rcu"] = svc.authority.rcu
        # No queries, answers, identities, tokens or document contents in logs.
        print(json.dumps({"event": "query", "path": result["path"], "metrics": result["metrics"]}))
        return response(200, result)
    except Denied:
        return response(403, {"error": "access_denied"})
    except Changed:
        return response(409, {"error": "context_changed", "retryable": True})
    except Unavailable as exc:
        cause = exc.__cause__
        # Log only a fixed error category / SDK error code, never its message.
        print(json.dumps({"event": "dependency_error", "category": str(exc),
                          "code": getattr(cause, "response", {}).get("Error", {}).get("Code"),
                          "type": type(cause).__name__ if cause else None}))
        return response(503, {"error": "dependency_unavailable"})
    except (ValueError, TypeError):
        return response(400, {"error": "invalid_request"})


def lambda_handler(event, context):
    # API Gateway verifies signature, issuer and audience before invoking us.
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    subject = claims.get("sub")
    if not subject:
        return response(401, {"error": "authentication_required"})
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode()
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) - {"question", "mode"}:
            raise ValueError("Unsupported fields")
    except (ValueError, TypeError):
        return response(400, {"error": "invalid_request"})
    return run(subject, body)


def test_handler(event, context):
    # This function has NO API Gateway route or function URL. Only the sandbox
    # operator can invoke it with IAM. Never expose these probes to clients.
    svc = service()
    action = event.get("action", "query")
    if action == "setup":
        svc.cache.setup()
        info = svc.cache.client.info("server")
        return {"ok": True, "engine": info.get("valkey_version", info.get("redis_version", "unknown")),
                "python": platform.python_version(), "architecture": platform.machine(),
                "boto3": boto3.__version__, "valkey_client": importlib.metadata.version("valkey"),
                "model_id": os.environ["MODEL_ID"], "embedding_model": "amazon.titan-embed-text-v2:0",
                "embedding_dimensions": 256, "similarity_threshold": svc.threshold}
    if action == "clear":
        return {"deleted": svc.cache.clear()}
    if action == "stats":
        return {"keys": svc.cache.client.dbsize()}
    if action == "vector_diagnostics":
        snapshot = svc.authority.snapshot(event["subject"])
        scope = snapshot.scope_id(svc.pipeline)
        from ragcache.core import semantic_contract
        question = event["question"]
        contract = semantic_contract(question)
        vector, _ = svc.model.embed(question)
        prefix = f"(@scope:{{{scope}}} @contract:{{{digest(contract)}}})"
        results = {}
        for label, query, options in [
            ("explicit_sort", prefix+"=>[KNN 3 @embedding $vec AS distance]",
             ["SORTBY", "distance", "ASC", "RETURN", 2, "payload", "distance"]),
            ("native_knn", prefix+"=>[KNN 3 @embedding $vec]", []),
        ]:
            try:
                result = svc.cache.client.execute_command("FT.SEARCH", svc.cache.INDEX, query,
                    "PARAMS", 2, "vec", struct.pack(f"<{len(vector)}f", *vector), *options, "DIALECT", 2)
                results[label] = {"matches": result[0]}
            except Exception as exc:
                results[label] = {"error_type": type(exc).__name__, "message": str(exc)}
        return results
    if action != "query":
        raise ValueError("Unsupported test action")
    barrier = event.get("barrier")
    fault = event.get("fault")

    def hook(phase):
        if fault == "authority_unavailable" and phase == "before_final_authorize":
            raise Unavailable("Injected authority fault")
        if not barrier or phase != barrier["phase"]:
            return
        key = {"pk": {"S": f"TEST#{barrier['id']}"}}
        client, table = svc.authority.client, svc.authority.table
        client.update_item(TableName=table, Key=key,
            UpdateExpression="SET reached = :true", ExpressionAttributeValues={":true": {"BOOL": True}})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            item = client.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item", {})
            if item.get("released", {}).get("BOOL"):
                return
            time.sleep(0.1)
        raise Unavailable("Test barrier timed out")

    original_cache = svc.cache
    try:
        if fault == "cache_auth_rejected":
            # Exercise a real TLS connection and AUTH rejection, without
            # modifying the shared cache or disrupting other requests.
            svc.cache = ValkeyCache(os.environ["CACHE_HOST"], "intentionally-invalid-test-token")
        return run(event["subject"], event["request"], hook)
    finally:
        svc.cache = original_cache
