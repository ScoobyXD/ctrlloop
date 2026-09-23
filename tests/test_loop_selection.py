"""Final control selection batches conditional settling questions without live calls."""

from types import SimpleNamespace
import unittest

from typesafe_sdk import Choice

from src.selection import (
    Candidate, ChoiceSelector, EVIDENCE_CONFIRMATION_QUESTION, EVIDENCE_QUESTION,
    NONE_KEY, PINNED_MODEL, SETTLING_MS, SelectionError,
)


REQUEST = "Open the details in my observed browser."
CONTEXT = {"app_name": "Observed Browser", "title": "Example page"}


def candidates(count):
    return [
        Candidate(
            key=f"control_{index}",
            description={"role": "Button", "label": f"Details {index}"},
            record={"element_token": f"private_token_{index}"},
        )
        for index in range(count)
    ]


class FakeClient:
    def __init__(self, timing_labels=("100ms",), *, choose_first=False,
                 group_choices=None, confirmation="yes"):
        self.calls = []
        self.timing_labels = timing_labels
        self.choose_first = choose_first
        self.group_choices = group_choices or {}
        self.confirmation = confirmation

    def system_one(self, *, state, questions, model):
        self.calls.append({"state": state, "questions": questions, "model": model})
        answers = {}
        timing_index = 0
        for key, question in questions.items():
            labels = list(question.criteria)
            if labels == list(SETTLING_MS):
                chosen = self.timing_labels[timing_index % len(self.timing_labels)]
                timing_index += 1
            elif key.endswith("_confirmation"):
                chosen = self.confirmation
            else:
                real_labels = [label for label in labels if label != NONE_KEY]
                chosen = self.group_choices.get(key, real_labels[0] if self.choose_first else real_labels[-1])
            answers[key] = SimpleNamespace(
                type="choice", choice=chosen, confidence=0.5,
                probabilities={
                    label: 0.7 if label == chosen else 0.3 / (len(labels) - 1)
                    for label in labels
                } if len(labels) > 1 else {chosen: 1.0},
            )
        return SimpleNamespace(
            model=PINNED_MODEL, answers=answers,
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        )


