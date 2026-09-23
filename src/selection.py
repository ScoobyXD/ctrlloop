"""Groups-of-ten Choice elimination for windows and controls."""

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
import logging
import math
from typing import Any, TYPE_CHECKING

from typesafe_sdk import Choice, Noul, TypeSafeError

if __package__:
    from .jev_questions import ACTION, QUESTION, TEXT_WORD, TIMER
else:
    from jev_questions import ACTION, QUESTION, TEXT_WORD, TIMER

if TYPE_CHECKING:
    from src.reporting import RunReport


PINNED_MODEL = "jev-1.13.0"
GROUP_SIZE = 10
CONTROL_CANDIDATES_PER_CALL = 400
# Experimental decision policy, not a calibrated reliability guarantee. A tie
# stays excluded; retain every raw Noul so this policy can be evaluated later.
WORD_INCLUSION_THRESHOLD = 0.5
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
        self, state: str | dict[str, Any], questions: dict[str, Choice | Noul], *,
        report: "RunReport | None" = None,
    ) -> tuple[dict[str, str | float], dict]:
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
                    f"Jev rejected the request batch (HTTP {safe_status}); verify "
                    "the service request limits and question format. The batch "
                    "was not retried in smaller calls."
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
        self, questions: dict[str, Choice | Noul], response: Any
    ) -> tuple[dict[str, str | float], dict]:
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
        choices: dict[str, str | float] = {}
        answer_traces = {}
        for key, question in questions.items():
            answer = answers[key]
            if isinstance(question, Noul):
                if getattr(answer, "type", None) != "noul":
                    raise SelectionError("Jev returned a non-Noul answer.")
                noul = getattr(answer, "noul", None)
                if not _probability(noul):
                    raise SelectionError("Jev returned an invalid Noul probability.")
                choices[key] = float(noul)
                answer_traces[key] = {"type": "noul", "noul": noul}
                continue
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
            "question_keys": list(questions),
            "questions": {
                key: {
                    "type": question.type,
                    "instructions": deepcopy(question.instructions),
                    "criteria": deepcopy(question.criteria),
                }
                for key, question in questions.items()
            },
            "answers": answer_traces,
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
                if stage not in {"windows", "controls"}:
                    raise ValueError("Selection stage must be windows or controls.")
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
            action = None
            include_settling = stage == "controls" and settling_context is not None
            while current and (len(current) > 1 or stage == "controls"):
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
                        for group_index, group in enumerate(groups):
                            key = f"{round_id}_g{group_index}"
                            group_keys.append(key)
                            criteria = {candidate.key: candidate.description for candidate in group}
                            questions[key] = Choice(
                                instructions=QUESTION,
                                criteria=criteria,
                            )
                        action_keys = {}
                        timing_keys = {}
                        if final_round and stage == "controls":
                            for candidate_index, candidate in enumerate(current):
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
                                    for possible_action in ("click", "type_text"):
                                        key = f"{round_id}_settling_{candidate_index}_{possible_action}"
                                        timing_keys[candidate.key, possible_action] = key
                                        questions[key] = Choice(
                                            instructions={
                                                "question": TIMER,
                                                "candidate": {candidate.key: candidate.description},
                                                "action": possible_action,
                                                "app_context": settling_context,
                                                "timing_scope": "after_cua_return",
                                                "premise": "Assume this candidate and action are selected.",
                                            },
                                            criteria={label: None for label in SETTLING_MS},
                                        )
                        groups_per_call = (
                            CONTROL_CANDIDATES_PER_CALL // GROUP_SIZE
                            if stage == "controls" else len(groups)
                        )
                        batches = [
                            group_keys[start : start + groups_per_call]
                            for start in range(0, len(group_keys), groups_per_call)
                        ]
                    chosen = {}
                    api_calls = []
                    for batch_index, batch_keys in enumerate(batches, start=1):
                        batch_candidate_count = sum(len(questions[key].criteria) for key in batch_keys)
                        # Independent action choices and hypothetical timers stay
                        # in the final round's single batch.
                        batch_questions = {
                            key: questions[key]
                            for key in (*batch_keys, *action_keys.values(), *timing_keys.values())
                        }
                        with report.span(
                            "selection.batch", batch=batch_index, batch_count=len(batches),
                            batch_candidate_count=batch_candidate_count,
                        ) if report else nullcontext({}) as batch_timing:
                            batch_choices, api_call = self.judge(
                                original_request, batch_questions, report=report,
                            )
                            api_call.update(
                                batch=batch_index, batch_count=len(batches),
                                candidate_count=batch_candidate_count,
                            )
                            if report:
                                api_call["batch_span_id"] = batch_timing["span_id"]
                            chosen.update(batch_choices)
                            api_calls.append(api_call)
                    # Advance only after every batch in this round succeeds.
                    with report.span("selection.advancement") if report else nullcontext({}) as advancement_timing:
                        winners = []
                        group_traces = []
                        for key, group in zip(group_keys, groups, strict=True):
                            winner_key = chosen[key]
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
                            "api_calls": api_calls,
                            "round": round_number,
                            "round_id": round_id,
                        }
                        if final_round and stage == "controls":
                            round_trace["actions"] = {
                                candidate.key: {
                                    "question_key": action_keys[candidate.key],
                                    "choice": chosen[action_keys[candidate.key]],
                                }
                                for candidate in current
                            }
                            action = chosen[action_keys[winners[0].key]]
                        if final_round and include_settling:
                            round_trace["settling"] = {
                                candidate.key: {
                                    possible_action: {
                                        "question_key": timing_keys[candidate.key, possible_action],
                                        "choice": chosen[timing_keys[candidate.key, possible_action]],
                                        "milliseconds": SETTLING_MS[chosen[timing_keys[candidate.key, possible_action]]],
                                    }
                                    for possible_action in ("click", "type_text")
                                }
                                for candidate in current
                            }
                            settling_ms = round_trace["settling"][winners[0].key][action]["milliseconds"]
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
                winner=current[0] if current else None, rounds=rounds,
                settling_ms=settling_ms, action=action,
            )

    def select_text(
        self, state: str | dict[str, Any], words: Sequence[Candidate], *,
        target: Mapping[str, Any], report: "RunReport | None" = None,
    ) -> Selection:
        """Judge all source words once, then copy a contiguous exact source span."""
        with report.span("selection", stage="text") if report else nullcontext({}):
            with report.span("selection.validation") if report else nullcontext({}):
                request = state.get("original_request") if isinstance(state, dict) else state
                if not isinstance(request, str) or not request.strip():
                    raise ValueError("The original request must be a nonempty string.")
                words = list(words)
                keys = set()
                previous_end = 0
                for word in words:
                    start, end = word.record.get("start"), word.record.get("end")
                    if (
                        not isinstance(word.key, str) or not word.key or word.key in keys
                        or type(start) is not int or type(end) is not int
                        or not previous_end <= start < end <= len(request)
                        or word.description.get("text") != request[start:end]
                    ):
                        raise ValueError("Words must have unique keys and ordered exact source offsets.")
                    keys.add(word.key)
                    previous_end = end
                text_state = dict(state) if isinstance(state, dict) else {"original_request": request}
                text_state["text_target"] = dict(target)
            round_id = "text_r1"
            with report.span(
                "selection.round", round=1, round_id=round_id, candidate_count=len(words),
            ) if report else nullcontext({}) as round_timing:
                with report.span("selection.grouping") if report else nullcontext({}) as grouping_timing:
                    questions = {
                        f"{round_id}_{word.key}": Noul(instructions={
                            "question": TEXT_WORD,
                            "word": {"index": index, "text": word.description["text"], **word.record},
                        })
                        for index, word in enumerate(words)
                    }
                api_calls = []
                outcomes = {}
                if questions:
                    # All original-request words share this one call. Never
                    # chunk a rejected request or infer from a partial response.
                    with report.span(
                        "selection.batch", batch=1, batch_count=1,
                        batch_candidate_count=len(words),
                    ) if report else nullcontext({}) as batch_timing:
                        outcomes, api_call = self.judge(text_state, questions, report=report)
                        api_call.update(batch=1, batch_count=1, candidate_count=len(words))
                        if report:
                            api_call["batch_span_id"] = batch_timing["span_id"]
                        api_calls.append(api_call)
                with report.span("selection.advancement") if report else nullcontext({}) as advancement_timing:
                    selected_indices = [
                        index for index, word in enumerate(words)
                        if outcomes[f"{round_id}_{word.key}"] > WORD_INCLUSION_THRESHOLD
                    ]
                    winner = None
                    reason = None
                    if not selected_indices:
                        reason = "No request words exceeded the text-inclusion threshold."
                    elif selected_indices != list(range(selected_indices[0], selected_indices[-1] + 1)):
                        reason = "Selected request words are not one contiguous source span."
                    else:
                        start = words[selected_indices[0]].record["start"]
                        end = words[selected_indices[-1]].record["end"]
                        exact_text = request[start:end]
                        winner = Candidate("text", {"text": exact_text}, {"text": exact_text})
                    round_trace = {
                        "round": 1, "round_id": round_id, "candidate_count": len(words),
                        "question_count": len(questions), "winner_count": int(winner is not None),
                        "api_calls": api_calls, "threshold": WORD_INCLUSION_THRESHOLD,
                        "threshold_comparison": ">", "selected_indices": selected_indices,
                        "selected_offsets": [dict(words[index].record) for index in selected_indices],
                        "reason": reason,
                    }
                    if winner is not None:
                        round_trace["source_span"] = {"start": start, "end": end}
                        round_trace["text"] = winner.record["text"]
                if report:
                    round_trace.update(
                        span_id=round_timing["span_id"], grouping_span_id=grouping_timing["span_id"],
                        advancement_span_id=advancement_timing["span_id"],
                    )
            return Selection(winner=winner, rounds=[round_trace], action="type_text" if winner else None)
