from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class QAPair:
    question: str
    expected_answer: str
    context: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    retrieved_contexts: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    qa_pair: QAPair
    actual_answer: str
    faithfulness: float
    relevance: float
    completeness: float
    passed: bool
    failure_type: str | None = None
    context_precision: float | None = None
    context_recall: float | None = None

    def overall_score(self) -> float:
        return (self.faithfulness + self.relevance + self.completeness) / 3.0


STOPWORDS: set[str] = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "to", "for", "with", "as", "by", "and", "or",
    "it", "its", "this", "that", "these", "those", "from", "into", "than",
}


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    tokens = re.findall(r"\b\w+\b", text.lower())
    return {token for token in tokens if token not in STOPWORDS}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


class RAGASEvaluator:
    def evaluate_faithfulness(self, answer: str, context: str) -> float:
        answer_tokens = _tokenize(answer)
        if not answer_tokens:
            return 1.0
        context_tokens = _tokenize(context)
        return _clamp01(len(answer_tokens & context_tokens) / len(answer_tokens))

    def evaluate_relevance(self, answer: str, question: str) -> float:
        question_tokens = _tokenize(question)
        if not question_tokens:
            return 1.0
        answer_tokens = _tokenize(answer)
        return _clamp01(len(answer_tokens & question_tokens) / len(question_tokens))

    def evaluate_completeness(self, answer: str, expected: str) -> float:
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        answer_tokens = _tokenize(answer)
        return _clamp01(len(answer_tokens & expected_tokens) / len(expected_tokens))

    def evaluate_context_recall(self, contexts: list[str], expected: str) -> float:
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        union_tokens: set[str] = set()
        for chunk in contexts:
            union_tokens.update(_tokenize(chunk))
        return _clamp01(len(expected_tokens & union_tokens) / len(expected_tokens))

    def evaluate_context_precision(
        self,
        contexts: list[str],
        expected: str,
        relevance_threshold: float = 0.1,
    ) -> float:
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        if not contexts:
            return 0.0

        relevant_flags: list[bool] = []
        for chunk in contexts:
            chunk_tokens = _tokenize(chunk)
            coverage = len(chunk_tokens & expected_tokens) / len(expected_tokens)
            relevant_flags.append(coverage >= relevance_threshold)

        total_relevant = sum(relevant_flags)
        if total_relevant == 0:
            return 0.0

        relevant_seen = 0
        ap_sum = 0.0
        for rank, is_relevant in enumerate(relevant_flags, start=1):
            if is_relevant:
                relevant_seen += 1
                ap_sum += relevant_seen / rank
        return _clamp01(ap_sum / total_relevant)

    def run_full_eval(
        self,
        answer: str,
        question: str,
        context: str,
        expected: str,
        contexts: list[str] | None = None,
    ) -> EvalResult:
        faithfulness = self.evaluate_faithfulness(answer, context)
        relevance = self.evaluate_relevance(answer, question)
        completeness = self.evaluate_completeness(answer, expected)
        passed = faithfulness >= 0.5 and relevance >= 0.5 and completeness >= 0.5

        failure_type: str | None = None
        if not passed:
            if faithfulness < 0.3:
                failure_type = "hallucination"
            elif relevance < 0.3:
                failure_type = "irrelevant"
            elif completeness < 0.3:
                failure_type = "incomplete"
            else:
                failure_type = "off_topic"

        context_recall: float | None = None
        context_precision: float | None = None
        if contexts is not None:
            context_recall = self.evaluate_context_recall(contexts, expected)
            context_precision = self.evaluate_context_precision(contexts, expected)

        return EvalResult(
            qa_pair=QAPair(
                question=question,
                expected_answer=expected,
                context=context,
                retrieved_contexts=[] if contexts is None else list(contexts),
            ),
            actual_answer=answer,
            faithfulness=faithfulness,
            relevance=relevance,
            completeness=completeness,
            passed=passed,
            failure_type=failure_type,
            context_precision=context_precision,
            context_recall=context_recall,
        )


def rerank_by_overlap(contexts: list[str], query: str) -> list[str]:
    query_tokens = _tokenize(query)
    return sorted(
        contexts,
        key=lambda chunk: len(_tokenize(chunk) & query_tokens),
        reverse=True,
    )


