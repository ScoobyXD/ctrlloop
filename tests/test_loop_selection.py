"""Final control selection batches conditional settling questions without live calls."""

from types import SimpleNamespace
import unittest

from typesafe_sdk import Choice

from src.selection import Candidate, ChoiceSelector, PINNED_MODEL, SETTLING_MS, SelectionError


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
    def __init__(self, timing_labels=("100ms",), *, choose_first=False):
        self.calls = []
        self.timing_labels = timing_labels
        self.choose_first = choose_first

    def system_one(self, *, state, questions, model):
        self.calls.append({"state": state, "questions": questions, "model": model})
        answers = {}
        timing_index = 0
        for key, question in questions.items():
            labels = list(question.criteria)
            if labels == list(SETTLING_MS):
                chosen = self.timing_labels[timing_index % len(self.timing_labels)]
                timing_index += 1
            else:
                chosen = labels[0] if self.choose_first else labels[-1]
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
        self.assertEqual(set(final["settling"]), set(final["groups"][0]["options"]))
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
                self.assertEqual(len(client.calls[0]["questions"]), 1)
                self.assertIsNone(result.rounds[0]["groups"][0]["question_key"])
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
                self.assertEqual(client.calls, [])
                self.assertEqual(result.rounds, [])
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


if __name__ == "__main__":
    unittest.main()
