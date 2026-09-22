"""Policy and boundary tests for Choice elimination; no external API calls."""

import json
from types import SimpleNamespace
import unittest

import httpx2
from typesafe_sdk import Choice, TypeSafeAPIError, TypeSafeError

from src.selection import (
    Candidate,
    ChoiceSelector,
    GROUP_SIZE,
    PINNED_MODEL,
    QUESTION,
    SelectionError,
)


ORIGINAL = "  Find the user’s ‘Save’ control.\nKeep this original request unchanged.  "
EXPECTED_QUESTION = (
    "Which of these candidates is best suited to help fulfill the user’s original "
    "request, based on the provided candidate information?"
)


def candidates(count):
    return [
        Candidate(
            key=f"candidate_{index:04d}",
            description={"label": f"Observed {index:04d}"},
            record={"local_identity": index, "private_context": "not model state"},
        )
        for index in range(count)
    ]


class FakeClient:
    def __init__(self, mutate=None, choose=None):
        self.calls = []
        self.mutate = mutate
        self.choose = choose or (lambda labels: labels[-1])

    def system_one(self, *, state, questions, model):
        self.calls.append({"state": state, "questions": questions, "model": model})
        answers = {}
        for key, question in questions.items():
            labels = list(question.criteria)
            winner = self.choose(labels)
            answers[key] = SimpleNamespace(
                type="choice",
                choice=winner,
                probabilities={label: float(label == winner) for label in labels},
                confidence=1.0,
            )
        response = SimpleNamespace(
            model=PINNED_MODEL,
            answers=answers,
            usage=SimpleNamespace(input_tokens=101, output_tokens=17),
        )
        if self.mutate:
            self.mutate(response)
        return response


