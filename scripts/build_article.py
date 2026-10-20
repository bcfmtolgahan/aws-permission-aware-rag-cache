#!/usr/bin/env python3
"""Build a portable HTML article from repository excerpts and recorded evidence.

No AWS calls. --check verifies the saved article, links, snippets and measurements.
"""
import argparse
import ast
import base64
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import statistics
import textwrap
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CORE = "src/ragcache/core.py"
ADAPTERS = "src/ragcache/aws_adapters.py"
TERRAFORM = "infra/main.tf"
LAB = "scripts/lab.py"
SNIPPETS = []


def read(path):
    return (ROOT / path).read_text()


def data(name):
    return json.loads(read(f"results/{name}.json"))


def excerpt(path, first, last, language="python"):
    lines = read(path).splitlines()
    code = textwrap.dedent("\n".join(lines[first - 1:last]))
    SNIPPETS.append({
        "path": path, "start": first, "end": last,
        "sha256": hashlib.sha256(code.encode()).hexdigest(),
    })
    label = f"{path} · lines {first}–{last}"
    return code_block(code, label, language, path)


def symbol(path, qualified):
    node = ast.parse(read(path))
    for name in qualified.split("."):
        node = next(n for n in node.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))
                    and n.name == name)
    return excerpt(path, node.lineno, node.end_lineno)


def span(path, first, last, language="python", extra_lines=0):
    lines = read(path).splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == first]
    if len(starts) != 1:
        raise ValueError(f"Ambiguous start in {path}: {first}")
    start = starts[0]
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == last)
    return excerpt(path, start + 1, end + 1 + extra_lines, language)


def hcl(header):
    lines = read(TERRAFORM).splitlines()
    start = lines.index(header)
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return excerpt(TERRAFORM, start + 1, end + 1, "hcl")


def code_block(code, label, language, source=None):
    title = f'<a href="{escape(source)}">{escape(label)}</a>' if source else escape(label)
    return (
        '<div class="code-block"><div class="code-label"><span>' + title
        + '</span><button class="copy" type="button" hidden>Copy</button></div>'
        + f'<pre tabindex="0"><code class="language-{language}">'
        + escape(code) + "</code></pre></div>"
    )


