"""Groups-of-ten Choice elimination for windows, controls and action evidence."""

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import math
from typing import Any, TYPE_CHECKING

from typesafe_sdk import Choice, TypeSafeError

if TYPE_CHECKING:
    from src.reporting import RunReport


PINNED_MODEL = "jev-1.13.0"
GROUP_SIZE = 10
NONE_KEY = "none"
SETTLING_MS = {
    "<50ms": 50,
    "100ms": 100,
    "200ms": 200,
    "300ms": 300,
    "400ms": 400,
    ">500ms": 500,
}
QUESTION = (
    "Which of these candidates is best suited to help fulfill the user’s original "
    "request, based on the provided candidate information?"
)
EVIDENCE_QUESTION = (
    "Which of these freshly observed candidates provides the clearest evidence that "
    "the action recorded in `last_action`, targeting the unverified `last_control`, "
    "achieved its intended result? Choose none if no candidate provides such evidence. "
    "A past selection or action-delivery result alone is not proof."
)
EVIDENCE_CONFIRMATION_QUESTION = (
    "Based on these evidence candidates and the action we attempted, did the action "
    "achieve its intended result?"
)
logger = logging.getLogger(__name__)


class SelectionError(Exception):
    """Jev rejected a complete round or its response cannot be used safely."""


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
    verification: bool | None = None


