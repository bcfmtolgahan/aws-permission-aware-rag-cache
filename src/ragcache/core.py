"""Service-independent authorization and cache protocol.

The final authority snapshot is the authorization linearization point. It is
not atomic with network delivery. Cache eviction never grants authorization.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import re
import time
from typing import Callable, Protocol


class Denied(Exception):
    pass


class Changed(Exception):
    pass


class Unavailable(Exception):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def normalize(question: str) -> str:
    return " ".join(question.lower().strip().rstrip("?.!").split())


def semantic_contract(question: str) -> str | None:
    """Deliberately bounded admission grammar, NOT a general semantic verifier.

    Only unqualified retention questions for two synthetic products can share
    answers by embedding similarity. Everything else uses exact matching.
    """
    text = normalize(question)
    patterns = (
        r"what is the (?:data )?retention (?:period|policy) for (atlas|boreal)",
        r"how long does (atlas|boreal) (?:keep|retain|store) (?:data|records)",
        r"how long (?:is|are) (atlas|boreal) (?:data|records) (?:kept|retained|stored)",
        r"for how long does (atlas|boreal) retain (?:data|records)",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return f"retention:{match[1]}:unqualified:v1"
    return None


@dataclass(frozen=True)
class DocumentRef:
    doc_id: str
    version: str
    scope: str
    topic: str
    key: str
    sha256: str
    priority: int = 0


@dataclass(frozen=True)
class Snapshot:
    subject: str
    tenant: str
    principal_version: int
    corpus_version: int
    grants: tuple[str, ...]
    documents: tuple[DocumentRef, ...]
    enabled: bool = True

    @property
    def allowed(self) -> tuple[DocumentRef, ...]:
        return tuple(d for d in self.documents if d.scope in self.grants)

    def validate(self):
        if not self.enabled or not self.grants:
            raise Denied("No active document entitlement")

    def scope_id(self, pipeline: str) -> str:
        # Users can share only an identical current, non-personalized view.
        # Principal revision is checked separately so equal entitlements share.
        return digest({
            "tenant": self.tenant, "corpus": self.corpus_version,
            "grants": sorted(self.grants), "pipeline": pipeline,
            "visible_documents": sorted((d.doc_id, d.version, d.sha256, d.scope, d.topic,
                                         d.key, d.priority) for d in self.allowed),
        })

    def authorization_stamp(self) -> str:
        return digest(asdict(self))


@dataclass
class Entry:
    scope_id: str
    tenant: str
    corpus_version: int
    pipeline: str
    question: str
    contract: str | None
    answer: str
    dependencies: list[dict]
    vector: list[float] = field(default_factory=list)

    def valid_for(self, snapshot: Snapshot, pipeline: str, scope_id: str) -> bool:
        allowed = {(d.doc_id, d.version, d.sha256) for d in snapshot.allowed}
        dependencies = {(d["doc_id"], d["version"], d["sha256"]) for d in self.dependencies}
        return (
            self.scope_id == scope_id and self.tenant == snapshot.tenant
            and self.corpus_version == snapshot.corpus_version and self.pipeline == pipeline
            and bool(dependencies) and dependencies <= allowed
        )


class Authority(Protocol):
    def snapshot(self, subject: str) -> Snapshot: ...


class Cache(Protocol):
    def get_exact(self, scope: str, question: str) -> Entry | None: ...
    def nearest(self, scope: str, contract: str, vector: list[float], threshold: float) -> Entry | None: ...
    def put(self, entry: Entry): ...


class Documents(Protocol):
    def load(self, ref: DocumentRef) -> dict: ...


class Model(Protocol):
    def embed(self, text: str) -> tuple[list[float], int]: ...
    def generate(self, question: str, contexts: list[dict]) -> tuple[str, dict]: ...


def cosine(left, right) -> float:
    denominator = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    return sum(a*b for a, b in zip(left, right, strict=True)) / denominator if denominator else 0


class Service:
    def __init__(self, authority: Authority, cache: Cache, documents: Documents, model: Model,
                 pipeline: str, threshold: float = 0.75, top_k: int = 8):
        self.authority, self.cache, self.documents, self.model = authority, cache, documents, model
        self.pipeline, self.threshold, self.top_k = pipeline, threshold, top_k

    def query(self, subject: str, question: str, mode: str = "semantic",
              hook: Callable[[str], None] = lambda phase: None) -> dict:
        if mode not in {"none", "exact", "semantic"}:
            raise ValueError("mode must be none, exact or semantic")
        if not isinstance(question, str) or not 3 <= len(question) <= 1000:
            raise ValueError("question must contain 3 to 1000 characters")
        started = time.perf_counter()
        metrics = {"authority_reads": 0, "embedding_tokens": 0, "input_tokens": 0,
                   "output_tokens": 0, "document_reads": 0, "cache_errors": 0, "model_calls": 0}

        def read_authority():
            metrics["authority_reads"] += 1
            snap = self.authority.snapshot(subject)
            snap.validate()
            return snap

        initial = read_authority()
        scope = initial.scope_id(self.pipeline)
        contract = semantic_contract(question)
        entry, vector = None, None
        path = "generation"
        if mode != "none":
            try:
                entry = self.cache.get_exact(scope, question)
                if entry:
                    path = "exact_hit"
            except Unavailable:
                metrics["cache_errors"] += 1
        if entry and (entry.question != question or not entry.valid_for(initial, self.pipeline, scope)):
            entry = None
        if not entry and mode == "semantic" and contract:
            vector, tokens = self.model.embed(question)
            metrics["embedding_tokens"] += tokens
            try:
                candidate = self.cache.nearest(scope, contract, vector, self.threshold)
                if candidate and candidate.contract == contract and candidate.valid_for(initial, self.pipeline, scope):
                    entry, path = candidate, "semantic_hit"
            except Unavailable:
                metrics["cache_errors"] += 1
        hook("after_lookup")

        if entry is None:
            path = "generation"
            if vector is None:
                vector, tokens = self.model.embed(question)
                metrics["embedding_tokens"] += tokens
            ranked = []
            for ref in initial.allowed:
                doc = self.documents.load(ref)
                metrics["document_reads"] += 1
                score = cosine(vector, doc["embedding"])
                ranked.append((score, ref, doc))
            ranked.sort(key=lambda row: (-row[0], row[1].doc_id))
            selected = ranked[:self.top_k]
            if not selected:
                raise Denied("No authorized sources")
            contexts = [{"doc_id": ref.doc_id, "priority": ref.priority, "text": doc["text"]}
                        for _, ref, doc in selected]
            answer, usage = self.model.generate(question, contexts)
            metrics["model_calls"] += 1
            metrics["input_tokens"] = usage.get("inputTokens", 0)
            metrics["output_tokens"] = usage.get("outputTokens", 0)
            entry = Entry(scope, initial.tenant, initial.corpus_version, self.pipeline, question,
                          contract, answer,
                          [{"doc_id": ref.doc_id, "version": ref.version, "sha256": ref.sha256}
                           for _, ref, _ in selected], vector)
            hook("after_generate")

        # The decision is based on one final authoritative snapshot. It does
        # not lock the authority through HTTP delivery; that boundary is tested.
        hook("before_final_authorize")
        final = read_authority()
        if final.authorization_stamp() != initial.authorization_stamp():
            raise Changed("Authorization or corpus changed; retry with fresh context")
        if not entry.valid_for(final, self.pipeline, scope):
            raise Denied("Candidate dependencies are not authorized")
        hook("after_final_authorize")

        if path == "generation" and mode != "none":
            try:
                self.cache.put(entry)
            except Unavailable:
                metrics["cache_errors"] += 1
        metrics["server_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return {"answer": entry.answer, "path": path, "dependencies": entry.dependencies,
                "metrics": metrics, "corpus_version": initial.corpus_version,
                "semantic_admitted": contract is not None}
