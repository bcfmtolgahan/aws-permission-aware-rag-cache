#!/usr/bin/env python3
"""Seed, exercise, benchmark and verify cleanup of the owned AWS lab."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import argparse
import base64
import hashlib
import json
import secrets
import statistics
import sys
import time
import urllib.error
import urllib.request

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config

from cloud import LOCAL, ROOT, guarded_session

sys.path.insert(0, str(ROOT / "src"))
from ragcache.aws_adapters import BedrockModel  # noqa: E402

SER = TypeSerializer()
DES = TypeDeserializer()
Q = "What is the retention period for Atlas?"


def encode(item):
    return {k: SER.serialize(v) for k, v in item.items()}


def decode(item):
    return {k: DES.deserialize(v) for k, v in item.items()}


def save_report(name, data):
    directory = ROOT / "results"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(json.dumps(data, indent=2, default=str) + "\n")


class Lab:
    def __init__(self):
        self.session, self.account = guarded_session()
        self.r = json.loads((LOCAL / "resources.json").read_text())
        run = json.loads((ROOT / "infra" / "sandbox.auto.tfvars.json").read_text())["run_id"]
        if self.r["name"] != f"parc-{run}" or self.r["region"] != "eu-central-1":
            raise SystemExit("Unexpected stack identity")
        self.ddb = self.session.client("dynamodb")
        self.s3 = self.session.client("s3")
        self.cognito = self.session.client("cognito-idp")
        self.lam = self.session.client("lambda", config=Config(read_timeout=90, retries={"max_attempts": 0}))
        self.model = BedrockModel(self.session.client("bedrock-runtime"), self.r["model_id"])
        self.users_path = LOCAL / "users.json"
        self.pool_pin = LOCAL / "users-pool.json"
        same_pool = self.pool_pin.exists() and json.loads(self.pool_pin.read_text())["pool"] == self.r["pool_id"]
        self.users = json.loads(self.users_path.read_text()) if self.users_path.exists() and same_pool else {}

    def get(self, pk):
        item = self.ddb.get_item(TableName=self.r["table"], Key={"pk": {"S": pk}},
                                 ConsistentRead=True).get("Item")
        return decode(item) if item else None

    def put(self, item):
        self.ddb.put_item(TableName=self.r["table"], Item=encode(item))

    def invoke(self, event):
        start = time.perf_counter()
        raw = self.lam.invoke(FunctionName=self.r["test_function"], InvocationType="RequestResponse",
                              Payload=json.dumps(event).encode())
        data = json.loads(raw["Payload"].read())
        if raw.get("FunctionError"):
            raise RuntimeError(json.dumps(data))
        if "body" in data:
            data["body"] = json.loads(data["body"])
        data["client_ms"] = round((time.perf_counter() - start) * 1000, 3)
        return data

    def query(self, user="alice", question=Q, mode="semantic", **kwargs):
        return self.invoke({"action": "query", "subject": self.users[user]["sub"],
                            "request": {"question": question, "mode": mode}, **kwargs})

    def token(self, user):
        result = self.cognito.admin_initiate_auth(UserPoolId=self.r["pool_id"], ClientId=self.r["client_id"],
            AuthFlow="ADMIN_USER_PASSWORD_AUTH", AuthParameters={
                "USERNAME": user, "PASSWORD": self.users[user]["password"]})
        return result["AuthenticationResult"]["IdToken"]

    def http(self, body, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.r["api_url"] + "/query", data=json.dumps(body).encode(),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return {"statusCode": response.status, "body": json.loads(response.read()),
                        "cache_control": response.headers.get("Cache-Control")}
        except urllib.error.HTTPError as exc:
            return {"statusCode": exc.code, "body": json.loads(exc.read())}

    def grant(self, user, grants, enabled=True):
        pk = f"USER#{self.users[user]['sub']}"
        old = self.get(pk)
        self.ddb.update_item(TableName=self.r["table"], Key={"pk": {"S": pk}},
            UpdateExpression="SET grants=:g, enabled=:e, #v=:next",
            ConditionExpression="#v=:old", ExpressionAttributeNames={"#v": "version"},
            ExpressionAttributeValues=encode({":g": grants, ":e": enabled,
                                               ":old": old["version"], ":next": old["version"]+1}))

    def publish(self, tenant, documents):
        pk = f"TENANT#{tenant}"
        old = self.get(pk)
        version = int(old["version"]) + 1 if old else 1
        item = {"pk": pk, "tenant": tenant, "version": version, "documents": documents}
        # All immutable S3 objects already exist. Publishing the manifest is a
        # single conditional authority write; partial uploads are never active.
        if old:
            self.ddb.put_item(TableName=self.r["table"], Item=encode(item),
                ConditionExpression="#v=:old", ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues=encode({":old": old["version"]}))
        else:
            self.ddb.put_item(TableName=self.r["table"], Item=encode(item),
                ConditionExpression="attribute_not_exists(pk)")
        return version

    def document(self, tenant, doc_id, scope, topic, days, priority=0):
        text = (f"{topic.title()} retention policy for the synthetic {tenant} organization. "
                f"The {topic.title()} data retention period is {days} days. "
                f"Records are kept for {days} days. This is the {scope} policy. "
                "A higher-priority policy overrides a lower-priority policy for the same product.")
        vector, tokens = self.model.embed(text)
        raw = json.dumps({"text": text, "embedding": vector}, sort_keys=True).encode()
        checksum = hashlib.sha256(raw).hexdigest()
        key = f"documents/{tenant}/{doc_id}/{checksum}.json"
        self.s3.put_object(Bucket=self.r["bucket"], Key=key, Body=raw, ContentType="application/json")
        return {"doc_id": doc_id, "version": checksum[:16], "scope": scope, "topic": topic,
                "key": key, "sha256": checksum, "priority": priority}, tokens

    def seed(self):
        for user, tenant, grants in [
            ("alice", "acme", ["public", "internal"]), ("bob", "acme", ["public"]),
            ("carol", "acme", ["public", "internal"]), ("dana", "globex", ["public", "internal"]),
        ]:
            if user not in self.users:
                try:
                    created = self.cognito.admin_create_user(UserPoolId=self.r["pool_id"], Username=user,
                                                             MessageAction="SUPPRESS")
                    attributes = created["User"]["Attributes"]
                except self.cognito.exceptions.UsernameExistsException:
                    # Recover an interrupted seed in this exact owned pool.
                    attributes = self.cognito.admin_get_user(UserPoolId=self.r["pool_id"], Username=user)["UserAttributes"]
                password = "Aa1!" + secrets.token_urlsafe(28)
                self.cognito.admin_set_user_password(UserPoolId=self.r["pool_id"], Username=user,
                                                     Password=password, Permanent=True)
                sub = next(x["Value"] for x in attributes if x["Name"] == "sub")
                self.users[user] = {"sub": sub, "password": password, "tenant": tenant}
                self.users_path.write_text(json.dumps(self.users))
                self.users_path.chmod(0o600)
                self.pool_pin.write_text(json.dumps({"pool": self.r["pool_id"]}))
                self.pool_pin.chmod(0o600)
            prior = self.get(f"USER#{self.users[user]['sub']}")
            self.put({"pk": f"USER#{self.users[user]['sub']}", "tenant": tenant, "enabled": True,
                      "version": int(prior["version"]) + 1 if prior else 1, "grants": grants})
        total_tokens, baselines = 0, {}
        for tenant, values in {"acme": (30, 90, 60, 120), "globex": (14, 180, 21, 240)}.items():
            docs = []
            for (topic, scope), days in zip(
                    [("atlas", "public"), ("atlas", "internal"), ("boreal", "public"), ("boreal", "internal")], values):
                doc, tokens = self.document(tenant, f"{topic}-{scope}", scope, topic, days,
                                            10 if scope == "internal" else 0)
                total_tokens += tokens
                docs.append(doc)
            self.publish(tenant, docs)
            baselines[tenant] = docs
        (LOCAL / "baseline-documents.json").write_text(json.dumps(baselines))
        print(json.dumps({"seeded": True, "tenants": 2, "users": 4, "documents": 8,
                          "ingestion_embedding_tokens": total_tokens}), flush=True)
        runtime = self.invoke({"action": "setup"})
        print(json.dumps(runtime), flush=True)
        save_report("runtime.json", runtime)
        save_report("ingestion.json", {"documents": 8, "embedding_tokens": total_tokens})

    def restore(self):
        for tenant, documents in json.loads((LOCAL / "baseline-documents.json").read_text()).items():
            self.publish(tenant, documents)
        for user in ["alice", "carol", "dana"]:
            self.grant(user, ["public", "internal"])
        self.grant("bob", ["public"])

    def race(self, phase, mutation, warm=False):
        self.restore()
        self.invoke({"action": "clear"})
        if warm:
            result = self.query()
            assert result["statusCode"] == 200, result
        barrier_id = secrets.token_hex(8)
        pk = f"TEST#{barrier_id}"
        self.put({"pk": pk, "reached": False, "released": False})
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.query, barrier={"id": barrier_id, "phase": phase})
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if self.get(pk).get("reached"):
                    break
                if future.done():
                    raise AssertionError(f"Invocation ended before barrier: {future.result()}")
                time.sleep(0.1)
            else:
                raise AssertionError("Barrier was not reached")
            try:
                mutation()
            finally:
                self.ddb.update_item(TableName=self.r["table"], Key={"pk": {"S": pk}},
                    UpdateExpression="SET released=:true", ExpressionAttributeValues={":true": {"BOOL": True}})
            return future.result(timeout=60)

    def test(self):
        results = []
        def check(name, fn):
            try:
                details = fn() or {}
                results.append({"name": name, "passed": True, **details})
                print(f"PASS {name}", flush=True)
            except Exception as exc:
                results.append({"name": name, "passed": False, "error": str(exc)})
                print(f"FAIL {name}: {exc}", flush=True)
            finally:
                save_report("cloud-tests.json", {"generated_at": datetime.now(timezone.utc).isoformat(),
                    "execution_scope": "dedicated sandbox", "region": "eu-central-1", "tests": results})

        def expect(result, code=200, days=None, path=None):
            assert result["statusCode"] == code, result
            body = result["body"]
            if days is not None:
                assert f"{days} days" in body["answer"].lower(), body
            if path:
                assert body["path"] == path, body
            return body

        self.restore()
        self.invoke({"action": "clear"})
        alice_token, bob_token = self.token("alice"), self.token("bob")

        def no_token():
            expect(self.http({"question": Q}), 401)
        check("public API requires authentication", no_token)

        def forged_token():
            parts = alice_token.split(".")
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            payload["sub"] = self.users["dana"]["sub"]
            parts[1] = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            expect(self.http({"question": Q}, ".".join(parts)), 401)
        check("modified JWT signature is rejected", forged_token)

        def injected_fields():
            expect(self.http({"question": Q, "tenant": "globex", "barrier": {}}, alice_token), 400)
        check("client cannot select tenant or inject test hooks", injected_fields)

        def api_generation():
            result = self.http({"question": Q}, alice_token)
            expect(result, days=90, path="generation")
            assert result["cache_control"] == "no-store"
        check("authenticated HTTP API generates and disables downstream caching", api_generation)

        def exact():
            body = expect(self.query(), days=90, path="exact_hit")
            assert body["metrics"]["model_calls"] == 0
        check("exact cache hit avoids generation", exact)

        def semantic():
            body = expect(self.query(question="How long does Atlas keep records?"), days=90, path="semantic_hit")
            assert body["metrics"]["model_calls"] == 0
        check("admitted paraphrase uses real Valkey vector search", semantic)

        def cohort():
            expect(self.query("carol"), days=90, path="exact_hit")
        check("same-tenant equivalent entitlements can share an answer", cohort)

        def restricted():
            body = expect(self.http({"question": Q}, bob_token), days=30)
            assert all("internal" not in d["doc_id"] for d in body["dependencies"])
        check("same-tenant restricted user cannot reuse internal answer", restricted)

        def other_tenant():
            expect(self.query("dana"), days=180, path="generation")
        check("different tenant cannot reuse identical-question answer", other_tenant)

        def revoke_warm():
            before = self.invoke({"action": "stats"})["keys"]
            self.grant("alice", ["public"])
            body = expect(self.http({"question": Q}, alice_token), days=30)
            assert all("internal" not in d["doc_id"] for d in body["dependencies"])
            assert self.invoke({"action": "stats"})["keys"] >= before
        check("revocation works with old JWT and old cache entries still present", revoke_warm)

        def grant_again():
            self.grant("alice", ["public", "internal"])
            expect(self.query(), days=90)
        check("new grant changes the authorized answer view", grant_again)

        def disabled():
            self.grant("alice", ["public", "internal"], enabled=False)
            expect(self.query(), 403)
            self.grant("alice", ["public", "internal"])
        check("disabled principal cannot read a warm cache", disabled)

        def addition():
            original = self.get("TENANT#acme")["documents"]
            doc, _ = self.document("acme", "atlas-amendment", "public", "atlas", 45, 20)
            self.publish("acme", original + [doc])
            expect(self.query("bob"), days=45, path="generation")
            self.restore()
        check("new document invalidates answers whose old sources did not change", addition)

        def replacement():
            docs = self.get("TENANT#acme")["documents"]
            new, _ = self.document("acme", "atlas-public", "public", "atlas", 60, 0)
            self.publish("acme", [new if d["doc_id"] == "atlas-public" else d for d in docs])
            expect(self.query("bob"), days=60, path="generation")
            self.restore()
        check("source replacement invalidates old answer", replacement)

        for phase, warm in [("after_lookup", True), ("after_generate", False), ("before_final_authorize", False)]:
            def concurrent_revoke(phase=phase, warm=warm):
                result = self.race(phase, lambda: self.grant("alice", ["public"]), warm=warm)
                expect(result, 409)
                assert "answer" not in result["body"]
            check(f"controlled revocation race at {phase}", concurrent_revoke)

        def concurrent_publish():
            def publish():
                self.publish("acme", self.get("TENANT#acme")["documents"])
            expect(self.race("after_generate", publish), 409)
        check("controlled corpus publication race rejects obsolete generation", concurrent_publish)

        def late_boundary():
            result = self.race("after_final_authorize", lambda: self.grant("alice", ["public"]))
            expect(result, days=90)
            expect(self.query(), days=30)
            return {"boundary": "First response authorized before revocation; late fill is not reusable by revoked user"}
        check("post-authorization boundary and delayed cache fill are explicit", late_boundary)

        def unavailable():
            self.restore()
            expect(self.query(), days=90)
            for mode in ["none", "exact", "semantic"]:
                result = self.query(mode=mode, fault="authority_unavailable")
                expect(result, 503)
                assert "answer" not in result["body"]
            return {"fault_method": "IAM-only handler injects authority failure before final read; not a service outage"}
        check("authority fault fails closed in all cache modes", unavailable)

        def cache_auth_failure():
            body = expect(self.query("bob", fault="cache_auth_rejected"), days=30, path="generation")
            assert body["metrics"]["cache_errors"] == 3
            assert all("internal" not in d["doc_id"] for d in body["dependencies"])
            return {"fault_method": "real Valkey TLS connection with intentionally invalid AUTH token"}
        check("Valkey authentication rejection falls back to authorized generation", cache_auth_failure)

        def negatives():
            for q in ["What is the retention period for Atlas in 2024?",
                      "What is NOT the retention period for Atlas?",
                      "What is the retention period for Atlas contractors?"]:
                body = expect(self.query(question=q))
                assert not body["semantic_admitted"]
                assert body["path"] == "generation"
        check("date negation and audience qualifiers bypass semantic reuse", negatives)

        def source_integrity():
            ref = next(d for d in self.get("TENANT#acme")["documents"] if d["doc_id"] == "atlas-public")
            original = self.s3.get_object(Bucket=self.r["bucket"], Key=ref["key"])["Body"].read()
            try:
                self.s3.put_object(Bucket=self.r["bucket"], Key=ref["key"], Body=b'{"text":"tampered fixture"}')
                result = self.query("bob", mode="none")
                expect(result, 503)
                assert "answer" not in result["body"]
            finally:
                self.s3.put_object(Bucket=self.r["bucket"], Key=ref["key"], Body=original, ContentType="application/json")
            expect(self.query("bob", mode="none"), days=30)
        check("changed S3 object bytes fail the authoritative source hash", source_integrity)

        self.restore()
        failed = sum(not x["passed"] for x in results)
        print(json.dumps({"passed": len(results)-failed, "failed": failed}), flush=True)
        if failed:
            raise SystemExit(1)

    def benchmark(self):
        self.restore()
        rows = []
        variants = [
            "What is the retention period for {product}?",
            "How long does {product} keep records?",
            "How long are {product} records stored?",
            "For how long does {product} retain data?",
        ]
        workload = [pattern.format(product=product) for pattern in variants for product in ["Atlas", "Boreal"]]
        workload += workload  # Repeat exact strings after the paraphrase pass.
        # Warm the test function and integrations, then isolate every cache arm.
        self.query(question=Q, mode="none")
        for mode in ["none", "exact", "semantic"]:
            self.invoke({"action": "clear"})
            for index, question in enumerate(workload):
                result = self.query(question=question, mode=mode)
                if result["statusCode"] != 200:
                    raise RuntimeError(result)
                body = result["body"]
                expected = 90 if "Atlas" in question else 120
                correct = f"{expected} days" in body["answer"].lower()
                rows.append({"mode": mode, "request": index, "question": question,
                    "correct": correct, "path": body["path"], "client_ms": result["client_ms"],
                    **body["metrics"]})
                save_report("benchmark-requests.json", rows)
                print(json.dumps({"mode": mode, "request": index, "path": body["path"],
                                  "correct": correct, "server_ms": body["metrics"]["server_ms"]}), flush=True)
        summary = {}
        for mode in ["none", "exact", "semantic"]:
            arm = [x for x in rows if x["mode"] == mode]
            latencies = sorted(x["server_ms"] for x in arm)
            summary[mode] = {
                "requests": len(arm), "correct": sum(x["correct"] for x in arm),
                "generation_calls": sum(x["model_calls"] for x in arm),
                "exact_hits": sum(x["path"] == "exact_hit" for x in arm),
                "semantic_hits": sum(x["path"] == "semantic_hit" for x in arm),
                "median_server_ms": round(statistics.median(latencies), 3),
                "p95_server_ms": latencies[max(0, __import__('math').ceil(len(latencies)*0.95)-1)],
                **{key: sum(x[key] for x in arm) for key in ["input_tokens", "output_tokens", "embedding_tokens",
                                                           "authority_rcu", "document_reads", "cache_errors"]},
            }
        save_report("benchmark-summary.json", {"workload": "16 sequential requests per arm; two products; four admitted paraphrases each, repeated twice",
            "transport": "IAM Lambda Invoke; client latency includes operator network; server latency excludes Lambda initialization",
            "cache_start": "empty for each arm", "model": self.r["model_id"],
            "embedding_model": "amazon.titan-embed-text-v2:0", "arms": summary})
        print(json.dumps(summary, indent=2), flush=True)

    def diagnostics(self):
        self.restore()
        self.invoke({"action": "clear"})
        assert self.query()["statusCode"] == 200
        result = self.invoke({"action": "vector_diagnostics", "subject": self.users["alice"]["sub"],
                              "question": "How long does Atlas keep records?"})
        save_report("vector-compatibility.json", result)
        print(json.dumps(result, indent=2), flush=True)
        assert result["native_knn"].get("matches", 0) >= 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["seed", "test", "benchmark", "setup", "smoke", "diagnostics"])
    action = parser.parse_args().action
    lab = Lab()
    if action == "seed":
        lab.seed()
    elif action == "test":
        lab.test()
    elif action == "benchmark":
        lab.benchmark()
    elif action == "diagnostics":
        lab.diagnostics()
    elif action == "setup":
        print(json.dumps(lab.invoke({"action": "setup"})))
    elif action == "smoke":
        print(json.dumps(lab.query(), indent=2))


if __name__ == "__main__":
    main()