class LoopSelectionTests(unittest.TestCase):
    def evidence_state(self):
        return {
            "original_request": REQUEST, "iteration": 2,
            "current_window": CONTEXT,
            "last_control": {
                "window": CONTEXT, "control": {"role": "Button", "label": "Open details"},
                "status": "unverified",
            },
            "last_action": {"tool": "click", "result": {"delivery": "returned"}},
        }

    def test_only_final_round_has_per_finalist_timing_in_same_request(self):
        client = FakeClient()
        selection = ChoiceSelector(client).select(
            REQUEST, candidates(112), stage="controls", settling_context=CONTEXT,
        )
        self.assertEqual([r["candidate_count"] for r in selection.rounds], [112, 12, 2])
        self.assertEqual([len(call["questions"]) for call in client.calls], [12, 2, 3])
        for call in client.calls[:-1]:
            self.assertTrue(all("settling" not in key for key in call["questions"]))
        final = selection.rounds[-1]
        self.assertEqual(set(final["settling"]), set(final["groups"][0]["options"]) - {NONE_KEY})
        self.assertEqual(len(final["settling"]), 2)
        self.assertEqual(selection.winner.key, "control_111")
        self.assertEqual(selection.settling_ms, 100)
        for call in client.calls:
            self.assertEqual(call["state"], REQUEST)
            self.assertEqual(call["model"], PINNED_MODEL)

    def test_questions_identify_observed_app_browser_and_independent_candidate(self):
        observed = candidates(2)
        client = FakeClient()
        context = {**CONTEXT, "snapshot_app": {"name": "Browser reported by snapshot"}}
        result = ChoiceSelector(client).select(
            REQUEST, observed, stage="controls", settling_context=context,
        )
        call = client.calls[0]
        self.assertEqual(len(call["questions"]), 3)
        for question in call["questions"].values():
            self.assertIn("Observed Browser", question.instructions)
            self.assertIn("Example page", question.instructions)
            self.assertIn("Browser reported by snapshot", question.instructions)
        for candidate in observed:
            timing = result.rounds[0]["settling"][candidate.key]
            question = call["questions"][timing["question_key"]]
            self.assertIn(candidate.key, question.instructions)
            self.assertIn(candidate.description["label"], question.instructions)
            self.assertNotIn(candidate.record["element_token"], question.instructions)
            self.assertIn("After CUA finishes delivering", question.instructions)
            self.assertIn("call returns", question.instructions)
            self.assertIn("additional time", question.instructions)
            self.assertIn("independently of which candidate wins", question.instructions)
            self.assertIn("excluding CUA execution duration", question.instructions)
            self.assertEqual(list(question.criteria), list(SETTLING_MS))

    def test_loop_state_is_shared_by_selection_and_batched_settling_questions(self):
        state = {
            "original_request": REQUEST, "iteration": 2,
            "current_window": CONTEXT,
            "last_control": {"role": "Button", "label": "Open"},
        }
        client = FakeClient()
        selection = ChoiceSelector(client).select(
            state, candidates(112), stage="controls", settling_context=CONTEXT,
        )
        self.assertEqual([len(call["questions"]) for call in client.calls], [12, 2, 3])
        self.assertEqual(selection.settling_ms, 100)
        for call, round_trace in zip(client.calls, selection.rounds, strict=True):
            self.assertEqual(call["state"], state)
            self.assertEqual(call["model"], PINNED_MODEL)
            self.assertEqual(round_trace["api_call"]["state"], state)
        self.assertEqual(len(selection.rounds[-1]["settling"]), 2)

    def test_only_winning_candidates_timing_is_consumed_and_all_are_retained(self):
        client = FakeClient(("200ms", ">500ms"), choose_first=True)
        result = ChoiceSelector(client).select(
            REQUEST, candidates(2), stage="controls", settling_context=CONTEXT,
        )
        self.assertEqual(result.winner.key, "control_0")
        self.assertEqual(result.settling_ms, 200)
        final = result.rounds[0]
        self.assertEqual(final["settling"]["control_1"]["milliseconds"], 500)
        losing_key = final["settling"]["control_1"]["question_key"]
        losing_answer = final["api_call"]["answers"][losing_key]
        self.assertEqual(losing_answer["choice"], ">500ms")
        self.assertEqual(losing_answer["probabilities"][">500ms"], 0.7)
        self.assertEqual(set(losing_answer["probabilities"]), set(SETTLING_MS))

    def test_exact_timing_mapping_including_endpoints_for_singleton(self):
        self.assertEqual(SETTLING_MS, {
            "<50ms": 50, "100ms": 100, "200ms": 200,
            "300ms": 300, "400ms": 400, ">500ms": 500,
        })
        for label, milliseconds in SETTLING_MS.items():
            with self.subTest(label=label):
                client = FakeClient((label,))
                observed = candidates(1)
                result = ChoiceSelector(client).select(
                    REQUEST, observed, stage="controls", settling_context=CONTEXT,
                )
                self.assertIs(result.winner, observed[0])
                self.assertEqual(result.settling_ms, milliseconds)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(len(client.calls[0]["questions"]), 2)
                self.assertEqual(result.rounds[0]["groups"][0]["question_key"], "controls_r1_g0")
                self.assertEqual(result.rounds[0]["candidate_count"], 1)

    def test_no_timing_when_no_control_or_execution_context(self):
        for count, stage, context in (
            (0, "controls", CONTEXT), (1, "controls", None), (1, "windows", CONTEXT),
        ):
            with self.subTest(count=count, stage=stage, context=context):
                client = FakeClient()
                result = ChoiceSelector(client).select(
                    REQUEST, candidates(count), stage=stage, settling_context=context,
                )
                expected_calls = int(count == 1 and stage == "controls")
                self.assertEqual(len(client.calls), expected_calls)
                self.assertEqual(len(result.rounds), expected_calls)
                self.assertTrue(all("settling" not in key for call in client.calls for key in call["questions"]))
                self.assertIsNone(result.settling_ms)

    def test_trace_preserves_actual_questions_state_and_full_probabilities(self):
        client = FakeClient()
        observed = candidates(2)
        result = ChoiceSelector(client).select(
            REQUEST, observed, stage="controls", settling_context=CONTEXT,
        )
        trace = result.rounds[0]["api_call"]
        self.assertEqual(trace["state"], REQUEST)
        for key, question in client.calls[0]["questions"].items():
            self.assertEqual(trace["questions"][key], {
                "type": "choice", "instructions": question.instructions,
                "criteria": question.criteria,
            })
            answer = trace["answers"][key]
            self.assertEqual(answer["type"], "choice")
            self.assertEqual(answer["confidence"], 0.5)
            self.assertEqual(set(answer["probabilities"]), set(question.criteria))
            self.assertAlmostEqual(sum(answer["probabilities"].values()), 1.0)
        observed[0].description["label"] = "Changed after the call"
        self.assertEqual(
            trace["questions"]["controls_r1_g0"]["criteria"]["control_0"]["label"],
            "Details 0",
        )

    def test_public_judge_reuses_client_validation_and_preserves_structured_state(self):
        client = FakeClient()
        selector = ChoiceSelector(client)
        state = {"original_request": REQUEST, "observation": {"title": "Ready"}}
        questions = {
            "changed": Choice(instructions="Did anything change?", criteria={"yes": None, "no": None}),
            "done": Choice(instructions="Are we done?", criteria={"yes": None, "no": None}),
        }
        choices, trace = selector.judge(state, questions)
        self.assertEqual(choices, {"changed": "no", "done": "no"})
        self.assertEqual(trace["state"], state)
        self.assertEqual(len(client.calls), 1)
        state["observation"]["title"] = "Changed"
        self.assertEqual(trace["state"]["observation"]["title"], "Ready")

    def test_invalid_timing_answer_is_rejected_by_shared_validation(self):
        client = FakeClient(("unsupported",))
        with self.assertRaisesRegex(SelectionError, "unknown option label"):
            ChoiceSelector(client).select(
                REQUEST, candidates(2), stage="controls", settling_context=CONTEXT,
            )
        self.assertEqual(len(client.calls), 1)

    def test_none_final_control_has_no_winning_timing(self):
        client = FakeClient(group_choices={"controls_r1_g0": NONE_KEY})
        result = ChoiceSelector(client).select(
            REQUEST, candidates(2), stage="controls", settling_context=CONTEXT,
        )
        self.assertIsNone(result.winner)
        self.assertIsNone(result.settling_ms)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(client.calls[0]["questions"]), 3)
        self.assertEqual(set(result.rounds[0]["settling"]), {"control_0", "control_1"})
        self.assertNotIn(NONE_KEY, result.rounds[0]["settling"])

    def test_evidence_rounds_share_context_and_only_final_round_confirms(self):
        state = self.evidence_state()
        observed = candidates(112)
        client = FakeClient()
        result = ChoiceSelector(client).select(
            state, observed, stage="evidence", settling_context=CONTEXT,
        )
        self.assertEqual([round_["candidate_count"] for round_ in result.rounds], [112, 12, 2])
        self.assertEqual([len(call["questions"]) for call in client.calls], [12, 2, 2])
        self.assertIs(result.winner, observed[-1])
        self.assertTrue(result.verification)
        self.assertIsNone(result.settling_ms)
        for call in client.calls:
            self.assertIs(call["state"], state)
            self.assertEqual(call["model"], PINNED_MODEL)
            for key, question in call["questions"].items():
                self.assertNotIn("settling", key)
                if key.endswith("_confirmation"):
                    continue
                self.assertEqual(question.instructions, EVIDENCE_QUESTION)
                self.assertIn("last_control", question.instructions)
                self.assertIn("last_action", question.instructions)
                self.assertEqual(list(question.criteria)[-1], NONE_KEY)
                self.assertLessEqual(len(question.criteria), 11)
        final_call = client.calls[-1]
        confirmation = final_call["questions"]["evidence_r3_confirmation"]
        self.assertEqual(confirmation.instructions["question"], EVIDENCE_CONFIRMATION_QUESTION)
        self.assertEqual(confirmation.instructions["evidence_candidates"], {
            "control_99": observed[99].description, "control_111": observed[111].description,
        })
        self.assertEqual(confirmation.criteria, {"yes": None, "no": None})
        self.assertNotIn("private_token", str(confirmation.instructions))
        final_trace = result.rounds[-1]
        self.assertEqual(final_trace["verification"], {
            "question_key": "evidence_r3_confirmation", "choice": "yes", "verified": True,
        })
        answer = final_trace["api_call"]["answers"]["evidence_r3_confirmation"]
        self.assertEqual(answer["choice"], "yes")
        self.assertEqual(answer["probabilities"], {"yes": 0.7, "no": 0.3})
        state["last_control"]["status"] = "verified"
        observed[99].description["label"] = "Changed after the request"
        self.assertEqual(final_trace["api_call"]["state"]["last_control"]["status"], "unverified")
        self.assertEqual(
            final_trace["api_call"]["questions"]["evidence_r3_confirmation"]["instructions"]["evidence_candidates"]["control_99"]["label"],
            "Details 99",
        )

    def test_evidence_singleton_batches_selection_and_confirmation(self):
        for confirmation in ("yes", "no"):
            with self.subTest(confirmation=confirmation):
                observed = candidates(1)
                client = FakeClient(confirmation=confirmation)
                result = ChoiceSelector(client).select(self.evidence_state(), observed, stage="evidence")
                self.assertIs(result.winner, observed[0])
                self.assertEqual(result.verification, confirmation == "yes")
                self.assertIsNone(result.settling_ms)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(list(client.calls[0]["questions"]), ["evidence_r1_g0", "evidence_r1_confirmation"])
                self.assertEqual(result.rounds[0]["groups"][0]["options"], ["control_0", NONE_KEY])

    def test_sparse_evidence_advancement_still_confirms_surviving_singleton(self):
        client = FakeClient(group_choices={
            "evidence_r1_g0": NONE_KEY, "evidence_r1_g1": "control_10",
        })
        observed = candidates(11)
        result = ChoiceSelector(client).select(self.evidence_state(), observed, stage="evidence")
        self.assertEqual([round_["candidate_count"] for round_ in result.rounds], [11, 1])
        self.assertEqual([round_["winner_count"] for round_ in result.rounds], [1, 1])
        self.assertEqual([len(call["questions"]) for call in client.calls], [2, 2])
        self.assertIs(result.winner, observed[10])
        self.assertTrue(result.verification)
        final_questions = client.calls[-1]["questions"]
        self.assertEqual(list(final_questions["evidence_r2_g0"].criteria), ["control_10", NONE_KEY])
        self.assertEqual(final_questions["evidence_r2_confirmation"].instructions["evidence_candidates"], {
            "control_10": observed[10].description,
        })

    def test_none_evidence_cannot_be_overridden_by_yes_confirmation(self):
        client = FakeClient(group_choices={"evidence_r1_g0": NONE_KEY}, confirmation="yes")
        result = ChoiceSelector(client).select(self.evidence_state(), candidates(3), stage="evidence")
        self.assertIsNone(result.winner)
        self.assertFalse(result.verification)
        self.assertEqual(result.rounds[0]["verification"]["choice"], "yes")
        self.assertFalse(result.rounds[0]["verification"]["verified"])

    def test_empty_evidence_is_unverified_without_inference(self):
        client = FakeClient()
        result = ChoiceSelector(client).select(self.evidence_state(), [], stage="evidence")
        self.assertIsNone(result.winner)
        self.assertFalse(result.verification)
        self.assertEqual(result.rounds, [])
        self.assertEqual(client.calls, [])

    def test_confirmation_choice_is_used_without_a_probability_or_confidence_gate(self):
        class LowConfidenceClient(FakeClient):
            def system_one(self, **kwargs):
                response = super().system_one(**kwargs)
                confirmation = response.answers["evidence_r1_confirmation"]
                confirmation.confidence = 0.0
                confirmation.probabilities = {"yes": 0.01, "no": 0.99}
                return response

        result = ChoiceSelector(LowConfidenceClient()).select(
            self.evidence_state(), candidates(2), stage="evidence",
        )
        self.assertTrue(result.verification)


if __name__ == "__main__":
    unittest.main()
