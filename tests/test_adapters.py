import io
from dataclasses import asdict
import json
import sys
from pathlib import Path

from boto3.dynamodb.types import TypeSerializer
import pytest

from ragcache.aws_adapters import DynamoAuthority, S3Documents, ValkeyCache
from ragcache.core import Denied, DocumentRef, Entry, Unavailable


def encode(data):
    return {k: TypeSerializer().serialize(v) for k, v in data.items()}


class Dynamo:
    def __init__(self, routed_tenant="acme", transactional_tenant="acme"):
        self.routed_tenant, self.transactional_tenant = routed_tenant, transactional_tenant
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        return {"Item": encode({"tenant": self.routed_tenant}), "ConsumedCapacity": {"CapacityUnits": 1}}

    def transact_get_items(self, **kwargs):
        self.calls.append(kwargs)
        user = {"tenant": self.transactional_tenant, "version": 4, "enabled": True, "grants": ["public"]}
        corpus = {"tenant": self.routed_tenant, "version": 8, "documents": []}
        return {"Responses": [{"Item": encode(user)}, {"Item": encode(corpus)}],
                "ConsumedCapacity": [{"CapacityUnits": 4}]}


def test_authority_rejects_tenant_move_between_routing_and_transaction():
    with pytest.raises(Denied):
        DynamoAuthority(Dynamo(transactional_tenant="globex"), "test").snapshot("alice")


def test_authority_uses_transactional_user_and_corpus_versions():
    client = Dynamo()
    authority = DynamoAuthority(client, "test")
    snapshot = authority.snapshot("alice")
    assert (snapshot.principal_version, snapshot.corpus_version) == (4, 8)
    assert client.calls[0]["ConsistentRead"] is True
    assert len(client.calls[1]["TransactItems"]) == 2
    assert authority.rcu == 5


def test_modified_s3_object_fails_before_model_input():
    class S3:
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(b'{"text":"tampered"}')}
    ref = DocumentRef("atlas", "v1", "public", "atlas", "key", "expected-checksum")
    with pytest.raises(Unavailable, match="digest mismatch"):
        S3Documents(S3(), "bucket").load(ref)


@pytest.mark.parametrize("payload", [b"not json", b"{}", json.dumps({"answer": "bad"}).encode()])
def test_malformed_cache_records_are_not_answers(payload):
    with pytest.raises(Unavailable):
        ValkeyCache.decode(payload)


def test_exact_key_preserves_case_and_punctuation():
    assert ValkeyCache.key("scope", "US policy?") != ValkeyCache.key("scope", "us policy?")
    assert ValkeyCache.key("scope", "Value 1.0") != ValkeyCache.key("scope", "Value 1.0.")


@pytest.mark.parametrize("distance,accepted", [(0.24, True), (0.25, True), (0.26, False)])
def test_native_knn_distance_is_converted_to_similarity(distance, accepted):
    entry = Entry("scope", "acme", 1, "pipeline", "question", "contract", "answer",
                  [{"doc_id": "d", "version": "v", "sha256": "h"}], [1.0, 0.0])
    class Client:
        def execute_command(self, *args):
            return [1, b"key", [b"__embedding_score", str(distance).encode(),
                                b"payload", json.dumps(asdict(entry)).encode()]]
    cache = object.__new__(ValkeyCache)
    cache.client = Client()
    candidate = cache.nearest("scope", "contract", [1.0, 0.0], 0.75)
    assert (candidate is not None) is accepted


def test_cloud_environment_cannot_select_another_profile(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
    import cloud
    monkeypatch.setattr(cloud, "LOCAL", tmp_path)
    cloud.configure("test-sandbox", "000000000000")
    monkeypatch.setenv("AWS_PROFILE", "another-profile")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "not-a-real-key")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://invalid.example")
    monkeypatch.setenv("TF_VAR_expected_account_id", "000000000000")
    monkeypatch.setenv("TF_CLI_ARGS_apply", "-auto-approve")
    env = cloud.clean_environment()
    assert env["AWS_PROFILE"] == "test-sandbox"
    assert env["AWS_REGION"] == "eu-central-1"
    assert not {"AWS_ACCESS_KEY_ID", "AWS_ENDPOINT_URL", "TF_VAR_expected_account_id",
                "TF_CLI_ARGS_apply"} & env.keys()