class SelectionTests(unittest.TestCase):
    def assert_request_contract(self, client, observed):
        descriptions = {candidate.key: candidate.description for candidate in observed}
        for call in client.calls:
            self.assertEqual(call["state"], ORIGINAL)
            self.assertEqual(call["model"], "jev-1.13.0")
            for question in call["questions"].values():
                self.assertIsInstance(question, Choice)
                self.assertEqual(question.instructions, EXPECTED_QUESTION)
                self.assertLessEqual(len(question.criteria), 10)
                self.assertGreaterEqual(len(question.criteria), 1)
                for key, description in question.criteria.items():
                    self.assertEqual(description, descriptions[key])
                    self.assertNotIn("private_context", description)

    def test_fixed_experiment_constants(self):
        self.assertEqual(GROUP_SIZE, 10)
        self.assertEqual(PINNED_MODEL, "jev-1.13.0")
        self.assertEqual(QUESTION, EXPECTED_QUESTION)

    def test_required_candidate_counts_and_one_winner_per_group(self):
        cases = {
            0: [], 1: [], 9: [9], 10: [10], 11: [11, 2],
            392: [392, 40, 4], 1000: [1000, 100, 10],
        }
        for count, round_counts in cases.items():
            with self.subTest(count=count):
                observed = candidates(count)
                client = FakeClient()
                result = ChoiceSelector(client).select(ORIGINAL, observed, stage="windows")
                self.assertIs(result.winner, observed[-1] if observed else None)
                self.assertEqual([r["candidate_count"] for r in result.rounds], round_counts)
                self.assertEqual(len(client.calls), len(round_counts))
                expected_keys = [candidate.key for candidate in observed]
                for round_trace, call in zip(result.rounds, client.calls, strict=True):
                    expected_groups = [expected_keys[i : i + 10] for i in range(0, len(expected_keys), 10)]
                    self.assertEqual([g["options"] for g in round_trace["groups"]], expected_groups)
                    expected_keys = [group[-1] for group in expected_groups]
                    self.assertEqual([g["winner"] for g in round_trace["groups"]], expected_keys)
                    self.assertEqual(round_trace["winner_count"], len(expected_groups))
                    self.assertEqual(round_trace["group_count"], len(expected_groups))
                    asked_keys = [g["question_key"] for g in round_trace["groups"]]
                    api_call = round_trace["api_call"]
                    self.assertEqual(api_call["question_keys"], asked_keys)
                    self.assertEqual(list(call["questions"]), asked_keys)
                    self.assertEqual(api_call["requested_model"], PINNED_MODEL)
                    self.assertEqual(api_call["response_model"], PINNED_MODEL)
                    self.assertEqual(api_call["input_tokens"], 101)
                    self.assertEqual(api_call["output_tokens"], 17)
                self.assert_request_contract(client, observed)

    def test_same_request_and_question_for_both_stages(self):
        for stage in ("windows", "controls"):
            with self.subTest(stage=stage):
                observed = candidates(123)
                client = FakeClient()
                ChoiceSelector(client).select(ORIGINAL, observed, stage=stage)
                self.assert_request_contract(client, observed)

    def test_392_candidates_use_one_call_per_round_with_all_group_questions(self):
        observed = candidates(392)
        # Exceed the former 48,000-byte heuristic without changing descriptions.
        for candidate in observed:
            candidate.description["context"] = "x" * 1000
        client = FakeClient()
        result = ChoiceSelector(client).select(ORIGINAL, observed, stage="controls")
        self.assertEqual([len(call["questions"]) for call in client.calls], [40, 4, 1])
        self.assertEqual([len(q.criteria) for q in client.calls[0]["questions"].values()], [10] * 39 + [2])
        self.assertEqual(
            [list(call["questions"]) for call in client.calls],
            [round_trace["api_call"]["question_keys"] for round_trace in result.rounds],
        )
        self.assertEqual(
            [key for q in client.calls[0]["questions"].values() for key in q.criteria],
            [candidate.key for candidate in observed],
        )
        expected_winners = [c.key for c in observed[9::10]] + [observed[-1].key]
        self.assertEqual(
            [key for q in client.calls[1]["questions"].values() for key in q.criteria],
            expected_winners,
        )
        self.assert_request_contract(client, observed)

    def test_trailing_singleton_gets_one_group_question_in_the_same_call(self):
        observed = candidates(11)
        client = FakeClient()
        result = ChoiceSelector(client).select(ORIGINAL, observed, stage="controls")
        self.assertEqual([len(call["questions"]) for call in client.calls], [2, 1])
        singleton = client.calls[0]["questions"]["controls_r1_g1"]
        self.assertEqual(singleton.criteria, {observed[-1].key: observed[-1].description})
        self.assertEqual(result.rounds[0]["groups"][1]["question_key"], "controls_r1_g1")

    def test_choice_controls_winner_even_at_zero_confidence(self):
        def flat_distribution(response):
            for answer in response.answers.values():
                answer.confidence = 0.0
                answer.probabilities = {key: 1.0 / len(answer.probabilities) for key in answer.probabilities}

        observed = candidates(100)
        client = FakeClient(mutate=flat_distribution)
        result = ChoiceSelector(client).select(ORIGINAL, observed, stage="controls")
        self.assertIs(result.winner, observed[-1])
        self.assertEqual(result.rounds[1]["groups"][0]["options"], [c.key for c in observed[9::10]])

    def test_group_order_is_observation_order_not_sorted_key_order(self):
        observed = list(reversed(candidates(24)))
        client = FakeClient()
        result = ChoiceSelector(client).select(ORIGINAL, observed, stage="windows")
        self.assertEqual(result.rounds[0]["groups"][0]["options"], [c.key for c in observed[:10]])
        self.assertIs(result.winner, observed[-1])

    def test_confidence_does_not_reorder_winners_between_groups(self):
        def different_confidence(response):
            for index, answer in enumerate(response.answers.values()):
                answer.confidence = 0.1 + 0.8 * index

        observed = candidates(20)
        client = FakeClient(mutate=different_confidence)
        result = ChoiceSelector(client).select(ORIGINAL, observed, stage="windows")
        final_question = next(iter(client.calls[-1]["questions"].values()))
        self.assertEqual(list(final_question.criteria), [observed[9].key, observed[19].key])
        self.assertIs(result.winner, observed[19])

    def test_large_prompt_or_group_is_submitted_unchanged_in_one_call(self):
        for prompt, observed in (("é" * 50_000, candidates(2)), (ORIGINAL, candidates(2))):
            with self.subTest(prompt_length=len(prompt)):
                if prompt == ORIGINAL:
                    observed[0].description["label"] = "x" * 50_000
                client = FakeClient()
                ChoiceSelector(client).select(prompt, observed, stage="controls")
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(client.calls[0]["state"], prompt)
                question = next(iter(client.calls[0]["questions"].values()))
                self.assertEqual(question.criteria, {c.key: c.description for c in observed})

    def test_service_rejecting_round_surfaces_error_without_splitting_or_dropping(self):
        class RejectingClient(FakeClient):
            def system_one(self, **kwargs):
                self.calls.append(kwargs)
                raise TypeSafeAPIError(status, {"detail": "private service detail"}, httpx2.Headers())

        for status in (413, 422):
            with self.subTest(status=status):
                client = RejectingClient()
                with self.assertRaisesRegex(SelectionError, f"complete round.*HTTP {status}") as caught:
                    ChoiceSelector(client).select(ORIGINAL, candidates(392), stage="controls")
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(len(client.calls[0]["questions"]), 40)
                self.assertNotIn("private service detail", str(caught.exception))

    def test_invalid_config_or_duplicate_identity_fails_without_api_calls(self):
        client = FakeClient()
        for model in ("jev", "jev-latest", "jev-1.12.0"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                ChoiceSelector(client, model=model)
        observed = candidates(2)
        with self.assertRaises(ValueError):
            ChoiceSelector(client).select(ORIGINAL, [observed[0], observed[0]], stage="windows")
        self.assertEqual(client.calls, [])

    def assert_bad_response(self, mutate):
        client = FakeClient(mutate=mutate)
        with self.assertRaises(SelectionError):
            ChoiceSelector(client).select(ORIGINAL, candidates(2), stage="controls")
        self.assertEqual(len(client.calls), 1)

    def test_wrong_or_missing_returned_model(self):
        for model in (None, "jev-latest", "jev-1.12.0"):
            with self.subTest(model=model):
                self.assert_bad_response(lambda response: setattr(response, "model", model))

    def test_answer_keys_must_match_exactly(self):
        self.assert_bad_response(lambda response: response.answers.clear())
        self.assert_bad_response(lambda response: response.answers.update(unasked=next(iter(response.answers.values()))))
        self.assert_bad_response(lambda response: setattr(response, "answers", []))

    def test_wrong_answer_type_or_unknown_option(self):
        for field, values in (("type", [None, "noul", "score"]), ("choice", [None, 0, "invented-option"])):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_bad_response(lambda response: setattr(next(iter(response.answers.values())), field, value))

    def test_probability_labels_values_and_total_are_validated(self):
        invalid_distributions = [
            None,
            {},
            {"candidate_0000": 1.0},
            {"candidate_0000": 0.0, "candidate_0001": 1.0, "invented": 0.0},
            {"candidate_0000": 0.0, "candidate_0001": 0.0},
        ]
        for value in (float("nan"), float("inf"), -0.1, 1.1, True, "1"):
            invalid_distributions.append({"candidate_0000": 0.0, "candidate_0001": value})
        for distribution in invalid_distributions:
            with self.subTest(distribution=distribution):
                self.assert_bad_response(lambda response: setattr(next(iter(response.answers.values())), "probabilities", distribution))

    def test_probability_total_accepts_up_to_three_percent_deviation(self):
        distributions = {
            "reported_99_percent": [0.08, 0.20, 0.05, 0.03, 0.04, 0.04, 0.04, 0.10, 0.20, 0.21],
            "lower_boundary": [0.1] * 9 + [0.07],
            "upper_boundary": [0.1] * 9 + [0.13],
        }
        for name, values in distributions.items():
            with self.subTest(distribution=name):
                def set_distribution(response):
                    answer = next(iter(response.answers.values()))
                    answer.probabilities = dict(zip(answer.probabilities, values, strict=True))

                observed = candidates(10)
                client = FakeClient(mutate=set_distribution, choose=lambda labels: labels[1])
                result = ChoiceSelector(client).select(ORIGINAL, observed, stage="controls")
                self.assertIs(result.winner, observed[1])

    def test_probability_total_rejects_more_than_three_percent_deviation(self):
        for last_probability in (0.069, 0.131):
            with self.subTest(last_probability=last_probability):
                def set_distribution(response):
                    answer = next(iter(response.answers.values()))
                    values = [0.1] * 9 + [last_probability]
                    answer.probabilities = dict(zip(answer.probabilities, values, strict=True))

                client = FakeClient(mutate=set_distribution)
                with self.assertRaisesRegex(SelectionError, "more than 3%"):
                    ChoiceSelector(client).select(ORIGINAL, candidates(10), stage="controls")
                self.assertEqual(len(client.calls), 1)

    def test_confidence_must_be_finite_probability(self):
        for value in (None, float("nan"), float("inf"), -0.1, 1.1, True, "1"):
            with self.subTest(value=value):
                self.assert_bad_response(lambda response: setattr(next(iter(response.answers.values())), "confidence", value))

    def test_usage_must_be_nonnegative_integer(self):
        for field in ("input_tokens", "output_tokens"):
            for value in (None, -1, 1.5, True, "100"):
                with self.subTest(field=field, value=value):
                    self.assert_bad_response(lambda response: setattr(response.usage, field, value))

    def test_zero_usage_is_valid(self):
        client = FakeClient(mutate=lambda response: setattr(response, "usage", SimpleNamespace(input_tokens=0, output_tokens=0)))
        result = ChoiceSelector(client).select(ORIGINAL, candidates(2), stage="windows")
        self.assertIsNotNone(result.winner)

    def test_unknown_wire_answer_cannot_be_hidden_by_sdk_decoding(self):
        def add_wire_response(response):
            wire_answers = {key: {"type": "choice"} for key in response.answers}
            wire_answers["unrequested"] = {"type": "future-unknown-kind"}
            response.raw_http_response = SimpleNamespace(json=lambda: {"answers": wire_answers})

        self.assert_bad_response(add_wire_response)

    def test_malformed_wire_json_fails(self):
        def malformed_json():
            raise json.JSONDecodeError("invalid", "broken", 0)

        self.assert_bad_response(lambda response: setattr(response, "raw_http_response", SimpleNamespace(json=malformed_json)))

    def test_sdk_error_is_sanitized(self):
        class FailingClient:
            def system_one(self, **kwargs):
                raise TypeSafeError("sensitive request body and fake-secret")

        with self.assertRaises(SelectionError) as caught:
            ChoiceSelector(FailingClient()).select(ORIGINAL, candidates(2), stage="controls")
        self.assertNotIn("fake-secret", str(caught.exception))
        self.assertNotIn("sensitive request", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
