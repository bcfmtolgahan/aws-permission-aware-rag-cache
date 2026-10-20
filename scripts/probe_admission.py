#!/usr/bin/env python3
"""Measure fixture similarity and admission, without claiming general accuracy."""
import json
import sys

from cloud import ROOT, guarded_session

sys.path.insert(0, str(ROOT/"src"))
from ragcache.aws_adapters import BedrockModel  # noqa: E402
from ragcache.core import cosine, semantic_contract  # noqa: E402


def main():
    session, _ = guarded_session()
    model = BedrockModel(session.client("bedrock-runtime"), "eu.amazon.nova-micro-v1:0")
    questions = ["What is the retention period for Atlas?", "How long does Atlas keep records?",
                 "How long are Atlas records stored?", "For how long does Atlas retain data?",
                 "What is the retention period for Boreal?", "What is the retention period for Atlas in 2024?",
                 "What is NOT the retention period for Atlas?", "What is the retention period for Atlas contractors?"]
    vectors = [model.embed(q)[0] for q in questions]
    rows = [{"question": q, "similarity_to_atlas": round(cosine(vectors[0], v), 6),
             "admitted_contract": semantic_contract(q)} for q, v in zip(questions, vectors)]
    report = {"purpose": "fixture diagnostic, not a general semantic-safety evaluation", "rows": rows}
    (ROOT/"results").mkdir(exist_ok=True)
    (ROOT/"results/admission-probe.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
