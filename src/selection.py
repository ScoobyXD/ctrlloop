"""Deterministic groups-of-ten Choice elimination for windows and controls."""

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
import logging
import math
from typing import Any, TYPE_CHECKING

from typesafe_sdk import Choice, TypeSafeError

if TYPE_CHECKING:
    from src.reporting import RunReport


PINNED_MODEL = "jev-1.13.0"
GROUP_SIZE = 10
QUESTION = (
    "Which of these candidates is best suited to help fulfill the user’s original "
    "request, based on the provided candidate information?"
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

    def _judge(
        self, state: str, questions: dict[str, Choice], *, report: "RunReport | None" = None,
    ) -> tuple[dict[str, str], dict]:
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
            "requested_model": self.model,
            "response_model": returned_model,
            **tokens,
        }

    def select(
        self, original_request: str, candidates: Sequence[Candidate], *, stage: str,
        report: "RunReport | None" = None,
    ) -> Selection:
        with report.span("selection", stage=stage) if report else nullcontext({}):
            with report.span("selection.validation") if report else nullcontext({}):
                if not isinstance(original_request, str) or not original_request.strip():
                    raise ValueError("The original request must be a nonempty string.")
                current = list(candidates)
                if any(not isinstance(candidate.key, str) or not candidate.key for candidate in current):
                    raise ValueError("Candidate keys must be nonempty strings.")
                if len({candidate.key for candidate in current}) != len(current):
                    raise ValueError("Candidate keys must be unique within a selection.")
            rounds: list[dict[str, Any]] = []
            while len(current) > 1:
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
                        for group_index, group in enumerate(groups):
                            key = f"{round_id}_g{group_index}"
                            group_keys.append(key)
                            questions[key] = Choice(
                                instructions=QUESTION,
                                criteria={candidate.key: candidate.description for candidate in group},
                            )
                    # Every group's question belongs in this round's single call.
                    chosen, api_call = self._judge(original_request, questions, report=report)
                    with report.span("selection.advancement") if report else nullcontext({}) as advancement_timing:
                        winners = []
                        group_traces = []
                        for key, group in zip(group_keys, groups, strict=True):
                            winner_key = chosen[key]
                            winner = next(candidate for candidate in group if candidate.key == winner_key)
                            winners.append(winner)
                            group_traces.append({
                                "question_key": key,
                                "options": [candidate.key for candidate in group],
                                "winner": winner.key,
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
                        current = winners
                    if report:
                        round_trace.update(
                            span_id=round_timing["span_id"],
                            grouping_span_id=grouping_timing["span_id"],
                            advancement_span_id=advancement_timing["span_id"],
                        )
                    rounds.append(round_trace)
            return Selection(winner=current[0] if current else None, rounds=rounds)
