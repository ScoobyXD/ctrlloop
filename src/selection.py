"""Choice elimination with byte-budgeted batches."""

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass, field
import logging
import math
from time import perf_counter_ns
from typing import Any, TYPE_CHECKING

import httpx2
from typesafe_sdk import Choice, SystemOneResponse, TypeSafeAPIError, TypeSafeError
from typesafe_sdk._core.config import Config
from typesafe_sdk._core.endpoints import prepare_system_one

if __package__:
    from .jev_questions import ACTION, QUESTION, TIMER
else:
    from jev_questions import ACTION, QUESTION, TIMER

if TYPE_CHECKING:
    from src.reporting import RunReport


PINNED_MODEL = "jev-1.13.0"
# Jev 1.13.0 limits and an optimistic, model-specific wire-byte estimate.
# These are independent context limits, not allocations between state/questions.
MAX_CHOICE_OPTIONS = 255
BYTES_PER_INPUT_TOKEN = 2.05
QUESTION_BUDGET_BYTES = round(32_000 * BYTES_PER_INPUT_TOKEN)
REQUEST_BUDGET_BYTES = round(64_000 * BYTES_PER_INPUT_TOKEN)
SETTLING_MS = {
    "<50ms": 50,
    "100ms": 100,
    "200ms": 200,
    "300ms": 300,
    "400ms": 400,
    ">500ms": 500,
}
logger = logging.getLogger(__name__)


class SelectionError(Exception):
    """Jev rejected a complete round or its response cannot be used safely."""


def _context_rejected(error: TypeSafeError) -> bool:
    """Recognize only the service's observed token-limit rejection contract."""
    return (
        isinstance(error, TypeSafeAPIError)
        and error.status == 400
        and isinstance(error.body, Mapping)
        and isinstance(error.body.get("detail"), Mapping)
        and error.body["detail"].get("error_type") == "max_tokens_exceeded"
    )


@dataclass(frozen=True)
class Candidate:
    key: str
    description: dict[str, Any]
    record: dict[str, Any]


@dataclass
class Selection:
    winner: Candidate | None
    rounds: list[dict[str, Any]] = field(default_factory=list)
    settling_ms: int | None = None
    action: str | None = None


def _probability(value: Any) -> bool:
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _request_body_size(
    state: str | dict[str, Any], questions: dict[str, Choice], model: str,
) -> int:
    """Count the encoded body with the pinned SDK's own request builder.

    SDK 0.7.1 has no public preparation API. Isolate its internal builder here
    instead of duplicating the request schema or serializer. This sends no HTTP
    request; system_one receives the same state, questions, and model to send.
    """
    config = Config(
        api_key="local-sizing-only", base_url="https://api.typesafe.ai",
        default_model=model, timeout=30.0, default_headers=httpx2.Headers(),
    )
    try:
        prepared = prepare_system_one(
            config, state, questions, model, None, None, None, SystemOneResponse,
        )
    except TypeSafeError as error:
        # Local preparation must retain judge's sanitized error contract too.
        status = getattr(error, "status", None)
        safe_status = status if type(status) is int else "unavailable"
        raise SelectionError(
            f"TypeSafe request failed: {type(error).__name__} (HTTP {safe_status})."
        ) from None
    assert prepared.content is not None
    return len(prepared.content)


