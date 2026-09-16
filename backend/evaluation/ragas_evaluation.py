"""Offline RAG evaluation with Ragas.

    python -m evaluation.ragas_evaluation                      # full suite
    python -m evaluation.ragas_evaluation --category abstention
    python -m evaluation.ragas_evaluation --limit 10 --output results.json

What is measured
----------------
Ragas metrics (LLM-judged, on cases that should be answered):
  * faithfulness      — is every claim supported by the retrieved context?
                        This is the hallucination detector and the metric that
                        matters most for a policy assistant.
  * answer_relevancy  — does the answer address the question actually asked?
  * context_precision — are the retrieved chunks relevant, and ranked well?
                        Low precision means the reranker is underperforming.
  * context_recall    — did retrieval find everything the answer needed?
                        Low recall means chunking or the corpus is the problem.

Deterministic checks (no LLM judge, run on every case):
  * abstention accuracy — did the system decline exactly when it should have?
    A system that answers everything scores well on relevancy and is still
    dangerous, so this is reported separately and never averaged away.
  * RBAC leakage        — did a restricted document reach a role that may not
    read it? This is a hard pass/fail: any leak fails the whole run.
  * citation validity   — does every answer carry at least one real citation?
  * latency             — p50 / p95 per question.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.models import UserRole  # noqa: E402
from app.services.rag_pipeline import answer_question  # noqa: E402
from app.services.search_service import SearchFilters  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("ragas-eval")

settings = get_settings()
DATASET_PATH = Path(__file__).parent / "golden_dataset.json"


@dataclass
class CaseResult:
    """One evaluated question."""

    id: str
    question: str
    category: str
    department: str | None
    role: str
    expected_answer: bool
    actually_answered: bool
    answer: str
    ground_truth: str
    contexts: list[str] = field(default_factory=list)
    citation_count: int = 0
    confidence: float = 0.0
    latency_ms: int = 0
    retrieved_access_levels: list[str] = field(default_factory=list)
    rbac_violation: bool = False
    error: str | None = None

    @property
    def abstention_correct(self) -> bool:
        return self.actually_answered == self.expected_answer


async def run_case(session: AsyncSession, case: dict[str, Any]) -> CaseResult:
    """Execute one golden case through the production pipeline."""
    role = UserRole(case.get("role", "student"))
    started = time.perf_counter()

    result = CaseResult(
        id=case["id"],
        question=case["question"],
        category=case["category"],
        department=case.get("department"),
        role=role.value,
        expected_answer=case["should_answer"],
        actually_answered=False,
        answer="",
        ground_truth=case["ground_truth"],
    )

    try:
        pipeline = await answer_question(
            session,
            case["question"],
            role,
            filters=SearchFilters(),
            # Eval traffic must not pollute the production failed-search backlog.
            log_search=False,
        )
    except Exception as exc:
        logger.exception("Case %s crashed", case["id"])
        result.error = f"{type(exc).__name__}: {exc}"
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    result.actually_answered = pipeline.answered
    result.answer = pipeline.answer
    result.contexts = [chunk.content for chunk in pipeline.retrieved]
    result.citation_count = len(pipeline.citations)
    result.confidence = pipeline.confidence
    result.latency_ms = pipeline.latency_ms
    result.retrieved_access_levels = sorted({c.access_level.value for c in pipeline.retrieved})

    # RBAC probes assert that a restricted level never appears in what was retrieved.
    restricted = case.get("restricted_access_level")
    if restricted and restricted in result.retrieved_access_levels:
        result.rbac_violation = True
        logger.error(
            "RBAC VIOLATION on %s: role=%s retrieved access_level=%s",
            case["id"],
            role.value,
            restricted,
        )

    return result


def compute_deterministic_metrics(results: list[CaseResult]) -> dict[str, Any]:
    """Metrics that need no LLM judge, so they are always available."""
    total = len(results)
    if total == 0:
        return {}

    latencies = [r.latency_ms for r in results if r.latency_ms > 0]
    answered = [r for r in results if r.actually_answered]
    should_abstain = [r for r in results if not r.expected_answer]
    should_answer = [r for r in results if r.expected_answer]
    rbac_cases = [r for r in results if r.category == "rbac_probe"]

    return {
        "total_cases": total,
        "errors": sum(1 for r in results if r.error),
        "abstention": {
            "overall_accuracy": round(
                sum(r.abstention_correct for r in results) / total, 4
            ),
            "correctly_declined": sum(r.abstention_correct for r in should_abstain),
            "should_have_declined": len(should_abstain),
            "false_answers": sum(1 for r in should_abstain if r.actually_answered),
            "false_refusals": sum(1 for r in should_answer if not r.actually_answered),
        },
        "rbac": {
            "probe_count": len(rbac_cases),
            "violations": sum(1 for r in results if r.rbac_violation),
            "passed": not any(r.rbac_violation for r in results),
        },
        "citations": {
            "answers_with_citations": sum(1 for r in answered if r.citation_count > 0),
            "answers_total": len(answered),
            "mean_citations_per_answer": round(
                statistics.mean([r.citation_count for r in answered]), 2
            )
            if answered
            else 0.0,
        },
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1) if latencies else None,
            "p95": round(
                sorted(latencies)[int(len(latencies) * 0.95) - 1] if len(latencies) > 1 else latencies[0],
                1,
            )
            if latencies
            else None,
            "mean": round(statistics.mean(latencies), 1) if latencies else None,
            "max": max(latencies) if latencies else None,
        },
        "mean_confidence": round(statistics.mean([r.confidence for r in results]), 4),
    }


def run_ragas_metrics(results: list[CaseResult]) -> dict[str, float] | None:
    """Score answered cases with Ragas.

    Only cases the system chose to answer are scored: faithfulness and
    relevancy are undefined for a deliberate refusal, and including refusals
    would drag the averages down for behaviour we actually want.
    """
    scorable = [r for r in results if r.actually_answered and r.contexts and not r.error]
    if not scorable:
        logger.warning("No answered cases with context; skipping Ragas metrics")
        return None

    try:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import (
            answer_relevancy,
            context_precision,
            context_recall,
            faithfulness,
        )
    except ImportError:
        logger.warning("Ragas is not installed; skipping LLM-judged metrics")
        return None

    dataset = Dataset.from_dict(
        {
            "question": [r.question for r in scorable],
            "answer": [r.answer for r in scorable],
            "contexts": [r.contexts for r in scorable],
            "ground_truth": [r.ground_truth for r in scorable],
        }
    )

    logger.info("Running Ragas over %d answered cases…", len(scorable))
    try:
        scores = evaluate(
            dataset,
            metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
        )
    except Exception:
        logger.exception("Ragas evaluation failed")
        return None

    return {key: round(float(value), 4) for key, value in dict(scores).items()}


def print_report(deterministic: dict[str, Any], ragas_scores: dict[str, float] | None) -> None:
    line = "=" * 68
    print(f"\n{line}\nCAMPUS KNOWLEDGE ASSISTANT — EVALUATION REPORT\n{line}")

    print(f"\nCases run: {deterministic['total_cases']}  (errors: {deterministic['errors']})")

    if ragas_scores:
        print("\nRagas metrics (answered cases only)")
        print("-" * 68)
        for name, value in ragas_scores.items():
            bar = "#" * int(value * 30)
            print(f"  {name:22} {value:.4f}  {bar}")
    else:
        print("\nRagas metrics: skipped")

    abstention = deterministic["abstention"]
    print("\nAbstention behaviour")
    print("-" * 68)
    print(f"  overall accuracy       {abstention['overall_accuracy']:.4f}")
    print(f"  correctly declined     {abstention['correctly_declined']}/{abstention['should_have_declined']}")
    print(f"  answered when it should not (hallucination risk): {abstention['false_answers']}")
    print(f"  declined when it should have answered:            {abstention['false_refusals']}")

    rbac = deterministic["rbac"]
    status = "PASS" if rbac["passed"] else "FAIL"
    print(f"\nRBAC enforcement: {status}")
    print("-" * 68)
    print(f"  probes: {rbac['probe_count']}   violations: {rbac['violations']}")

    citations = deterministic["citations"]
    print("\nCitations")
    print("-" * 68)
    print(f"  answers carrying >=1 citation: {citations['answers_with_citations']}/{citations['answers_total']}")
    print(f"  mean citations per answer:     {citations['mean_citations_per_answer']}")

    latency = deterministic["latency_ms"]
    print("\nLatency (ms)")
    print("-" * 68)
    print(f"  p50 {latency['p50']}   p95 {latency['p95']}   mean {latency['mean']}   max {latency['max']}")
    print(f"\n{line}\n")


async def main_async(args: argparse.Namespace) -> int:
    dataset = json.loads(DATASET_PATH.read_text())
    cases = dataset["cases"]

    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    if args.department:
        cases = [c for c in cases if c.get("department") == args.department]
    if args.limit:
        cases = cases[: args.limit]

    if not cases:
        logger.error("No cases matched the given filters")
        return 1

    logger.info("Evaluating %d cases", len(cases))

    async_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(async_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    results: list[CaseResult] = []
    try:
        async with session_factory() as session:
            for index, case in enumerate(cases, start=1):
                logger.info("[%d/%d] %s — %s", index, len(cases), case["id"], case["question"][:70])
                results.append(await run_case(session, case))
    finally:
        await engine.dispose()

    deterministic = compute_deterministic_metrics(results)
    ragas_scores = None if args.skip_ragas else run_ragas_metrics(results)
    print_report(deterministic, ragas_scores)

    if args.output:
        payload = {
            "dataset_version": dataset.get("version"),
            "deterministic_metrics": deterministic,
            "ragas_metrics": ragas_scores,
            "cases": [asdict(r) for r in results],
        }
        Path(args.output).write_text(json.dumps(payload, indent=2))
        logger.info("Wrote detailed results to %s", args.output)

    # A single RBAC leak fails the run regardless of every other score — an
    # assistant that leaks restricted policy is not shippable at any accuracy.
    if not deterministic["rbac"]["passed"]:
        logger.error("FAILING: RBAC violations detected")
        return 2
    if deterministic["abstention"]["false_answers"] > args.max_false_answers:
        logger.error(
            "FAILING: %d cases answered that should have been declined (limit %d)",
            deterministic["abstention"]["false_answers"],
            args.max_false_answers,
        )
        return 3
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the RAG pipeline against the golden set.")
    parser.add_argument("--category", help="Only run cases in this category")
    parser.add_argument("--department", help="Only run cases for this department")
    parser.add_argument("--limit", type=int, help="Run at most N cases")
    parser.add_argument("--output", help="Write detailed JSON results to this path")
    parser.add_argument("--skip-ragas", action="store_true", help="Deterministic metrics only")
    parser.add_argument(
        "--max-false-answers",
        type=int,
        default=0,
        help="Allowed number of cases answered that should have been declined",
    )
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
