from dataclasses import replace
import json

import pytest

from ragcache.core import (Changed, Denied, DocumentRef, Entry, Service, Snapshot, Unavailable,
                           digest, semantic_contract)


class Authority:
    def __init__(self):
        self.state = Snapshot("alice", "acme", 1, 1, ("internal", "public"), (
            DocumentRef("public", "v1", "public", "atlas", "public.json", "publichash", 0),
            DocumentRef("internal", "v1", "internal", "atlas", "internal.json", "internalhash", 10),
        ))
        self.fail = False

    def snapshot(self, subject):
        if self.fail:
            raise Unavailable("offline")
        return replace(self.state, subject=subject)

    def revoke(self):
        self.state = replace(self.state, grants=("public",), principal_version=self.state.principal_version + 1)


class Cache:
    def __init__(self):
        self.entries = {}
        self.fail = False

    def get_exact(self, scope, question):
        if self.fail:
            raise Unavailable("cache offline")
        return self.entries.get((scope, question))

    def nearest(self, scope, contract, vector, threshold):
        if self.fail:
            raise Unavailable("cache offline")
        return next((v for (s, _), v in self.entries.items() if s == scope and v.contract == contract), None)

    def put(self, entry):
        if self.fail:
            raise Unavailable("cache offline")
        self.entries[(entry.scope_id, entry.question)] = entry


class Documents:
    def load(self, ref):
        return {"text": "90 days" if ref.scope == "internal" else "30 days", "embedding": [1.0, 0.0]}


class Model:
    def __init__(self):
        self.calls = 0

    def embed(self, text):
        return [1.0, 0.0], 5

    def generate(self, question, contexts):
        self.calls += 1
        chosen = max(contexts, key=lambda d: d["priority"])
        return chosen["text"], {"inputTokens": 20, "outputTokens": 4}


@pytest.fixture
def lab():
    authority, cache, model = Authority(), Cache(), Model()
    return Service(authority, cache, Documents(), model, "pipeline-v1"), authority, cache, model


Q = "What is the retention period for Atlas?"
PARAPHRASE = "How long does Atlas keep records?"


def test_exact_cache_avoids_model_and_embedding(lab):
    svc, _, _, model = lab
    assert svc.query("alice", Q)["path"] == "generation"
    second = svc.query("alice", Q)
    assert second["path"] == "exact_hit"
    assert second["metrics"]["embedding_tokens"] == 0
    assert model.calls == 1


def test_semantic_hit_requires_admitted_contract(lab):
    svc, _, _, model = lab
    svc.query("alice", Q)
    assert svc.query("alice", PARAPHRASE)["path"] == "semantic_hit"
    assert model.calls == 1


@pytest.mark.parametrize("question", [
    "What is the retention period for Boreal?",
    "What is NOT the retention period for Atlas?",
    "What was the retention period for Atlas in 2024?",
    "What is the retention period for Atlas contractors?",
    "Is the retention period for Atlas 30 days?",
    "How long does Atlas keep records except backups?",
])
def test_nearby_but_different_questions_do_not_reuse_answer(lab, question):
    svc, _, _, _ = lab
    svc.query("alice", Q)
    result = svc.query("alice", question)
    assert result["path"] == "generation"


def test_revocation_does_not_depend_on_cache_eviction(lab):
    svc, authority, cache, _ = lab
    assert svc.query("alice", Q)["answer"] == "90 days"
    old_keys = set(cache.entries)
    authority.revoke()
    result = svc.query("alice", Q)
    assert result["answer"] == "30 days"
    assert old_keys <= set(cache.entries)
    assert [d["doc_id"] for d in result["dependencies"]] == ["public"]


def test_cohort_sharing_without_personal_context(lab):
    svc, _, _, _ = lab
    svc.query("alice", Q)
    assert svc.query("carol", Q)["path"] == "exact_hit"


def test_tenant_isolation_even_with_identical_document_ids(lab):
    svc, authority, _, _ = lab
    svc.query("alice", Q)
    authority.state = replace(authority.state, tenant="globex")
    assert svc.query("dana", Q)["path"] == "generation"


@pytest.mark.parametrize("phase", ["after_lookup", "after_generate", "before_final_authorize"])
def test_revocation_before_final_snapshot_rejects_inflight_answer(lab, phase):
    svc, authority, _, _ = lab
    def hook(actual):
        if actual == phase:
            authority.revoke()
    with pytest.raises(Changed):
        svc.query("alice", Q, hook=hook)


def test_revocation_during_cache_hit_rejects_answer(lab):
    svc, authority, _, _ = lab
    svc.query("alice", Q)
    with pytest.raises(Changed):
        svc.query("alice", Q, hook=lambda phase: authority.revoke() if phase == "after_lookup" else None)