class LLMJudge:
    def __init__(self, judge_llm_fn: Callable[[str], str]) -> None:
        self.judge_llm_fn = judge_llm_fn

    def score_response(
        self,
        question: str,
        answer: str,
        rubric: dict[str, Any],
    ) -> dict[str, Any]:
        rubric_text = "\n".join(
            f"- {name}: {description}" for name, description in rubric.items()
        )
        prompt = (
            "You are an impartial evaluator. Score the answer for every rubric "
            "criterion from 0.0 to 1.0. Return only JSON.\n\n"
            f"Question:\n{question}\n\n"
            f"Answer:\n{answer}\n\n"
            f"Rubric:\n{rubric_text}\n"
        )
        raw = self.judge_llm_fn(prompt)
        scores = {name: 0.5 for name in rubric}

        try:
            parsed = json.loads(raw)
            candidate = parsed.get("scores", parsed) if isinstance(parsed, dict) else {}
            if isinstance(candidate, dict):
                for name in rubric:
                    value = candidate.get(name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        numeric = float(value)
                        if 1.0 < numeric <= 5.0:
                            numeric /= 5.0
                        scores[name] = _clamp01(numeric)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        return {"scores": scores, "reasoning": raw}

    def detect_bias(self, scores_batch: list[dict[str, Any]]) -> dict[str, Any]:
        item_averages: list[float] = []
        all_scores: list[float] = []

        for item in scores_batch:
            score_map = item.get("scores", {}) if isinstance(item, dict) else {}
            values = [
                float(value)
                for value in score_map.values()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
            if values:
                item_averages.append(sum(values) / len(values))
                all_scores.extend(values)

        overall_average = sum(all_scores) / len(all_scores) if all_scores else 0.5
        positional_bias = False
        if len(item_averages) >= 2:
            later_average = sum(item_averages[1:]) / len(item_averages[1:])
            positional_bias = item_averages[0] > later_average + 0.1

        return {
            "positional_bias": positional_bias,
            "leniency_bias": bool(all_scores) and overall_average > 0.8,
            "severity_bias": bool(all_scores) and overall_average < 0.3,
        }


class BenchmarkRunner:
    def run(
        self,
        qa_pairs: list[QAPair],
        agent_fn: Callable[[str], str],
        evaluator: RAGASEvaluator,
    ) -> list[EvalResult]:
        results: list[EvalResult] = []
        for pair in qa_pairs:
            answer = agent_fn(pair.question)
            result = evaluator.run_full_eval(
                answer=answer,
                question=pair.question,
                context=pair.context or "",
                expected=pair.expected_answer,
                contexts=pair.retrieved_contexts,
            )
            result.qa_pair = pair
            results.append(result)
        return results

    def generate_report(self, results: list[EvalResult]) -> dict[str, Any]:
        total = len(results)
        if total == 0:
            return {
                "total": 0,
                "passed": 0,
                "pass_rate": 0.0,
                "avg_faithfulness": 0.0,
                "avg_relevance": 0.0,
                "avg_completeness": 0.0,
                "avg_context_recall": None,
                "avg_context_precision": None,
                "failure_types": {},
            }

        passed_count = sum(result.passed for result in results)
        failure_types: dict[str, int] = {}
        for result in results:
            if not result.passed:
                key = result.failure_type or "unknown"
                failure_types[key] = failure_types.get(key, 0) + 1

        recalls = [
            result.context_recall for result in results if result.context_recall is not None
        ]
        precisions = [
            result.context_precision
            for result in results
            if result.context_precision is not None
        ]

        return {
            "total": total,
            "passed": passed_count,
            "pass_rate": passed_count / total,
            "avg_faithfulness": sum(result.faithfulness for result in results) / total,
            "avg_relevance": sum(result.relevance for result in results) / total,
            "avg_completeness": sum(result.completeness for result in results) / total,
            "avg_context_recall": sum(recalls) / len(recalls) if recalls else None,
            "avg_context_precision": (
                sum(precisions) / len(precisions) if precisions else None
            ),
            "failure_types": failure_types,
        }

    def run_regression(
        self,
        new_results: list[EvalResult],
        baseline_results: list[EvalResult],
    ) -> dict[str, Any]:
        def average(values: list[EvalResult], attribute: str) -> float:
            if not values:
                return 0.0
            return sum(float(getattr(result, attribute)) for result in values) / len(values)

        metrics = ("faithfulness", "relevance", "completeness")
        new_averages = {metric: average(new_results, metric) for metric in metrics}
        baseline_averages = {
            metric: average(baseline_results, metric) for metric in metrics
        }
        regressions = [
            metric
            for metric in metrics
            if baseline_averages[metric] - new_averages[metric] > 0.05
        ]

        return {
            "new_avg_faithfulness": new_averages["faithfulness"],
            "new_avg_relevance": new_averages["relevance"],
            "new_avg_completeness": new_averages["completeness"],
            "baseline_avg_faithfulness": baseline_averages["faithfulness"],
            "baseline_avg_relevance": baseline_averages["relevance"],
            "baseline_avg_completeness": baseline_averages["completeness"],
            "regressions": regressions,
            "passed": not regressions,
        }

    def identify_failures(
        self,
        results: list[EvalResult],
        threshold: float = 0.5,
    ) -> list[EvalResult]:
        return [
            result
            for result in results
            if (
                result.faithfulness < threshold
                or result.relevance < threshold
                or result.completeness < threshold
            )
        ]


class FailureAnalyzer:
    def categorize_failures(self, failures: list[EvalResult]) -> dict[str, int]:
        categories: dict[str, int] = {}
        for failure in failures:
            key = failure.failure_type or "unknown"
            categories[key] = categories.get(key, 0) + 1
        return categories

    def find_root_cause(self, failure: EvalResult) -> str:
        scores = {
            "faithfulness": failure.faithfulness,
            "relevance": failure.relevance,
            "completeness": failure.completeness,
        }
        minimum = min(scores.values())
        lowest = [name for name, score in scores.items() if score == minimum]

        if len(lowest) != 1:
            return "Multiple issues detected - review full pipeline"
        if lowest[0] == "faithfulness":
            return "Context is missing or irrelevant - improve retrieval"
        if lowest[0] == "relevance":
            return "Answer does not address the question - improve prompt clarity"
        if lowest[0] == "completeness":
            return (
                "Answer is missing key information - increase context window "
                "or improve generation"
            )
        return "Multiple issues detected - review full pipeline"

    @staticmethod
    def _escape_markdown_cell(value: Any) -> str:
        return str(value).replace("|", r"\|").replace("\n", " ").strip()

    def generate_improvement_log(
        self, failures: list[EvalResult], suggestions: list[str]
    ) -> str:
        rows = [
            "| Failure ID | Type | Root Cause | Suggested Fix | Status |",
            "|------------|------|------------|---------------|--------|",
        ]
        for index, failure in enumerate(failures, start=1):
            suggestion = (
                suggestions[index - 1]
                if index - 1 < len(suggestions)
                else "Review the trace and improve the weakest pipeline stage"
            )
            rows.append(
                "| {failure_id} | {failure_type} | {root_cause} | {suggestion} | Open |".format(
                    failure_id=f"F{index:03d}",
                    failure_type=self._escape_markdown_cell(
                        failure.failure_type or "unknown"
                    ),
                    root_cause=self._escape_markdown_cell(self.find_root_cause(failure)),
                    suggestion=self._escape_markdown_cell(suggestion),
                )
            )
        return "\n".join(rows)

    def generate_improvement_suggestions(
        self, failures: list[EvalResult]
    ) -> list[str]:
        if not failures:
            return []

        categories = {
            key.casefold(): value for key, value in self.categorize_failures(failures).items()
        }
        suggestions: list[str] = []

        if categories.get("hallucination", 0):
            suggestions.append(
                "Add grounding checks and require every claim to be supported by retrieved context"
            )
        if categories.get("irrelevant", 0) or categories.get("off_topic", 0):
            suggestions.append(
                "Improve query routing and prompt instructions so answers address the user question"
            )
        if categories.get("incomplete", 0) or categories.get("low_completeness", 0):
            suggestions.append(
                "Increase retrieval coverage or context window and require complete answers"
            )
        if categories.get("refusal", 0):
            suggestions.append(
                "Calibrate guardrails with in-scope examples to reduce unnecessary refusals"
            )

        fallbacks = [
            "Inspect the lowest-scoring traces and add representative cases to the golden dataset",
            "Tune retrieval top-k or reranking to place supporting evidence earlier",
            "Add regression gates for faithfulness, relevance, and completeness before deployment",
        ]
        for suggestion in fallbacks:
            if len(suggestions) >= 3:
                break
            if suggestion not in suggestions:
                suggestions.append(suggestion)

        return suggestions