class ChoiceSelector:
    def __init__(
        self,
        client: Any,
        *,
        model: str = PINNED_MODEL,
    ) -> None:
        if model != PINNED_MODEL:
            raise ValueError(f"This experiment requires {PINNED_MODEL}.")
        self.client = client
        self.model = model
        # Kept for this selector's ongoing run, never calibrated through API calls.
        self.question_budget_bytes = QUESTION_BUDGET_BYTES
        self.request_budget_bytes = REQUEST_BUDGET_BYTES

    def judge(
        self, state: str | dict[str, Any], questions: dict[str, Choice], *,
        report: "RunReport | None" = None,
    ) -> tuple[dict[str, str], dict]:
        """Attempt one batch; return explicit context rejection for the retry loop."""
        body_bytes = _request_body_size(state, questions, self.model)
        single_sizes = [
            _request_body_size(state, {key: question}, self.model)
            for key, question in questions.items()
        ]
        if any(size > QUESTION_BUDGET_BYTES for size in single_sizes):
            raise SelectionError("A complete question with shared state exceeds the Jev question byte allowance.")
        # An aggregate rejection must not make an otherwise fitting standalone
        # question impossible to try. Each still obeys the independent 32k estimate.
        request_budget = max(self.request_budget_bytes, max(single_sizes, default=0))
        if body_bytes > request_budget:
            raise SelectionError("Jev request exceeds the current request byte allowance.")
        trace = {
            "state": deepcopy(state),
            "question_keys": list(questions),
            "question_count": len(questions),
            "questions": {
                key: {"type": question.type, "instructions": deepcopy(question.instructions),
                      "criteria": deepcopy(question.criteria)}
                for key, question in questions.items()
            },
            "answers": {}, "requested_model": self.model,
            "body_bytes": body_bytes, "request_budget_bytes": request_budget,
            "question_budget_bytes": max(self.question_budget_bytes, max(single_sizes, default=0)),
            "target_question_budget_bytes": self.question_budget_bytes,
            "input_tokens": None, "output_tokens": None, "input_cost_usd": None,
            "input_price_per_million_usd": 0.042,
            "outcome": "error", "context_rejection": False,
        }
        started_ns = perf_counter_ns()
        try:
            # SDK time includes request encoding and response decoding. The
            # report's HTTP timing separately measures the client-observed call.
            with report.span(
                "selection.sdk_call", question_keys=list(questions),
                question_count=len(questions), body_bytes=body_bytes,
                request_budget_bytes=request_budget,
                question_budget_bytes=trace["question_budget_bytes"],
                target_question_budget_bytes=self.question_budget_bytes,
            ) if report else nullcontext({}) as sdk_timing:
                if report:
                    trace["sdk_span_id"] = sdk_timing["span_id"]
                response = self.client.system_one(
                    state=state, questions=questions, model=self.model
                )
            with report.span("selection.response_validation") if report else nullcontext({}) as validation_timing:
                choices, validated = self._validate_response(questions, response)
                trace.update(validated, outcome="ok")
                if report:
                    trace["response_validation_span_id"] = validation_timing["span_id"]
            return choices, trace
        except TypeSafeError as error:
            status = getattr(error, "status", None)
            trace.update(status_code=status if type(status) is int else None,
                         error_type=type(error).__name__)
            if _context_rejected(error):
                trace.update(outcome="context_rejected", context_rejection=True,
                             service_error_type="max_tokens_exceeded")
                usage = error.body.get("usage")
                if isinstance(usage, Mapping):
                    for name in ("input_tokens", "output_tokens"):
                        value = usage.get(name)
                        if type(value) is int and value >= 0:
                            trace[name] = value
                    if trace["input_tokens"] is not None:
                        trace["input_cost_usd"] = trace["input_tokens"] * 0.042 / 1_000_000
                return {}, trace
            # Do not expose raw bodies or reinterpret unrelated HTTP failures.
            safe_status = status if type(status) is int else "unavailable"
            if status in (413, 422):
                raise SelectionError(
                    f"Jev rejected the request batch (HTTP {safe_status}); verify "
                    "the service request limits and question format."
                ) from None
            raise SelectionError(
                f"TypeSafe request failed: {type(error).__name__} (HTTP {safe_status})."
            ) from None
        finally:
            trace["duration_ms"] = (perf_counter_ns() - started_ns) / 1_000_000
            if report:
                # Also durable when an atomic failure prevents round advancement.
                # Exact payloads already live in the existing HTTP capture hooks.
                report.append("selection_attempt", **{
                    key: value for key, value in trace.items()
                    if key not in {"state", "questions", "answers"}
                })

    def _group_candidates(
        self, state: str | dict[str, Any], candidates: list[Candidate], round_id: str,
        *, question_budget_bytes: int | None = None,
    ) -> tuple[dict[str, list[Candidate]], dict[str, Choice]]:
        """Propose up to the service's 255 options; shrink only on byte overflow."""
        budget = self.question_budget_bytes if question_budget_bytes is None else question_budget_bytes
        groups = {}
        questions = {}
        start = 0
        while start < len(candidates):
            key = f"{round_id}_g{len(groups)}"

            def question_for(count: int) -> Choice:
                return Choice(
                    instructions=QUESTION,
                    criteria={
                        candidate.key: candidate.description
                        for candidate in candidates[start:start + count]
                    },
                )

            count = min(MAX_CHOICE_OPTIONS, len(candidates) - start)
            question = question_for(count)
            if _request_body_size(state, {key: question}, self.model) > budget:
                low, high = 1, count - 1
                count = 0
                while low <= high:
                    middle = (low + high) // 2
                    proposed = question_for(middle)
                    size = _request_body_size(state, {key: proposed}, self.model)
                    if size <= budget:
                        count, question = middle, proposed
                        low = middle + 1
                    else:
                        high = middle - 1
                if not count:
                    # A sibling's rejection cannot prove this indivisible
                    # candidate is too large. Try it under the original estimate;
                    # an explicit rejection of that singleton is terminal.
                    count, question = 1, question_for(1)
                    if _request_body_size(state, {key: question}, self.model) > QUESTION_BUDGET_BYTES:
                        raise SelectionError(
                            "A single candidate question with shared state exceeds the "
                            f"{QUESTION_BUDGET_BYTES}-byte initial question allowance."
                        )
            if count == 1 and start + 1 < len(candidates) and question_budget_bytes is None:
                # Two is the minimum meaningful comparison. A smaller sibling's
                # rejection is not evidence that this pair cannot fit. Never use
                # this fallback while splitting an explicitly rejected question.
                pair = question_for(2)
                if _request_body_size(state, {key: pair}, self.model) <= QUESTION_BUDGET_BYTES:
                    count, question = 2, pair
            groups[key] = candidates[start:start + count]
            questions[key] = question
            start += count

        return groups, questions

    def _pack_questions(
        self, state: str | dict[str, Any], questions: dict[str, Choice],
        *, request_budget_bytes: int | None = None,
    ) -> list[tuple[dict[str, Choice], int]]:
        """Measure the full round first, then find the largest fitting prefixes."""
        items = list(questions.items())
        single_sizes = [
            _request_body_size(state, {key: question}, self.model)
            for key, question in items
        ]
        if any(size > QUESTION_BUDGET_BYTES for size in single_sizes):
            raise SelectionError(
                "A complete question with shared state exceeds the Jev question byte allowance; "
                "the required action or settling question cannot be split safely."
            )
        request_budget = (min(self.request_budget_bytes, request_budget_bytes)
                          if request_budget_bytes is not None else self.request_budget_bytes)
        batches = []
        start = 0
        while start < len(items):
            remaining = dict(items[start:])
            remaining_bytes = _request_body_size(state, remaining, self.model)
            if remaining_bytes <= request_budget:
                batches.append((remaining, remaining_bytes))
                break

            # Search complete question prefixes; each Choice keeps its
            # entire byte-sized group.
            low, high = 1, len(items) - start - 1
            count, body_bytes = 0, 0
            while low <= high:
                middle = (low + high) // 2
                size = _request_body_size(state, dict(items[start:start + middle]), self.model)
                if size <= request_budget:
                    count, body_bytes = middle, size
                    low = middle + 1
                else:
                    high = middle - 1
            if not count:
                # A lowered aggregate target may be smaller than an indivisible
                # question. Isolate it under the independently checked question
                # limit; do not lend its larger allowance to unrelated batches.
                count, body_bytes = 1, single_sizes[start]
            batches.append((dict(items[start:start + count]), body_bytes))
            start += count
        return batches

    def _judge_batches(
        self, state: str | dict[str, Any], batches: list[tuple[dict[str, Choice], int]],
        groups: dict[str, list[Candidate]], *, report: "RunReport | None",
    ) -> tuple[dict[str, str], list[dict]]:
        """Retain successes, then resize only rejected work between parallel waves.

        Groups are replaced in place when a candidate question needs splitting.
        All resulting winners advance together in the caller's logical round.
        """
        pending = [(batch, None) for batch in batches]
        chosen, api_calls = {}, []
        attempt = 0
        while pending:
            jobs = []
            for (questions, body_bytes), parent in pending:
                replacements = {}
                prepared = {}
                for index, (key, question) in enumerate(questions.items()):
                    if parent is None or len(questions) > 1:
                        prepared[key] = question
                        continue
                    # A standalone rejection has already been checked below:
                    # only a candidate group with at least two options is split.
                    # Use this payload's reduction, not a smaller sibling's, so
                    # heterogeneous descriptions retain meaningful comparisons.
                    prefix = f"retry{parent['batch']}_{index}"
                    children, child_questions = self._group_candidates(
                        state, groups[key], prefix,
                        question_budget_bytes=int(parent["body_bytes"] * 0.8),
                    )
                    if len(children) < 2:
                        raise SelectionError("Jev context recovery could not split the candidate question.")
                    replacements[key] = list(children)
                    ordered = {}
                    for group_key, candidates in groups.items():
                        if group_key == key:
                            ordered.update(children)
                        else:
                            ordered[group_key] = candidates
                    groups.clear()
                    groups.update(ordered)
                    prepared.update(child_questions)
                retry_batches = [(questions, body_bytes)] if parent is None else self._pack_questions(
                    state, prepared,
                    request_budget_bytes=parent["body_bytes"] - 1 if parent else None,
                )
                if parent is not None:
                    # A failed payload is never resent unchanged, even if shared
                    # state dominates the body or longer replacement IDs add bytes.
                    if any(size >= parent["body_bytes"] for _, size in retry_batches):
                        raise SelectionError(
                            "Jev context recovery cannot produce a smaller complete request "
                            "without dropping essential state or candidates."
                        )
                    parent.update(replacement_questions=replacements,
                                  retry_batches=list(range(attempt + 1, attempt + 1 + len(retry_batches))))
                    if report:
                        report.append(
                            "selection_retry", retry_of_batch=parent["batch"],
                            retry_batches=parent["retry_batches"],
                            replacement_questions=replacements,
                            request_budget_bytes=self.request_budget_bytes,
                            question_budget_bytes=self.question_budget_bytes,
                        )
                for batch in retry_batches:
                    attempt += 1
                    jobs.append((attempt, batch, parent))

            def run_batch(job):
                number, (questions, body_bytes), parent = job
                retry_of = parent["batch"] if parent else None
                retry_limit = parent["body_bytes"] - 1 if parent else None
                batch_groups = [key for key in questions if key in groups]
                candidate_count = sum(len(groups[key]) for key in batch_groups)
                metadata = {
                    "batch": number, "batch_count": attempt,
                    "batch_candidate_count": candidate_count,
                    "group_keys": batch_groups, "question_keys": list(questions),
                    "question_count": len(questions), "body_bytes": body_bytes,
                    "retry_of_batch": retry_of,
                    "retry_body_bytes_limit": retry_limit,
                    "request_budget_bytes": self.request_budget_bytes,
                    "question_budget_bytes": self.question_budget_bytes,
                }
                with report.span("selection.batch", **metadata) if report else nullcontext({}) as timing:
                    choices, trace = self.judge(state, questions, report=report)
                    trace.update(batch=number, batch_count=attempt,
                                 candidate_count=candidate_count, group_keys=batch_groups,
                                 retry_of_batch=retry_of, retry_body_bytes_limit=retry_limit)
                    timing.update(outcome=trace["outcome"], context_rejection=trace["context_rejection"])
                    if report:
                        trace["batch_span_id"] = timing["span_id"]
                    return choices, trace

            # Budgets and group mappings are immutable while workers are active.
            # Collect every result before retrying so successful siblings survive.
            if len(jobs) == 1:
                results = [run_batch(jobs[0])]
            else:
                with ThreadPoolExecutor(thread_name_prefix="jev-batch") as executor:
                    futures = [executor.submit(copy_context().run, run_batch, job) for job in jobs]
                    results, errors = [], []
                    try:
                        for future in futures:
                            try:
                                results.append(future.result())
                            except Exception as error:
                                errors.append(error)
                    except BaseException:
                        for future in futures:
                            future.cancel()
                        raise
                    if errors:
                        raise errors[0]
            pending = []
            for (choices, trace), (_, (questions, _), _) in zip(results, jobs, strict=True):
                api_calls.append(trace)
                if trace["outcome"] == "ok":
                    chosen.update(choices)
                    continue
                if len(questions) > 1:
                    # Isolate complete questions before attributing overflow to
                    # any one question. Shared state is counted in every size.
                    largest = max(_request_body_size(state, {key: q}, self.model)
                                  for key, q in questions.items())
                    self.request_budget_bytes = min(
                        self.request_budget_bytes, max(largest, int(trace["body_bytes"] * 0.8)),
                    )
                else:
                    key, question = next(iter(questions.items()))
                    if key not in groups or len(groups[key]) < 2:
                        raise SelectionError(
                            "Jev rejected shared state plus one atomic question/candidate for "
                            "exceeding its token limit; selection stopped without sending an action."
                        )
                    self.question_budget_bytes = min(
                        self.question_budget_bytes, int(trace["body_bytes"] * 0.8),
                    )
                pending.append(((questions, trace["body_bytes"]), trace))
        for trace in api_calls:
            trace["batch_count"] = len(api_calls)
        return chosen, api_calls

    def _validate_response(
        self, questions: dict[str, Choice], response: Any
    ) -> tuple[dict[str, str], dict]:
        returned_model = getattr(response, "model", None)
        logger.info("Jev requested_model=%s response_model=%r", self.model, returned_model)
        if returned_model != self.model:
            raise SelectionError("Jev returned a model identity different from the pinned model.")
        # SDK forward compatibility skips unknown answer kinds. Check wire keys too
        # so an unexpected extra question cannot disappear during SDK decoding.
        raw_http_response = getattr(response, "raw_http_response", None)
        if raw_http_response is not None:
            try:
                raw = raw_http_response.json()
            except (ValueError, TypeError):
                raise SelectionError("Jev returned an invalid response body.") from None
            if (
                not isinstance(raw, Mapping)
                or not isinstance(raw.get("answers"), Mapping)
                or set(raw["answers"]) != set(questions)
            ):
                raise SelectionError("Jev must return exactly the requested question keys.")
        answers = getattr(response, "answers", None)
        if not isinstance(answers, Mapping) or set(answers) != set(questions):
            raise SelectionError("Jev must return exactly the requested question keys.")
        choices: dict[str, str] = {}
        answer_traces = {}
        for key, question in questions.items():
            answer = answers[key]
            if getattr(answer, "type", None) != "choice":
                raise SelectionError("Jev returned a non-Choice answer.")
            choice = getattr(answer, "choice", None)
            if not isinstance(choice, str) or choice not in question.criteria:
                raise SelectionError("Jev returned an unknown option label.")
            probabilities = getattr(answer, "probabilities", None)
            if not isinstance(probabilities, Mapping) or set(probabilities) != set(question.criteria):
                raise SelectionError("Jev probability labels do not match the observed options.")
            if not all(_probability(value) for value in probabilities.values()):
                raise SelectionError("Jev returned an invalid probability.")
            # Approximate totals may differ by up to three percentage points.
            probability_total = math.fsum(probabilities.values())
            if not 0.97 <= probability_total <= 1.03:
                raise SelectionError("Jev probability total differs from one by more than 3%.")
            if not _probability(getattr(answer, "confidence", None)):
                raise SelectionError("Jev returned an invalid confidence.")
            # Distribution validation is structural only. Never rank/gate winners by it.
            choices[key] = choice
            answer_traces[key] = {
                "type": "choice",
                "choice": choice,
                "probabilities": dict(probabilities),
                "confidence": answer.confidence,
            }

        usage = getattr(response, "usage", None)
        tokens = {name: getattr(usage, name, None) for name in ("input_tokens", "output_tokens")}
        if any(type(value) is not int or value < 0 for value in tokens.values()):
            raise SelectionError("Jev returned invalid token usage.")
        return choices, {
            "answers": answer_traces,
            "response_model": returned_model,
            **tokens,
            "input_cost_usd": tokens["input_tokens"] * 0.042 / 1_000_000,
            "input_price_per_million_usd": 0.042,
        }

    def select(
        self, original_request: str | dict[str, Any], candidates: Sequence[Candidate], *, stage: str,
        report: "RunReport | None" = None,
        settling_context: dict[str, Any] | None = None,
        requested_action: str | None = None,
    ) -> Selection:
        """Select with the original request or the caller's shared loop state."""
        with report.span("selection", stage=stage) if report else nullcontext({}):
            with report.span("selection.validation") if report else nullcontext({}):
                if stage not in {"windows", "controls"}:
                    raise ValueError("Selection stage must be windows or controls.")
                if requested_action not in {None, "click", "type_text", "press_key", "read"}:
                    raise ValueError("Unsupported authored action.")
                request_text = (
                    original_request.get("original_request")
                    if isinstance(original_request, dict) else original_request
                )
                if not isinstance(request_text, str) or not request_text.strip():
                    raise ValueError("The original request must be a nonempty string.")
                current = list(candidates)
                if any(not isinstance(candidate.key, str) or not candidate.key for candidate in current):
                    raise ValueError("Candidate keys must be nonempty strings.")
                if len({candidate.key for candidate in current}) != len(current):
                    raise ValueError("Candidate keys must be unique within a selection.")
            rounds: list[dict[str, Any]] = []
            settling_ms = None
            action = requested_action
            include_settling = stage == "controls" and settling_context is not None and requested_action != "read"
            possible_actions = (requested_action,) if requested_action else ("click", "type_text")
            while current and (len(current) > 1 or stage == "controls"):
                round_number = len(rounds) + 1
                round_id = f"{stage}_r{round_number}"
                with report.span(
                    "selection.round", round=round_number, round_id=round_id,
                    candidate_count=len(current),
                ) if report else nullcontext({}) as round_timing:
                    with report.span("selection.grouping") if report else nullcontext({}) as grouping_timing:
                        round_state = deepcopy(original_request)
                        groups, questions = self._group_candidates(round_state, current, round_id)
                        final_round = len(groups) == 1
                        action_keys = {}
                        timing_keys = {}
                        if final_round and stage == "controls":
                            for candidate_index, candidate in enumerate(current):
                                if requested_action is None:
                                    key = f"{round_id}_action_{candidate_index}"
                                    action_keys[candidate.key] = key
                                    questions[key] = Choice(
                                        instructions={
                                            "question": ACTION,
                                            "candidate": {candidate.key: candidate.description},
                                        },
                                        criteria={"click": None, "type_text": None},
                                    )
                                if include_settling:
                                    for possible_action in possible_actions:
                                        key = f"{round_id}_settling_{candidate_index}_{possible_action}"
                                        timing_keys[candidate.key, possible_action] = key
                                        questions[key] = Choice(
                                            instructions={
                                                "question": TIMER,
                                                "candidate": {candidate.key: candidate.description},
                                                "action": possible_action,
                                                "app_context": settling_context,
                                                "timing_scope": "after_native_action_return",
                                                "premise": "Assume this candidate and action are selected.",
                                            },
                                            criteria={label: None for label in SETTLING_MS},
                                        )
                    with report.span(
                        "selection.packing", request_budget_bytes=self.request_budget_bytes,
                        question_budget_bytes=self.question_budget_bytes,
                    ) if report else nullcontext({}) as packing_timing:
                        # Selection, action choices, and hypothetical timers are
                        # fixed before sizing and independent across batches.
                        questions = deepcopy(questions)
                        batches = self._pack_questions(round_state, questions)
                        packing_timing["batch_count"] = len(batches)
                    chosen, api_calls = self._judge_batches(
                        round_state, batches,
                        groups, report=report,
                    )
                    # Recovery can split a formerly final group. Recompare all
                    # its winners before consuming any speculative action.
                    final_round = len(groups) == 1
                    if len(current) > 1 and len(groups) >= len(current):
                        raise SelectionError(
                            "The Jev context allowance permits only singleton groups; "
                            "selection cannot narrow these candidates safely."
                        )
                    # Advance only after every batch in this round succeeds.
                    with report.span("selection.advancement") if report else nullcontext({}) as advancement_timing:
                        winners = []
                        group_traces = []
                        for key, group in groups.items():
                            winner_key = chosen[key]
                            winners.append(next(candidate for candidate in group if candidate.key == winner_key))
                            group_traces.append({
                                "question_key": key,
                                "options": [candidate.key for candidate in group],
                                "winner": winner_key,
                            })
                        round_trace = {
                            "candidate_count": len(current),
                            "group_count": len(groups),
                            "winner_count": len(winners),
                            "groups": group_traces,
                            "api_calls": api_calls,
                            "round": round_number,
                            "round_id": round_id,
                        }
                        if action_keys:
                            round_trace["actions"] = {
                                candidate.key: {
                                    "question_key": action_keys[candidate.key],
                                    "choice": chosen[action_keys[candidate.key]],
                                }
                                for candidate in current
                            }
                            if final_round:
                                action = chosen[action_keys[winners[0].key]]
                        if timing_keys:
                            round_trace["settling"] = {
                                candidate.key: {
                                    possible_action: {
                                        "question_key": timing_keys[candidate.key, possible_action],
                                        "choice": chosen[timing_keys[candidate.key, possible_action]],
                                        "milliseconds": SETTLING_MS[chosen[timing_keys[candidate.key, possible_action]]],
                                    }
                                    for possible_action in possible_actions
                                }
                                for candidate in current
                            }
                            if final_round:
                                settling_ms = round_trace["settling"][winners[0].key][action]["milliseconds"]
                        current = winners
                    if report:
                        round_trace.update(
                            span_id=round_timing["span_id"],
                            grouping_span_id=grouping_timing["span_id"],
                            packing_span_id=packing_timing["span_id"],
                            advancement_span_id=advancement_timing["span_id"],
                        )
                    rounds.append(round_trace)
                    if final_round:
                        break
            return Selection(
                winner=current[0] if current else None, rounds=rounds,
                settling_ms=settling_ms, action=action,
            )
