"""Deterministic groups-of-ten Choice elimination, shared by both stages."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import logging
import math
from typing import Any

from typesafe_sdk import Choice, TypeSafeError


PINNED_MODEL = "jev-1.13.0"
GROUP_SIZE = 10
QUESTION = (
    "Which of these candidates is best suited to help fulfill the user’s original "
    "request, based on the provided candidate information?"
)
logger = logging.getLogger(__name__)


class SelectionError(Exception):
    """A request cannot be packed or a Jev response cannot be used safely."""


@dataclass(frozen=True)
class Candidate:
    key: str
    description: dict[str, Any]
    record: dict[str, Any]


@dataclass(frozen=True)
class RequestBudget:
    # UTF-8 JSON bytes are a conservative packing heuristic, NOT a tokenizer.
    # Leave headroom for the API's 64k total / 32k state+one-question limits.
    max_request_bytes: int = 48_000
    max_question_bytes: int = 24_000

    def __post_init__(self) -> None:
        for value in (self.max_request_bytes, self.max_question_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("Request byte budgets must be positive integers.")


@dataclass
class Selection:
    winner: Candidate | None
    rounds: list[dict[str, Any]] = field(default_factory=list)


def _request_bytes(state: str, questions: Mapping[str, Choice], model: str) -> int:
    body = {
        "model": model,
        "state": state,
        "questions": {key: question.model_dump() for key, question in questions.items()},
    }
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


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
        budget: RequestBudget | None = None,
    ) -> None:
        if model != PINNED_MODEL:
            raise ValueError(f"This experiment requires {PINNED_MODEL}.")
        self.client = client
        self.model = model
        self.budget = budget or RequestBudget()

    def _pack(self, state: str, questions: dict[str, Choice]) -> list[dict[str, Choice]]:
        batches: list[dict[str, Choice]] = []
        batch: dict[str, Choice] = {}
        for key, question in questions.items():
            single_size = _request_bytes(state, {key: question}, self.model)
            if single_size > min(
                self.budget.max_question_bytes, self.budget.max_request_bytes
            ):
                raise SelectionError(
                    "The unchanged request plus one candidate group exceeds the "
                    "configured byte budget. Increase the budget within service "
                    "limits or supply a shorter original request."
                )
            proposed = {**batch, key: question}
            if batch and _request_bytes(state, proposed, self.model) > self.budget.max_request_bytes:
                batches.append(batch)
                batch = {}
            batch[key] = question
        if batch:
            batches.append(batch)
        return batches

    def _judge(self, state: str, questions: dict[str, Choice]) -> tuple[dict[str, str], dict]:
        try:
            response = self.client.system_one(
                state=state, questions=questions, model=self.model
            )
        except TypeSafeError as error:
            # No request bodies, UI content, credentials, or raw HTTP responses in errors.
            status = getattr(error, "status", None)
            safe_status = status if type(status) is int else "unavailable"
            raise SelectionError(
                f"TypeSafe request failed: {type(error).__name__} (HTTP {safe_status})."
            ) from None

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
            if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-4):
                raise SelectionError("Jev probabilities do not sum to one.")
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
            "request_bytes": _request_bytes(state, questions, self.model),
            **tokens,
        }

    def select(
        self, original_request: str, candidates: Sequence[Candidate], *, stage: str
    ) -> Selection:
        if not isinstance(original_request, str) or not original_request.strip():
            raise ValueError("The original request must be a nonempty string.")
        current = list(candidates)
        if any(not isinstance(candidate.key, str) or not candidate.key for candidate in current):
            raise ValueError("Candidate keys must be nonempty strings.")
        if len({candidate.key for candidate in current}) != len(current):
            raise ValueError("Candidate keys must be unique within a selection.")
        rounds: list[dict[str, Any]] = []
        while len(current) > 1:
            groups = [current[start : start + GROUP_SIZE] for start in range(0, len(current), GROUP_SIZE)]
            questions = {}
            group_keys = []
            for group_index, group in enumerate(groups):
                key = f"{stage}_r{len(rounds) + 1}_g{group_index}"
                group_keys.append(key)
                if len(group) > 1:
                    questions[key] = Choice(
                        instructions=QUESTION,
                        criteria={candidate.key: candidate.description for candidate in group},
                    )
            # Finish every independent batch in this round before constructing the next.
            chosen: dict[str, str] = {}
            batches = []
            for batch in self._pack(original_request, questions):
                answers, trace = self._judge(original_request, batch)
                chosen.update(answers)
                batches.append(trace)
            winners = []
            group_traces = []
            for key, group in zip(group_keys, groups, strict=True):
                winner_key = group[0].key if len(group) == 1 else chosen[key]
                winner = next(candidate for candidate in group if candidate.key == winner_key)
                winners.append(winner)
                group_traces.append({
                    "question_key": key if len(group) > 1 else None,
                    "options": [candidate.key for candidate in group],
                    "winner": winner.key,
                    "forwarded": len(group) == 1,
                })
            rounds.append({
                "candidate_count": len(current),
                "winner_count": len(winners),
                "groups": group_traces,
                "batches": batches,
            })
            current = winners
        return Selection(winner=current[0] if current else None, rounds=rounds)