def test_after_final_authorization_is_explicitly_outside_revocation_guarantee(lab):
    svc, authority, cache, _ = lab
    result = svc.query("alice", Q, hook=lambda phase: authority.revoke()
                       if phase == "after_final_authorize" else None)
    assert result["answer"] == "90 days"  # Authorized before revocation.
    assert len(cache.entries) == 1         # Delayed fill remains harmless.
    assert svc.query("alice", Q)["answer"] == "30 days"


def test_new_document_invalidates_answer_even_when_existing_dependencies_unchanged(lab):
    svc, authority, _, model = lab
    svc.query("alice", Q)
    authority.state = replace(authority.state, corpus_version=2, documents=authority.state.documents + (
        DocumentRef("amendment", "v1", "public", "atlas", "new.json", "newhash", 20),))
    assert svc.query("alice", Q)["path"] == "generation"
    assert model.calls == 2


def test_corpus_publish_during_generation_returns_conflict(lab):
    svc, authority, _, _ = lab
    def publish(phase):
        if phase == "after_generate":
            authority.state = replace(authority.state, corpus_version=2)
    with pytest.raises(Changed):
        svc.query("alice", Q, hook=publish)


@pytest.mark.parametrize("mode", ["none", "exact", "semantic"])
def test_authority_outage_fails_closed_in_all_modes(lab, mode):
    svc, authority, _, _ = lab
    svc.query("alice", Q)
    authority.fail = True
    with pytest.raises(Unavailable):
        svc.query("alice", Q, mode)


def test_final_authority_outage_never_releases_cached_answer(lab):
    svc, authority, _, _ = lab
    svc.query("alice", Q)
    def hook(phase):
        if phase == "before_final_authorize":
            authority.fail = True
    with pytest.raises(Unavailable):
        svc.query("alice", Q, hook=hook)


def test_cache_outage_falls_back_to_authorized_generation(lab):
    svc, _, cache, _ = lab
    cache.fail = True
    result = svc.query("alice", Q)
    assert result["answer"] == "90 days"
    assert result["metrics"]["cache_errors"] == 3


def test_disabled_principal_is_denied(lab):
    svc, authority, _, _ = lab
    svc.query("alice", Q)
    authority.state = replace(authority.state, enabled=False)
    with pytest.raises(Denied):
        svc.query("alice", Q)


def test_all_model_context_is_provenance_even_if_answer_uses_one_document(lab):
    svc, _, _, _ = lab
    result = svc.query("alice", Q)
    assert {d["doc_id"] for d in result["dependencies"]} == {"public", "internal"}


def test_pipeline_revision_invalidates_existing_entries(lab):
    svc, _, _, _ = lab
    svc.query("alice", Q)
    svc.pipeline = "pipeline-v2"
    assert svc.query("alice", Q)["path"] == "generation"


def test_source_interpretation_metadata_changes_scope(lab):
    svc, authority, _, _ = lab
    old_scope = authority.state.scope_id(svc.pipeline)
    docs = list(authority.state.documents)
    docs[0] = replace(docs[0], priority=100)
    authority.state = replace(authority.state, documents=tuple(docs))
    assert authority.state.scope_id(svc.pipeline) != old_scope


def test_unknown_question_case_is_not_normalized_for_exact_reuse(lab):
    svc, _, _, model = lab
    svc.query("alice", "Tell me the US policy", mode="exact")
    assert svc.query("alice", "Tell me the us policy", mode="exact")["path"] == "generation"
    assert model.calls == 2


def test_forged_dependency_is_rejected(lab):
    svc, authority, _, _ = lab
    snap = authority.state
    entry = Entry(snap.scope_id(svc.pipeline), snap.tenant, snap.corpus_version, svc.pipeline,
                  Q, semantic_contract(Q), "secret", [{"doc_id": "other", "version": "v1", "sha256": "x"}])
    assert not entry.valid_for(snap, svc.pipeline, snap.scope_id(svc.pipeline))


def test_corpus_fingerprint_defends_against_missed_version_bump(lab):
    svc, authority, _, _ = lab
    before = authority.state.scope_id(svc.pipeline)
    authority.state = replace(authority.state, documents=(replace(authority.state.documents[0], version="v2"),))
    assert authority.state.scope_id(svc.pipeline) != before


def test_public_handler_rejects_identity_and_test_hook_injection(monkeypatch):
    import handler
    event = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "alice"}}}},
             "body": json.dumps({"question": Q, "subject": "admin", "barrier": {}})}
    monkeypatch.setattr(handler, "run", lambda *_: pytest.fail("Must reject before execution"))
    assert handler.lambda_handler(event, None)["statusCode"] == 400


def test_public_handler_rejects_missing_gateway_identity():
    import handler
    assert handler.lambda_handler({"body": json.dumps({"question": Q})}, None)["statusCode"] == 401


def test_digest_is_stable_for_mapping_order():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
