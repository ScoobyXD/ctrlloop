"""Offline checks for registry-defined windows and the common selection pipeline."""

import json
from types import SimpleNamespace
import unittest

from src.pipeline import run_pipeline, window_candidates
from src.selection import Candidate, ChoiceSelector, PINNED_MODEL
from src.synthetic import (
    DEFAULT_REGISTRY, START_MENU_KEY,
    SyntheticRegistry, SyntheticWindow,
)


def candidate(key, **record):
    return Candidate(key=key, description={"label": key}, record=record)


def definition(key="launcher", *, controls=None, matches=None):
    if controls is None:
        controls = (candidate("search", role="Edit", actions=["type_text"]),)
    return SyntheticWindow(
        window=candidate(key, title="Application launcher"),
        controls=tuple(controls), matches_observed=matches,
    )


class ChoosingClient:
    def __init__(self, *, window="launcher", control="search"):
        self.winners = {"windows": window, "controls": control}
        self.calls = []

    def system_one(self, **request):
        self.calls.append(request)
        answers = {}
        for key, question in request["questions"].items():
            choice = self.winners[key.split("_")[0]]
            options = list(question.criteria)
            if choice not in options:
                raise AssertionError(f"Expected {choice!r} in {options!r}")
            answers[key] = SimpleNamespace(
                type="choice", choice=choice, confidence=1,
                probabilities={option: float(option == choice) for option in options},
            )
        return SimpleNamespace(
            model=PINNED_MODEL, answers=answers,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


class ObservingDriver:
    def __init__(self, windows=(), snapshot=None):
        self.windows = list(windows)
        self.snapshot = snapshot
        self.calls = []

    def list_windows(self):
        self.calls.append(("list_windows",))
        return self.windows

    def get_window_state(self, window):
        self.calls.append(("get_window_state", window))
        if self.snapshot is None:
            raise AssertionError("Synthetic windows must not request a driver snapshot")
        return self.snapshot


class RecordingReport:
    def __init__(self):
        self.events = []

    def append(self, event, **data):
        self.events.append(json.loads(json.dumps({"event": event, **data})))


class SyntheticRegistryTests(unittest.TestCase):
    def test_unknown_window_and_registered_empty_controls_are_distinct(self):
        registry = SyntheticRegistry([definition(controls=())])
        self.assertEqual(registry.controls_for("launcher"), [])
        self.assertIsNone(registry.controls_for("unknown"))
        self.assertFalse(registry.replaces({"title": "Application launcher"}))

    def test_registry_marks_returned_records_synthetic(self):
        registry = SyntheticRegistry([SyntheticWindow(
            window=candidate("launcher", synthetic=False),
            controls=(candidate("search", synthetic=False),),
        )])
        self.assertIs(registry.windows()[0].record["synthetic"], True)
        self.assertIs(registry.controls_for("launcher")[0].record["synthetic"], True)

    def test_constructor_copies_definitions_and_nested_candidate_data(self):
        window = Candidate("launcher", {"hint": {"labels": ["original"]}},
                           {"title": "Original", "metadata": {"names": ["original"]}})
        control = Candidate("search", {"hint": {"labels": ["original"]}},
                            {"actions": ["type_text"]})
        definitions = [SyntheticWindow(window, (control,))]
        registry = SyntheticRegistry(definitions)
        definitions.clear()
        window.description["hint"]["labels"].append("caller change")
        window.record["metadata"]["names"].append("caller change")
        control.description["hint"]["labels"].append("caller change")
        control.record["actions"].clear()

        stored_window = registry.windows()[0]
        stored_control = registry.controls_for("launcher")[0]
        self.assertEqual(stored_window.description["hint"]["labels"], ["original"])
        self.assertEqual(stored_window.record["metadata"]["names"], ["original"])
        self.assertEqual(stored_control.description["hint"]["labels"], ["original"])
        self.assertEqual(stored_control.record["actions"], ["type_text"])
        self.assertNotIn("synthetic", window.record)
        self.assertNotIn("synthetic", control.record)

    def test_returned_candidates_are_fresh_deep_copies(self):
        registry = SyntheticRegistry([SyntheticWindow(
            Candidate("launcher", {"tags": ["launch"]}, {"title": "Launcher"}),
            (Candidate("search", {"tags": ["search"]}, {"actions": ["type_text"]}),),
        )])
        windows = registry.windows()
        controls = registry.controls_for("launcher")
        windows[0].description["tags"].append("changed")
        windows[0].record["title"] = "Changed"
        controls[0].description["tags"].append("changed")
        controls[0].record["actions"].clear()
        windows.clear()
        controls.clear()

        self.assertEqual(registry.windows()[0].description, {"tags": ["launch"]})
        self.assertEqual(registry.windows()[0].record["title"], "Launcher")
        self.assertEqual(registry.controls_for("launcher")[0].description, {"tags": ["search"]})
        self.assertEqual(registry.controls_for("launcher")[0].record["actions"], ["type_text"])

    def test_invalid_and_reserved_window_keys_are_rejected(self):
        for key in ("", None, "window_0", "window_custom"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                SyntheticRegistry([definition(key)])

    def test_duplicate_window_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            SyntheticRegistry([definition("same"), definition("same")])

    def test_invalid_and_duplicate_control_keys_are_rejected(self):
        for controls in (
            (candidate(""),), (candidate(None),),
            (candidate("same"), candidate("same")),
        ):
            with self.subTest(keys=[control.key for control in controls]), self.assertRaises(ValueError):
                SyntheticRegistry([definition(controls=controls)])

    def test_matcher_callable_does_not_need_to_support_deepcopy(self):
        class Matcher:
            def __deepcopy__(self, memo):
                raise AssertionError("The registry must not copy callable implementation state")

            def __call__(self, window):
                return window.get("app_name") == "Launcher.exe"

        registry = SyntheticRegistry([definition(matches=Matcher())])
        self.assertTrue(registry.replaces({"app_name": "Launcher.exe"}))
        self.assertFalse(registry.replaces({"app_name": "Editor.exe"}))

    def test_control_keys_can_be_reused_in_different_windows(self):
        registry = SyntheticRegistry([definition("first"), definition("second")])
        self.assertEqual([window.key for window in registry.windows()], ["first", "second"])
        self.assertEqual(registry.controls_for("first")[0].key, "search")
        self.assertEqual(registry.controls_for("second")[0].key, "search")


class SyntheticPipelineTests(unittest.TestCase):
    def test_second_synthetic_window_uses_normal_control_judgment_and_reporting(self):
        start = SyntheticWindow(
            DEFAULT_REGISTRY.windows()[0],
            tuple(DEFAULT_REGISTRY.controls_for(START_MENU_KEY)),
        )
        launcher = definition(controls=(
            candidate("browse", role="Button", label="Browse", target="launcher_browse"),
            candidate("search", role="Edit", label="Search", target="launcher_search"),
        ))
        registry = SyntheticRegistry([start, launcher])
        real = {"app_name": "Editor", "title": "Document", "pid": 123, "window_id": 456}
        driver = ObservingDriver([real])
        client = ChoosingClient()
        report = RecordingReport()
        request = "  Search with my launcher.\r\n"
        result = run_pipeline(
            request, driver, ChoiceSelector(client), synthetic=registry, report=report,
        )

        self.assertEqual(result.status, "selected")
        self.assertEqual(result.window["title"], "Application launcher")
        self.assertIs(result.window["synthetic"], True)
        self.assertEqual(result.control["target"], "launcher_search")
        self.assertIs(result.control["synthetic"], True)
        self.assertIsNone(result.snapshot_id)
        self.assertEqual(result.observation["source"], "synthetic")
        self.assertEqual(driver.calls, [("list_windows",)])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(list(client.calls[0]["questions"]), ["windows_r1_g0"])
        self.assertEqual(list(client.calls[1]["questions"]), ["controls_r1_g0"])
        self.assertEqual(list(client.calls[0]["questions"]["windows_r1_g0"].criteria),
                         [START_MENU_KEY, "launcher", "window_0"])
        self.assertEqual(list(client.calls[1]["questions"]["controls_r1_g0"].criteria),
                         ["browse", "search"])
        for call in client.calls:
            self.assertEqual(call["state"], request)
            self.assertEqual(call["model"], PINNED_MODEL)
        self.assertEqual(result.selections["controls"][0]["groups"][0]["winner"], "search")
        self.assertEqual([entry["event"] for entry in report.events],
                         ["windows_discovered", "window_selected", "control_selected"])
        self.assertEqual(report.events[0]["windows"], [real])
        self.assertEqual(report.events[1]["window"], result.window)
        self.assertEqual(report.events[2]["control"], result.control)
        self.assertEqual(report.events[2]["selections"], result.selections["controls"])

    def test_custom_deduplication_preserves_remaining_real_identity_keys_and_order(self):
        registry = SyntheticRegistry([definition(
            matches=lambda window: window.get("app_name") == "Launcher.exe",
        )])
        first = {"app_name": "Editor", "title": "First", "window_id": 101}
        duplicate = {"app_name": "Launcher.exe", "title": "Launcher", "window_id": 102}
        last = {"app_name": "Editor", "title": "Launcher", "window_id": 103}
        candidates = window_candidates([first, duplicate, last], synthetic=registry)

        self.assertTrue(registry.replaces(duplicate))
        self.assertFalse(registry.replaces(last))
        self.assertEqual([item.key for item in candidates], ["launcher", "window_0", "window_2"])
        self.assertIs(candidates[1].record, first)
        self.assertIs(candidates[2].record, last)
        self.assertEqual(duplicate["window_id"], 102)

    def test_empty_registry_observes_real_start_window_normally(self):
        start = {"app_name": "StartMenuExperienceHost.exe", "title": "Start", "pid": 1, "window_id": 2}
        control = {"element_index": 7, "role": "Edit", "label": "Search", "element_token": "real:7"}
        driver = ObservingDriver([start], {"snapshot_id": "real", "elements": [control]})
        client = ChoosingClient()
        result = run_pipeline("Search Start", driver, ChoiceSelector(client), synthetic=SyntheticRegistry([]))

        self.assertEqual(result.status, "selected")
        self.assertIs(result.window, start)
        self.assertIs(result.control, control)
        self.assertEqual(result.snapshot_id, "real")
        self.assertNotIn("synthetic", result.window)
        self.assertEqual(driver.calls, [("list_windows",), ("get_window_state", start)])
        self.assertEqual(client.calls, [])

    def test_empty_registry_and_no_real_windows_returns_no_windows(self):
        driver = ObservingDriver()
        client = ChoosingClient()
        report = RecordingReport()
        result = run_pipeline(
            "Open launcher", driver, ChoiceSelector(client),
            synthetic=SyntheticRegistry([]), report=report,
        )
        self.assertEqual(result.status, "no_windows")
        self.assertIsNone(result.window)
        self.assertIsNone(result.control)
        self.assertEqual(driver.calls, [("list_windows",)])
        self.assertEqual(client.calls, [])
        self.assertEqual(report.events, [{"event": "windows_discovered", "windows": []}])

    def test_registered_window_with_no_controls_returns_no_controls(self):
        registry = SyntheticRegistry([definition(controls=())])
        driver = ObservingDriver()
        client = ChoosingClient()
        report = RecordingReport()
        result = run_pipeline(
            "Use launcher", driver, ChoiceSelector(client), synthetic=registry, report=report,
        )
        self.assertEqual(result.status, "no_controls")
        self.assertEqual(result.window["title"], "Application launcher")
        self.assertIsNone(result.control)
        self.assertIsNone(result.snapshot_id)
        self.assertEqual(result.observation["source"], "synthetic")
        self.assertTrue(result.reason)
        self.assertEqual(result.selections, {"windows": [], "controls": []})
        self.assertEqual(driver.calls, [("list_windows",)])
        self.assertEqual(client.calls, [])
        self.assertEqual([entry["event"] for entry in report.events],
                         ["windows_discovered", "window_selected"])

    def test_mutating_one_pipeline_result_does_not_change_later_results(self):
        registry = SyntheticRegistry([definition()])
        client = ChoosingClient()
        first = run_pipeline("Use launcher", ObservingDriver(), ChoiceSelector(client), synthetic=registry)
        first.window["title"] = "Changed"
        first.control["actions"].clear()
        second = run_pipeline("Use launcher", ObservingDriver(), ChoiceSelector(client), synthetic=registry)

        self.assertEqual(second.window["title"], "Application launcher")
        self.assertEqual(second.control["actions"], ["type_text"])
        self.assertIsNot(first.window, second.window)
        self.assertIsNot(first.control, second.control)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
