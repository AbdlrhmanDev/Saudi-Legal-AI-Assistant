"""Reproducible evaluation run (PRD section 15).

    python -m tests.evaluation.run_eval            # offline providers (hash + extractive)
    python -m tests.evaluation.run_eval --live     # providers from .env (e.g. OpenAI)

Rebuilds the TEST database, loads the fixture corpus, runs the simulated amendment
through sync -> approve, asks every gold question and writes measured results to
tests/evaluation/results/. Numbers in the report are what this run produced.
"""

import argparse
import json
import statistics
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
ANSWERABLE = ("direct", "paraphrase", "multi_article", "temporal", "adversarial")
TARGETS = {
    "recall_at_5": 0.90,
    "citation_accuracy": 0.95,
    "auto_checked_correctness": 0.90,
    "abstention": 1.0,
    "temporal": 1.0,
}


def run(live: bool = False) -> dict:
    from tests._env import configure, reset_schema

    configure(offline_models=not live)

    from sqlalchemy import select

    from legal_core.answer import answer_question
    from legal_core.cli import seed_fixtures, seed_sources
    from legal_core.config import get_settings
    from legal_core.db import get_sessionmaker
    from legal_core.embeddings import get_embedder
    from legal_core.jobs import enqueue, run_job
    from legal_core.llm import get_answer_model
    from legal_core.models import Article, DocumentVersion, LegalInstrument, QueryTrace, UpdateCandidate
    from legal_core.review import approve_candidate

    gold = json.loads((HERE / "gold.json").read_text(encoding="utf-8"))
    settings = get_settings()
    embedder, llm = get_embedder(settings), get_answer_model(settings)
    factory = get_sessionmaker()

    reset_schema()
    with factory() as db:
        seed_sources(db, settings)
        seed_fixtures(db, settings)
        job = enqueue(db, "feed_sync", {"source_id": None, "force": True})
        db.commit()
        run_job(factory, job.id, settings, embedder)
        candidate = db.scalars(select(UpdateCandidate).where(UpdateCandidate.external_id == "fixture-decision-1001")).one()
        approve_candidate(
            db, candidate.id, actor_id=None, effective_from=date(2026, 11, 1),
            effective_date_evidence="Fixture decision 1001 (test data).", embedder=embedder, settings=settings,
        )  # fmt: skip
        db.commit()

        def law_key(title: str) -> str | None:
            return next((key for key, prefix in gold["laws"].items() if title.startswith(prefix)), None)

        article_ref: dict[str, tuple[str | None, str | None]] = {
            str(article_id): (law_key(title), number)
            for article_id, number, title in db.execute(
                select(Article.id, Article.article_number, LegalInstrument.official_title_ar)
                .join(DocumentVersion, Article.version_id == DocumentVersion.id)
                .join(LegalInstrument, DocumentVersion.instrument_id == LegalInstrument.id)
            )
        }

        results = []
        for case in gold["cases"]:
            as_of = date.fromisoformat(case.get("as_of", gold["default_as_of"]))
            answer = answer_question(
                db, query=case["query"], domain=case.get("domain"), as_of_date=as_of,
                settings=settings, embedder=embedder, llm=llm,
            )  # fmt: skip
            trace = db.get(QueryTrace, uuid.UUID(answer["trace_id"]))
            results.append(_score(case, answer, trace, article_ref))

    return _report(gold, results, embedder.model_name, llm.model_name, live)


