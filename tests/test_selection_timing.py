"""Selection timing/correlation tests using fakes and offline SDK transport only."""

import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

import httpx2
from typesafe_sdk import RetryPolicy, TypeSafeClient, TypeSafeError

from src.reporting import RunReport
from src.selection import (
    ChoiceSelector, EVIDENCE_CONFIRMATION_QUESTION, NONE_KEY, PINNED_MODEL, SelectionError,
)
from tests.test_selection import EXPECTED_QUESTION, FakeClient, ORIGINAL, candidates


class SelectionTimingTests(unittest.TestCase):
    def setUp(self):
        # Inherit workspace ACLs, as in the existing report tests.
        self.workspace = Path(__file__).resolve().parents[1]
        self.path = self.workspace / f".selection-timing-tests-{uuid4().hex}"
        self.path.mkdir()
        self.addCleanup(self.cleanup_directory)

    def cleanup_directory(self):
        target = self.path.resolve()
        if target.parent != self.workspace or not target.name.startswith(".selection-timing-tests-"):
            raise ValueError("Unexpected test cleanup path")
        shutil.rmtree(target)

    def events(self, report):
        report.close()
        return json.loads(report.path.read_text(encoding="utf-8"))

    def assert_paired(self, events):
        starts = {e["span_id"]: e for e in events if e["event"] == "timing_started"}
        finishes = {e["span_id"]: e for e in events if e["event"] == "timing_finished"}
        self.assertEqual(set(starts), set(finishes))
        for span_id, start in starts.items():
            finish = finishes[span_id]
            self.assertEqual(start["operation"], finish["operation"])
            self.assertGreaterEqual(finish["duration_ms"], 0)
            self.assertGreaterEqual(finish["elapsed_ms"], start["elapsed_ms"])
            self.assertAlmostEqual(
                finish["duration_ms"],
                finish["finished_elapsed_ms"] - finish["started_elapsed_ms"],
            )
            self.assertIn(finish["status"], {"ok", "error", "interrupted"})
        return starts, finishes

    def test_timings_preserve_one_call_per_round_and_link_every_group(self):
        observed = candidates(41)
        plain = FakeClient()
        baseline = ChoiceSelector(plain).select(ORIGINAL, observed, stage="controls")
        timed = FakeClient()
        with RunReport(self.path) as report:
            result = ChoiceSelector(timed).select(
                ORIGINAL, observed, stage="controls", report=report,
            )
            events = self.events(report)
        self.assertIs(result.winner, baseline.winner)
        self.assertEqual(timed.calls, plain.calls)
        descriptions = {candidate.key: candidate.description for candidate in observed}
        for call in timed.calls:
            self.assertEqual(call["state"], ORIGINAL)
            self.assertEqual(call["model"], PINNED_MODEL)
            for key, question in call["questions"].items():
                self.assertRegex(key, r"^controls_r[1-9][0-9]*_g[0-9]+$")
                self.assertEqual(question.instructions, EXPECTED_QUESTION)
                self.assertEqual(
                    {key: value for key, value in question.criteria.items() if key != NONE_KEY},
                    {key: descriptions[key] for key in question.criteria if key != NONE_KEY},
                )
                self.assertIn("None of these candidates", question.criteria[NONE_KEY])
        self.assertEqual([len(call["questions"]) for call in timed.calls], [5, 1])
        starts, finishes = self.assert_paired(events)
        self.assertTrue(all(e["status"] == "ok" for e in finishes.values()))
        self.assertEqual(
            len([e for e in starts.values() if e["operation"] == "selection.sdk_call"]),
            len(timed.calls),
        )
        self.assertFalse(any("question" in e["operation"] for e in starts.values()))
        for round_trace in result.rounds:
            self.assertEqual(starts[round_trace["span_id"]]["round"], round_trace["round"])
            api_call = round_trace["api_call"]
            sdk = starts[api_call["sdk_span_id"]]
            validation = starts[api_call["response_validation_span_id"]]
            self.assertEqual(sdk["round_id"], round_trace["round_id"])
            self.assertEqual(sdk["question_keys"], api_call["question_keys"])
            self.assertEqual(sdk["stage"], "controls")
            self.assertLess(
                events.index(finishes[api_call["sdk_span_id"]]), events.index(validation),
            )
            for group in round_trace["groups"]:
                self.assertIn(group["question_key"], api_call["question_keys"])
                self.assertIn(group["winner"], group["options"])
        self.assertFalse(any(e["event"].startswith("selection_") for e in events))

    def test_http_questions_and_probabilities_share_the_single_round_call(self):
        wire_calls = []

        def handler(request):
            body = json.loads(request.content)
            wire_calls.append(body)
            answers = {}
            for key, question in body["questions"].items():
                labels = list(question["criteria"])
                winner = next(label for label in reversed(labels) if label != NONE_KEY)
                answers[key] = {
                    "type": "choice", "choice": winner, "confidence": 1.0,
                    "probabilities": {label: float(label == winner) for label in labels},
                }
            return httpx2.Response(200, json={
                "model": PINNED_MODEL, "answers": answers,
                "usage": {"input_tokens": 12, "output_tokens": 7},
            })

        with RunReport(self.path) as report:
            http_client = httpx2.Client(
                transport=httpx2.MockTransport(handler),
                event_hooks={"request": [report.on_request], "response": [report.on_response]},
            )
            with TypeSafeClient(
                api_key="offline-test-key", model=PINNED_MODEL,
                retry=RetryPolicy(max_retries=0), http_client=http_client,
            ) as client:
                result = ChoiceSelector(client).select(
                    ORIGINAL, candidates(21), stage="controls", report=report,
                )
            events = self.events(report)
        self.assertEqual(len(wire_calls), 2)
        self.assertEqual(len(wire_calls[0]["questions"]), 3)
        self.assertEqual(list(wire_calls[0]["questions"]["controls_r1_g2"]["criteria"]), ["candidate_0020", NONE_KEY])
        self.assertEqual(list(wire_calls[1]["questions"]), ["controls_r2_g0"])
        starts, _ = self.assert_paired(events)
        requests = [e for e in events if e["event"] == "jev_request"]
        responses = [e for e in events if e["event"] == "jev_response"]
        for request, response, round_trace in zip(requests, responses, result.rounds, strict=True):
            api_call = round_trace["api_call"]
            self.assertEqual(request["request_id"], response["request_id"])
            for event in (request, response):
                self.assertEqual(event["round_id"], round_trace["round_id"])
                self.assertEqual(event["question_keys"], api_call["question_keys"])
                self.assertNotIn("batch_id", event)
            self.assertEqual(list(request["body"]["questions"]), api_call["question_keys"])
            self.assertEqual(list(response["body"]["answers"]), api_call["question_keys"])
            self.assertEqual(starts[api_call["sdk_span_id"]]["round_id"], request["round_id"])
            for answer in response["body"]["answers"].values():
                self.assertIn("probabilities", answer)
                self.assertIn(NONE_KEY, answer["probabilities"])

    def test_evidence_http_final_batch_preserves_state_none_confirmation_and_trace(self):
        wire_calls = []
        state = {
            "original_request": ORIGINAL, "iteration": 2,
            "current_window": {"app_name": "Editor", "title": "Saved document"},
            "last_control": {"control": {"role": "Button", "label": "Save"}, "status": "unverified"},
            "last_action": {"tool": "click", "result": {"delivery": "returned"}},
        }
        observed = candidates(11)

        def handler(request):
            body = json.loads(request.content)
            wire_calls.append(body)
            answers = {}
            for key, question in body["questions"].items():
                labels = list(question["criteria"])
                winner = "yes" if key.endswith("_confirmation") else next(
                    label for label in reversed(labels) if label != NONE_KEY
                )
                answers[key] = {
                    "type": "choice", "choice": winner, "confidence": 0.7,
                    "probabilities": {label: float(label == winner) for label in labels},
                }
            return httpx2.Response(200, json={
                "model": PINNED_MODEL, "answers": answers,
                "usage": {"input_tokens": 120, "output_tokens": 17},
            })

        with RunReport(self.path) as report:
            http_client = httpx2.Client(
                transport=httpx2.MockTransport(handler),
                event_hooks={"request": [report.on_request], "response": [report.on_response]},
            )
            with TypeSafeClient(
                api_key="offline-test-key", model=PINNED_MODEL,
                retry=RetryPolicy(max_retries=0), http_client=http_client,
            ) as client:
                result = ChoiceSelector(client).select(state, observed, stage="evidence", report=report)
            events = self.events(report)
        self.assertIs(result.winner, observed[-1])
        self.assertTrue(result.verification)
        self.assertIsNone(result.settling_ms)
        self.assertEqual([len(call["questions"]) for call in wire_calls], [2, 2])
        for call in wire_calls:
            self.assertEqual(call["state"], state)
            self.assertEqual(call["model"], PINNED_MODEL)
            for key, question in call["questions"].items():
                self.assertNotIn("settling", key)
                if not key.endswith("_confirmation"):
                    self.assertIn(NONE_KEY, question["criteria"])
        confirmation = wire_calls[-1]["questions"]["evidence_r2_confirmation"]
        self.assertEqual(confirmation["instructions"]["question"], EVIDENCE_CONFIRMATION_QUESTION)
        self.assertEqual(confirmation["instructions"]["evidence_candidates"], {
            observed[9].key: observed[9].description, observed[10].key: observed[10].description,
        })
        self.assertEqual(confirmation["criteria"], {"yes": None, "no": None})
        requests = [event for event in events if event["event"] == "jev_request"]
        responses = [event for event in events if event["event"] == "jev_response"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(responses), 2)
        starts, _ = self.assert_paired(events)
        for request, response, round_ in zip(requests, responses, result.rounds, strict=True):
            api_call = round_["api_call"]
            self.assertEqual(request["body"]["state"], api_call["state"])
            self.assertEqual(request["request_id"], response["request_id"])
            self.assertEqual(request["question_keys"], api_call["question_keys"])
            self.assertEqual(starts[api_call["sdk_span_id"]]["stage"], "evidence")
        final_trace = result.rounds[-1]["api_call"]
        self.assertEqual(final_trace["answers"]["evidence_r2_confirmation"]["probabilities"], {"yes": 1.0, "no": 0.0})
        self.assertIn(NONE_KEY, final_trace["answers"]["evidence_r2_g0"]["probabilities"])
        state["last_control"]["status"] = "verified"
        self.assertEqual(final_trace["state"]["last_control"]["status"], "unverified")
        self.assertEqual(requests[-1]["body"]["state"]["last_control"]["status"], "unverified")

    def test_round_call_failure_closes_timings_and_propagates_original_error(self):
        client = FakeClient()
        system_one = client.system_one

        def fail_second(**kwargs):
            if client.calls:
                raise TypeSafeError("private failure details")
            return system_one(**kwargs)

        client.system_one = fail_second
        with RunReport(self.path) as report:
            with self.assertRaises(SelectionError):
                ChoiceSelector(client).select(
                    ORIGINAL, candidates(40), stage="controls", report=report,
                )
            events = self.events(report)
            self.assertNotIn("private failure details", report.path.read_text(encoding="utf-8"))
        _, finishes = self.assert_paired(events)
        calls = [e for e in finishes.values() if e["operation"] == "selection.sdk_call"]
        self.assertEqual(
            [(e["round_id"], e["status"]) for e in calls],
            [("controls_r1", "ok"), ("controls_r2", "error")],
        )
        self.assertFalse(any(e["event"].startswith("selection_") for e in events))

    def test_round_interruption_closes_timings_and_propagates(self):
        client = FakeClient()
        system_one = client.system_one
        interruption = KeyboardInterrupt("private interruption details")

        def interrupt_second_round(**kwargs):
            if client.calls:
                raise interruption
            return system_one(**kwargs)

        client.system_one = interrupt_second_round
        with RunReport(self.path) as report:
            with self.assertRaises(KeyboardInterrupt) as caught:
                ChoiceSelector(client).select(ORIGINAL, candidates(20), stage="windows", report=report)
            self.assertIs(caught.exception, interruption)
            events = self.events(report)
        _, finishes = self.assert_paired(events)
        rounds = [e for e in finishes.values() if e["operation"] == "selection.round"]
        self.assertEqual(
            [(e["round_id"], e["status"]) for e in rounds],
            [("windows_r1", "ok"), ("windows_r2", "interrupted")],
        )
        interrupted = [e for e in finishes.values() if e.get("error_type") == "KeyboardInterrupt"]
        self.assertEqual(
            {e["operation"] for e in interrupted},
            {"selection", "selection.round", "selection.sdk_call"},
        )
        self.assertTrue(all(e["status"] == "interrupted" for e in interrupted))

    def test_response_validation_failure_has_separate_returned_sdk_interval(self):
        client = FakeClient(mutate=lambda response: setattr(response, "model", "wrong-model"))
        with RunReport(self.path) as report:
            with self.assertRaises(SelectionError):
                ChoiceSelector(client).select(ORIGINAL, candidates(2), stage="controls", report=report)
            events = self.events(report)
        _, finishes = self.assert_paired(events)
        sdk = next(e for e in finishes.values() if e["operation"] == "selection.sdk_call")
        validation = next(e for e in finishes.values() if e["operation"] == "selection.response_validation")
        self.assertNotIn("error_type", sdk)
        self.assertEqual(sdk["status"], "ok")
        self.assertEqual(validation["error_type"], "SelectionError")
        self.assertEqual(validation["status"], "error")
        self.assertFalse(any(e["event"].startswith("selection_") for e in events))

    def test_invalid_selection_does_not_issue_requests(self):
        for prompt in ("", "   "):
            with self.subTest(prompt=prompt), RunReport(self.path) as report:
                client = FakeClient()
                with self.assertRaises(ValueError):
                    ChoiceSelector(client).select(
                        prompt, candidates(2), stage="windows", report=report,
                    )
                events = self.events(report)
                self.assertEqual(client.calls, [])
                starts, finishes = self.assert_paired(events)
                self.assertFalse(any(e["operation"] == "selection.sdk_call" for e in starts.values()))
                failed = next(e for e in finishes.values() if e["operation"] == "selection.validation")
                self.assertIn("error_type", failed)

    def test_empty_selection_or_singleton_window_has_no_inference(self):
        for count, stage in ((0, "controls"), (0, "windows"), (0, "evidence"), (1, "windows")):
            with self.subTest(count=count, stage=stage), RunReport(self.path) as report:
                client = FakeClient()
                result = ChoiceSelector(client).select(
                    ORIGINAL, candidates(count), stage=stage, report=report,
                )
                self.assertEqual(client.calls, [])
                self.assertEqual(result.rounds, [])
                starts, _ = self.assert_paired(self.events(report))
                self.assertEqual(
                    {e["operation"] for e in starts.values()}, {"selection", "selection.validation"},
                )

    def test_singleton_control_has_one_timed_inference_against_none(self):
        client = FakeClient()
        with RunReport(self.path) as report:
            result = ChoiceSelector(client).select(
                ORIGINAL, candidates(1), stage="controls", report=report,
            )
            starts, finishes = self.assert_paired(self.events(report))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["groups"][0]["options"], ["candidate_0000", NONE_KEY])
        sdk = [event for event in starts.values() if event["operation"] == "selection.sdk_call"]
        self.assertEqual(len(sdk), 1)
        self.assertEqual(sdk[0]["question_keys"], ["controls_r1_g0"])
        self.assertEqual(finishes[sdk[0]["span_id"]]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