def table(caption, headers, rows, numeric=()):
    def cells(values, tag):
        return "".join(
            f'<{tag}' + (' class="number"' if i in numeric else "")
            + (' scope="col"' if tag == "th" else "") + f">{escape(str(value))}</{tag}>"
            for i, value in enumerate(values)
        )
    return (
        '<div class="table-wrap"><table><caption>' + escape(caption) + "</caption><thead><tr>"
        + cells(headers, "th") + "</tr></thead><tbody>"
        + "".join("<tr>" + cells(row, "td") + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


FIGURES = {
    "cover": ("Amazon Bedrock, Amazon ElastiCache, Amazon DynamoDB and AWS Lambda",
              "The experiment uses Amazon Bedrock, Amazon ElastiCache, Amazon DynamoDB and AWS Lambda."),
    "architecture": (
        "Cognito and API Gateway call a private Lambda connected to ElastiCache, DynamoDB, S3 and Bedrock",
        "The deployed lab. Each authority snapshot includes a routing GetItem and a TransactGetItems. "
        "DynamoDB and S3 use gateway endpoints; Bedrock Runtime uses an interface endpoint. "
        "Nova Micro uses an EU inference profile. S3 immutability is enforced by the application protocol."),
    "authorization-flow": (
        "Initial authority snapshot, cache lookup or generation, then final authority snapshot",
        "Both hits and generated answers pass the final snapshot. A later revocation can race with delivery."),
}


def figure(name):
    alt, caption = FIGURES[name]
    raw = (ROOT / f"docs/diagrams/{name}.svg").read_bytes()
    encoded = base64.b64encode(raw).decode()
    return (f'<figure class="diagram"><img src="data:image/svg+xml;base64,{encoded}" '
            f'alt="{escape(alt)}"><figcaption>{escape(caption)}</figcaption></figure>')


def verify_measurements():
    rows, summary = data("benchmark-requests"), data("benchmark-summary")["arms"]
    for mode, expected in summary.items():
        arm = [row for row in rows if row["mode"] == mode]
        latencies = sorted(row["server_ms"] for row in arm)
        actual = {
            "requests": len(arm), "correct": sum(row["correct"] for row in arm),
            "generation_calls": sum(row["model_calls"] for row in arm),
            "exact_hits": sum(row["path"] == "exact_hit" for row in arm),
            "semantic_hits": sum(row["path"] == "semantic_hit" for row in arm),
            "median_server_ms": round(statistics.median(latencies), 3),
            "p95_server_ms": latencies[math.ceil(len(arm) * .95) - 1],
            **{key: sum(row[key] for row in arm) for key in (
                "input_tokens", "output_tokens", "embedding_tokens",
                "authority_rcu", "document_reads", "cache_errors")},
        }
        if actual != expected:
            raise ValueError(f"Raw observations disagree with summary: {mode}")
    if data("local-tests")["failed"] or not all(t["passed"] for t in data("cloud-tests")["tests"]):
        raise ValueError("Article must be revised for failing tests")
    if not data("cleanup")["all_absent"] or not all(c["absent"] for c in data("cleanup")["checks"]):
        raise ValueError("Cleanup evidence does not support the article")
    return summary


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.refs, self.codes, self.text = set(), [], [], []
        self.in_code = False
        self.in_pre = False
        self.h1 = 0
        self.images = 0
        self.headings = []
        self.in_heading = False
        self.in_script = False
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            if attributes["id"] in self.ids:
                raise ValueError("Duplicate HTML id")
            self.ids.add(attributes["id"])
        if tag == "a":
            self.refs.append(attributes.get("href", ""))
        if tag == "img":
            self.images += 1
            assert attributes.get("alt") and attributes["src"].startswith("data:image/svg+xml;base64,")
        if tag == "h1":
            self.h1 += 1
        if tag == "pre":
            self.in_pre = True
        if tag == "code" and self.in_pre:
            self.in_code = True
            self.codes.append("")
        if tag == "h2":
            self.in_heading = True
            self.headings.append("")
        if tag == "script":
            self.in_script = True
        if tag == "style":
            self.in_style = True

    def handle_endtag(self, tag):
        if tag == "code":
            self.in_code = False
        if tag == "pre":
            self.in_pre = False
        if tag == "h2":
            self.in_heading = False
        if tag == "script":
            self.in_script = False
        if tag == "style":
            self.in_style = False

    def handle_data(self, value):
        if self.in_code:
            self.codes[-1] += value
        if self.in_heading:
            self.headings[-1] += value
        if not self.in_code and not self.in_script and not self.in_style:
            self.text.append(value)


def validate(html):
    parser = ArticleParser()
    parser.feed(html)
    assert parser.h1 == 1 and parser.images == 3
    assert not re.search(r"\{\{(?:code|table|figure|value):", html), "Unexpanded article placeholder"
    prose = " ".join(parser.text)
    assert "—" not in prose and "##" not in prose
    for ref in parser.refs:
        parsed = urlsplit(ref)
        if parsed.scheme in {"http", "https"}:
            continue
        if parsed.path:
            assert (ROOT / unquote(parsed.path)).is_file(), f"Missing link: {ref}"
        elif parsed.fragment:
            assert parsed.fragment in parser.ids, f"Missing anchor: {ref}"
    for snippet in SNIPPETS:
        original = textwrap.dedent("\n".join(
            read(snippet["path"]).splitlines()[snippet["start"] - 1:snippet["end"]]))
        assert original in parser.codes, f"Changed source excerpt: {snippet['path']}"
    return parser, len(re.findall(r"\b[\w'-]+\b", prose))


COPY_SCRIPT = """
for (const button of document.querySelectorAll(".copy")) {
  if (!navigator.clipboard) continue;
  button.hidden = false;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(button.closest(".code-block").querySelector("code").textContent);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select to copy";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 1800);
  });
}
"""


def build():
    SNIPPETS.clear()
    arms = verify_measurements()
    cloud, local, runtime, cleanup = data("cloud-tests"), data("local-tests"), data("runtime"), data("cleanup")
    codes = {
        "endpoints": span(TERRAFORM, 'resource "aws_vpc_endpoint" "gateway" {',
                          "security_group_ids  = [aws_security_group.endpoints.id]", "hcl", extra_lines=1),
        "cache_resource": hcl('resource "aws_elasticache_replication_group" "lab" {'),
        "authority": symbol(ADAPTERS, "DynamoAuthority.snapshot"),
        "scope": symbol(CORE, "Snapshot.scope_id"),
        "exact_key": symbol(ADAPTERS, "ValkeyCache.key"),
        "pipeline": span("src/handler.py",
                         'pipeline = digest({"prompt": "authorized-retention-v1", "model": model_id,',
                         '"threshold": threshold, "temperature": 0, "max_tokens": 160})'),
        "entry_validation": symbol(CORE, "Entry.valid_for"),
        "provenance": span(CORE,
                           'contexts = [{"doc_id": ref.doc_id, "priority": ref.priority, "text": doc["text"]}',
                           "for _, ref, _ in selected], vector)"),
        "publish": symbol(LAB, "Lab.publish"),
        "grammar": symbol(CORE, "semantic_contract"),
        "index": symbol(ADAPTERS, "ValkeyCache.setup"),
        "nearest": symbol(ADAPTERS, "ValkeyCache.nearest"),
        "vector_diagnostic": code_block(
            json.dumps(data("vector-compatibility"), indent=2),
            "Recorded engine response · results/vector-compatibility.json", "json",
            "results/vector-compatibility.json"),
        "final_check": span(CORE, 'hook("before_final_authorize")', 'hook("after_final_authorize")'),
        "race_tests": span(LAB,
                           'for phase, warm in [("after_lookup", True), ("after_generate", False), ("before_final_authorize", False)]:',
                           'check(f"controlled revocation race at {phase}", concurrent_revoke)'),
        "late_boundary": symbol(LAB, "Lab.test.late_boundary"),
        "provider": hcl('provider "aws" {'),
        "commands": code_block(
            "uv sync --locked\nuv run pytest -q\nuv run python scripts/build.py\n\n"
            "uv run python scripts/cloud.py configure \\\n"
            "  --profile my-sandbox \\\n"
            "  --account-id YOUR_12_DIGIT_ACCOUNT_ID\n\n"
            "uv run python scripts/cloud.py preflight\nuv run python scripts/cloud.py init\n"
            "uv run python scripts/cloud.py plan\nuv run python scripts/cloud.py review\n"
            "uv run python scripts/cloud.py apply\nuv run python scripts/verify_deployment.py\n\n"
            "uv run python scripts/lab.py seed\nuv run python scripts/probe_admission.py\n"
            "uv run python scripts/lab.py diagnostics\nuv run python scripts/lab.py test\n"
            "uv run python scripts/lab.py benchmark\n\n"
            "uv run python scripts/cloud.py destroy\nuv run python scripts/verify_cleanup.py",
            "Run from the repository root · README.md", "bash", "README.md"),
    }
    names = {"none": "No answer cache", "exact": "Exact", "semantic": "Exact + semantic"}
    sizes = {a["requests"] for a in arms.values()}
    assert len(sizes) == 1, "Revise the article for unequal comparison groups"
    per_mode = sizes.pop()
    scenario_labels = {
        "authenticated HTTP API generates and disables downstream caching":
            "authenticated HTTP API generates and sends Cache-Control: no-store",
        "revocation works with old JWT and old cache entries still present":
            "old JWT uses updated grants without an eviction call; key count does not decrease",
    }
    assert set(scenario_labels) <= {t["name"] for t in cloud["tests"]}
    tables = {
        "similarity": table(
            "Titan V2, 256 dimensions. Similarity to the canonical Atlas question.",
            ["Question", "Cosine similarity", "Semantic admission"],
            [(r["question"], f'{r["similarity_to_atlas"]:.3f}',
              r["admitted_contract"].split(":")[1].title() if r["admitted_contract"] else "None")
             for r in data("admission-probe")["rows"][1:]], numeric=(1,)),
        "latency": table(
            f"{per_mode} sequential requests per mode. Measured inside the Lambda query service.",
            ["Mode", "Generation calls", "Exact hits", "Semantic hits", "Median ms", "p95 ms"],
            [(names[k], a["generation_calls"], a["exact_hits"], a["semantic_hits"],
              f'{a["median_server_ms"]:.1f}', f'{a["p95_server_ms"]:.1f}')
             for k, a in arms.items()], numeric=(1, 2, 3, 4, 5)),
        "usage": table(
            "Totals for the same requests. Ingestion and diagnostic calls are excluded.",
            ["Mode", "Input tokens", "Output tokens", "Embedding tokens", "DynamoDB RCU", "S3 reads"],
            [(names[k], f'{a["input_tokens"]:,}', a["output_tokens"], a["embedding_tokens"],
              f'{a["authority_rcu"]:g}', a["document_reads"]) for k, a in arms.items()],
            numeric=(1, 2, 3, 4, 5)),
        "tests": table("Saved AWS scenario results · results/cloud-tests.json",
                       ["Scenario", "Result"],
                       [(scenario_labels.get(t["name"], t["name"]), "PASS" if t["passed"] else "FAIL")
                        for t in cloud["tests"]]),
    }
    values = {
        "local_tests": local["passed"], "cloud_tests": len(cloud["tests"]),
        "requests_per_mode": per_mode, "requests_total": sum(a["requests"] for a in arms.values()),
        "generation_none": arms["none"]["generation_calls"],
        "generation_semantic": arms["semantic"]["generation_calls"],
        "avoided": f'{100 * (1 - arms["semantic"]["generation_calls"] / arms["none"]["generation_calls"]):.2f}',
        "median_none": f'{arms["none"]["median_server_ms"]:.0f}',
        "median_semantic": f'{arms["semantic"]["median_server_ms"]:.0f}',
        "p95_semantic": f'{arms["semantic"]["p95_server_ms"]:.1f}',
        **{f"runtime_{k}": runtime[v] for k, v in {
            "python": "python", "boto3": "boto3", "valkey": "valkey_client", "engine": "engine"}.items()},
        "cleanup_checks": len(cleanup["checks"]),
        "cleanup_time": cleanup["verified_at"].replace("T", " ").split(".")[0] + " UTC",
    }
    replacements = {**{f"code:{k}": v for k, v in codes.items()},
                    **{f"table:{k}": v for k, v in tables.items()},
                    **{f"value:{k}": escape(str(v)) for k, v in values.items()},
                    **{f"figure:{k}": figure(k) for k in FIGURES}}
    template = read("docs/article/body.html")
    used = re.findall(r"\{\{([^{}]+)}}", template)
    assert set(used) == set(replacements), "Unused or missing article component"
    body = re.sub(r"\{\{([^{}]+)}}", lambda m: replacements[m[1]], template)
    title = "Building Permission-Aware RAG Caching on AWS"
    html = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="author" content="bcfmtolgahan">'
        '<meta name="description" content="A tested AWS implementation of permission-aware RAG answer caching '
        'with Bedrock, ElastiCache for Valkey, DynamoDB, Terraform and controlled revocation races.">'
        f"<title>{title}</title><style>\n{read('docs/article/style.css')}\n</style></head>"
        f"<body><main><article>{body}</article></main><script>{COPY_SCRIPT}</script></body></html>\n"
    )
    parser, word_count = validate(html)
    report = {
        "article": "ARTICLE.html", "title": title, "language": "en",
        "prose_word_count_including_tables": word_count,
        "code_blocks": len(parser.codes), "source_excerpts": SNIPPETS,
        "embedded_official_icon_diagrams": parser.images,
        "headings": parser.headings, "raw_benchmark_recomputed": True,
        "local_links_verified": True, "source_excerpts_verified": True,
        "browser_render_checked": False,
        "article_sha256": hashlib.sha256(html.encode()).hexdigest(),
    }
    return html, json.dumps(report, indent=2, ensure_ascii=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    html, report = build()
    outputs = {"ARTICLE.html": html, "docs/article/validation.json": report}
    for path, contents in outputs.items():
        if args.check:
            if read(path) != contents:
                raise SystemExit(f"Stale generated article artifact: {path}")
        else:
            (ROOT / path).write_text(contents)
    details = json.loads(report)
    print(json.dumps({key: details[key] for key in (
        "article", "prose_word_count_including_tables", "code_blocks",
        "embedded_official_icon_diagrams", "raw_benchmark_recomputed", "local_links_verified")},
        indent=2))


if __name__ == "__main__":
    main()