def _score(case: dict, answer: dict, trace, article_ref: dict) -> dict:
    gold = {tuple(g) for g in case["gold"]}
    retrieved = [article_ref[r["article_id"]] for r in trace.retrieved[:5]]
    cited = [article_ref[c["article_id"]] for c in answer["citations"]]
    text = answer["answer_ar"]
    status = answer["answer_status"]
    problems: list[str] = []

    if case["class"] == "out_of_corpus" or case.get("expect_abstain"):
        if status != "insufficient_evidence":
            problems.append(f"expected abstention, got {status}")
        if cited:
            problems.append("abstention carried citations")
    elif case.get("expect_not_grounded"):
        if status == "grounded":
            problems.append("ambiguous question answered as grounded")
    elif case.get("allow_abstain") and status == "insufficient_evidence":
        if cited:
            problems.append("abstention carried citations")
    else:
        if status == "insufficient_evidence":
            problems.append("abstained on an answerable question")
        if not gold & set(retrieved):
            problems.append("gold article not in top 5")
        wrong = [c for c in cited if c not in gold]
        if wrong:
            problems.append(f"citation outside gold set: {wrong}")
        if cited and not gold & set(cited):
            problems.append("no gold article cited")
        for needle in case.get("must_contain", []):
            if needle not in text:
                problems.append(f"missing expected text: {needle}")
        expected_version = case.get("version_effective_from")
        if expected_version and any(c["effective_from"] != expected_version for c in answer["citations"]):
            problems.append("cited the wrong version for the as-of date")
        if case.get("expect_forthcoming") and not answer["forthcoming"]:
            problems.append("scheduled version was not announced as forthcoming")
    for needle in case.get("must_not_contain", []):
        if needle in text:
            problems.append(f"forbidden text present: {needle}")

    return {
        "id": case["id"],
        "class": case["class"],
        "query": case["query"],
        "as_of_date": answer["as_of_date"],
        "answer_status": status,
        "gold": sorted(gold),
        "retrieved_top5": retrieved,
        "cited": cited,
        "gold_recalled": len(gold & set(retrieved)),
        "citations_correct": sum(1 for c in cited if c in gold),
        "latency_ms": trace.latency_ms,
        "passed": not problems,
        "problems": problems,
        "trace_id": answer["trace_id"],
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _report(gold: dict, results: list[dict], embedding_model: str, llm_model: str, live: bool) -> dict:
    with_gold = [r for r in results if r["gold"]]
    abstain = [r for r in results if r["class"] == "out_of_corpus"]
    temporal = [r for r in results if r["class"] == "temporal"]
    latencies = sorted(r["latency_ms"] for r in results)
    by_class: dict[str, dict] = {}
    for r in results:
        entry = by_class.setdefault(r["class"], {"total": 0, "passed": 0})
        entry["total"] += 1
        entry["passed"] += int(r["passed"])

    metrics = {
        "recall_at_5": _ratio(sum(r["gold_recalled"] for r in with_gold), sum(len(r["gold"]) for r in with_gold)),
        "citation_accuracy": _ratio(sum(r["citations_correct"] for r in with_gold), sum(len(r["cited"]) for r in with_gold)),
        "auto_checked_correctness": _ratio(sum(r["passed"] for r in with_gold), len(with_gold)),
        "abstention": _ratio(sum(r["passed"] for r in abstain), len(abstain)),
        "temporal": _ratio(sum(r["passed"] for r in temporal), len(temporal)),
    }
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "corpus": "FICTIONAL fixture corpus (test data) - not the official pilot corpus",
        "providers": {"embedding_model": embedding_model, "answer_model": llm_model, "mode": "live" if live else "offline"},
        "dataset": {"total": len(results), "by_class": by_class},
        "metrics": metrics,
        "targets": TARGETS,
        "targets_met": {k: (metrics[k] is not None and metrics[k] >= v) for k, v in TARGETS.items()},
        "latency_ms": {
            "p50": statistics.median(latencies),
            "p95": latencies[max(0, round(0.95 * len(latencies)) - 1)],
            "max": latencies[-1],
        },
        "failures": [r for r in results if not r["passed"]],
        "cases": results,
        "notes": [
            "auto_checked_correctness is a string/citation check, not the human grading required by PRD 15.2.",
            "Offline mode uses a hashing embedder and a verbatim-quote answer model; it measures the pipeline "
            "(filters, lexical retrieval, gating, citations, temporal logic), not LLM answer quality.",
            "Prompt-injection resistance of a generative model must be measured with --live.",
        ],
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (RESULTS / "report.md").write_text(_markdown(report), encoding="utf-8")
    return report


def _markdown(report: dict) -> str:
    m, t, met = report["metrics"], report["targets"], report["targets_met"]
    lines = [
        "# Evaluation report (measured)",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Corpus: {report['corpus']}",
        f"- Embeddings: `{report['providers']['embedding_model']}` · Answer model: `{report['providers']['answer_model']}`"
        f" · Mode: {report['providers']['mode']}",
        f"- Cases: {report['dataset']['total']}",
        "",
        "| Metric | Measured | PRD target | Met |",
        "|---|---|---|---|",
    ]
    labels = {
        "recall_at_5": "Retrieval Recall@5",
        "citation_accuracy": "Citation accuracy",
        "auto_checked_correctness": "Auto-checked correctness (proxy)",
        "abstention": "Abstention on out-of-corpus",
        "temporal": "Effective-date scenarios",
    }
    for key, label in labels.items():
        value = "n/a" if m[key] is None else f"{m[key]:.1%}"
        lines.append(f"| {label} | {value} | ≥{t[key]:.0%} | {'yes' if met[key] else 'NO'} |")
    lat = report["latency_ms"]
    lines += ["", f"Latency (ms): p50 {lat['p50']}, p95 {lat['p95']}, max {lat['max']}.", "", "| Class | Passed | Total |", "|---|---|---|"]
    lines += [f"| {name} | {c['passed']} | {c['total']} |" for name, c in report["dataset"]["by_class"].items()]
    lines += ["", "## Failures", ""]
    if not report["failures"]:
        lines.append("None.")
    for failure in report["failures"]:
        lines.append(f"- **{failure['id']}** ({failure['class']}) `{failure['query']}` → {failure['answer_status']}: " + "; ".join(failure["problems"]))
    lines += ["", "## Notes", ""] + [f"- {note}" for note in report["notes"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="use the providers configured in .env instead of offline ones")
    report = run(parser.parse_args().live)
    print(_markdown(report))
    hard = ("abstention", "temporal")  # PRD hard guardrails
    return 0 if all(report["targets_met"][k] for k in hard) else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