def _probability(value: Any) -> bool:
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


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

    def judge(
        self, state: str | dict[str, Any], questions: dict[str, Choice], *,
        report: "RunReport | None" = None,
    ) -> tuple[dict[str, str], dict]:
        """Submit one complete batch using the pinned client and shared validation."""
        try:
            # SDK time includes request encoding and response decoding. The
            # report's HTTP timing separately measures the client-observed call.
            with report.span(
                "selection.sdk_call", question_keys=list(questions),
            ) if report else nullcontext({}) as sdk_timing:
                response = self.client.system_one(
                    state=state, questions=questions, model=self.model
                )
        except TypeSafeError as error:
            # No request bodies, UI content, credentials, or raw HTTP responses in errors.
            status = getattr(error, "status", None)
            safe_status = status if type(status) is int else "unavailable"
            if status in (413, 422):
                raise SelectionError(
                    f"Jev rejected the complete round (HTTP {safe_status}); verify "
                    "the service request limits and question format. The round "
                    "was not split into smaller calls."
                ) from None
            raise SelectionError(
                f"TypeSafe request failed: {type(error).__name__} (HTTP {safe_status})."
            ) from None

        with report.span("selection.response_validation") if report else nullcontext({}) as validation_timing:
            choices, trace = self._validate_response(questions, response)
            trace["state"] = deepcopy(state)
        if report:
            trace.update(
                sdk_span_id=sdk_timing["span_id"],
                response_validation_span_id=validation_timing["span_id"],
            )
        return choices, trace

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

        usage = getattr(response, "usage", None)
        tokens = {name: getattr(usage, name, None) for name in ("input_tokens", "output_tokens")}
        if any(type(value) is not int or value < 0 for value in tokens.values()):
            raise SelectionError("Jev returned invalid token usage.")
        return choices, {
            "question_keys": list(questions),
            "questions": {
                key: {
                    "type": "choice",
                    "instructions": deepcopy(question.instructions),
                    "criteria": deepcopy(question.criteria),
                }
                for key, question in questions.items()
            },
            "answers": {
                key: {
                    "type": "choice",
                    "choice": answer.choice,
                    "probabilities": dict(answer.probabilities),
                    "confidence": answer.confidence,
                }
                for key, answer in answers.items()
            },
            "requested_model": self.model,
            "response_model": returned_model,
            **tokens,
        }

    def select(
        self, original_request: str | dict[str, Any], candidates: Sequence[Candidate], *, stage: str,
        report: "RunReport | None" = None,
        settling_context: dict[str, Any] | None = None,
    ) -> Selection:
        """Select with the original request or the caller's shared loop state."""
        with report.span("selection", stage=stage) if report else nullcontext({}):
            with report.span("selection.validation") if report else nullcontext({}):
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
                include_none = stage in {"controls", "evidence"}
                if include_none and any(candidate.key == NONE_KEY for candidate in current):
                    raise ValueError("The none option key is reserved for control and evidence selection.")
            rounds: list[dict[str, Any]] = []
            settling_ms = None
            verification = False if stage == "evidence" else None
            include_settling = stage == "controls" and settling_context is not None
            while current and (len(current) > 1 or include_none):
                round_number = len(rounds) + 1
                round_id = f"{stage}_r{round_number}"
                with report.span(
                    "selection.round", round=round_number, round_id=round_id,
                    candidate_count=len(current),
                ) if report else nullcontext({}) as round_timing:
                    with report.span("selection.grouping") if report else nullcontext({}) as grouping_timing:
                        groups = [current[start : start + GROUP_SIZE] for start in range(0, len(current), GROUP_SIZE)]
                        questions = {}
                        group_keys = []
                        final_round = len(current) <= GROUP_SIZE
                        app_context = (
                            json.dumps(settling_context, ensure_ascii=False, separators=(",", ":"))
                            if final_round and include_settling else None
                        )
                        for group_index, group in enumerate(groups):
                            key = f"{round_id}_g{group_index}"
                            group_keys.append(key)
                            criteria = {candidate.key: candidate.description for candidate in group}
                            if include_none:
                                criteria[NONE_KEY] = (
                                    "None of these candidates provides evidence that the attempted action succeeded."
                                    if stage == "evidence" else
                                    "None of these candidates is suitable for the next action toward the user's request."
                                )
                            question = EVIDENCE_QUESTION if stage == "evidence" else QUESTION
                            questions[key] = Choice(
                                instructions=(
                                    f"{question} Observed app/browser context: {app_context}."
                                    if final_round and include_settling else question
                                ),
                                criteria=criteria,
                            )
                        timing_keys = {}
                        if final_round and include_settling:
                            for candidate_index, candidate in enumerate(current):
                                key = f"{round_id}_settling_{candidate_index}"
                                timing_keys[candidate.key] = key
                                description = json.dumps(
                                    candidate.description, ensure_ascii=False, separators=(",", ":"),
                                )
                                questions[key] = Choice(
                                    instructions=(
                                        f"Assume CUA executes a click on candidate {candidate.key} "
                                        f"described by {description}, independently of which candidate "
                                        f"wins selection. After CUA finishes delivering this candidate "
                                        f"action in the observed app/browser {app_context} and its call "
                                        f"returns, how much additional time should this browser/app "
                                        f"need for the resulting state to become ready for the next step? "
                                        f"Estimate additional app settling time, excluding CUA execution "
                                        f"duration. The >500ms answer schedules a first observation at "
                                        f"500ms; it does not assert that the app has finished."
                                    ),
                                    criteria={label: None for label in SETTLING_MS},
                                )
                        confirmation_key = None
                        if final_round and stage == "evidence":
                            confirmation_key = f"{round_id}_confirmation"
                            questions[confirmation_key] = Choice(
                                instructions={
                                    "question": EVIDENCE_CONFIRMATION_QUESTION,
                                    "evidence_candidates": {
                                        candidate.key: candidate.description for candidate in current
                                    },
                                    "assessment": (
                                        "Use these fresh observations with last_control and last_action "
                                        "from the shared state. An action return alone is not proof of "
                                        "success; missing or partial observations are not proof either. "
                                        "Answer independently of the sibling selection question."
                                    ),
                                },
                                criteria={"yes": None, "no": None},
                            )
                    # Group questions, conditional timings and final evidence
                    # confirmation share one request and never need sibling answers.
                    chosen, api_call = self.judge(original_request, questions, report=report)
                    with report.span("selection.advancement") if report else nullcontext({}) as advancement_timing:
                        winners = []
                        group_traces = []
                        for key, group in zip(group_keys, groups, strict=True):
                            winner_key = chosen[key]
                            if not include_none or winner_key != NONE_KEY:
                                winners.append(next(candidate for candidate in group if candidate.key == winner_key))
                            group_traces.append({
                                "question_key": key,
                                "options": list(questions[key].criteria),
                                "winner": winner_key,
                            })
                        round_trace = {
                            "candidate_count": len(current),
                            "group_count": len(groups),
                            "winner_count": len(winners),
                            "groups": group_traces,
                            "api_call": api_call,
                            "round": round_number,
                            "round_id": round_id,
                        }
                        if final_round and include_settling:
                            round_trace["settling"] = {
                                candidate.key: {
                                    "question_key": timing_keys[candidate.key],
                                    "choice": chosen[timing_keys[candidate.key]],
                                    "milliseconds": SETTLING_MS[chosen[timing_keys[candidate.key]]],
                                }
                                for candidate in current
                            }
                            if winners:
                                settling_ms = round_trace["settling"][winners[0].key]["milliseconds"]
                        if confirmation_key is not None:
                            verification = bool(winners) and chosen[confirmation_key] == "yes"
                            round_trace["verification"] = {
                                "question_key": confirmation_key,
                                "choice": chosen[confirmation_key],
                                "verified": verification,
                            }
                        current = winners
                    if report:
                        round_trace.update(
                            span_id=round_timing["span_id"],
                            grouping_span_id=grouping_timing["span_id"],
                            advancement_span_id=advancement_timing["span_id"],
                        )
                    rounds.append(round_trace)
                    if final_round:
                        break
            return Selection(
                winner=current[0] if current else None, rounds=rounds, settling_ms=settling_ms,
                verification=verification,
            )
