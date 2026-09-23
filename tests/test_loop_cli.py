"""Default loop CLI behavior without native actions."""

import contextlib
import io
import unittest
from unittest.mock import patch

from src import main as cli
from src.pipeline import PipelineResult
import test_pipeline


class LoopCliTests(unittest.TestCase):
    cli = cli
    invoke = test_pipeline.CliTests.invoke

    def test_request_runs_loop_by_default_without_execution_modes(self):
        result = PipelineResult("action_verified", iterations=[{"iteration": 2, "route": "verified"}])
        status, output, pipeline, _, _ = self.invoke(["Save document"], result=result)
        self.assertEqual(status, 0)
        self.assertEqual(output["status"], "action_verified")
        self.assertEqual(output["iterations"], result.iterations)
        pipeline.assert_called_once()
        self.assertEqual(pipeline.call_args.args[0], "Save document")
        self.assertEqual(pipeline.call_args.kwargs, {"report": self.report})
        self.assertEqual(self.report.events[0], {
            "event": "run_started", "request": "Save document",
        })
        self.assertTrue(self.report.closed)

    def test_unfinished_loop_returns_exit_two_and_existing_report_format(self):
        for reason in ("no_action", "no_control_selected", "no_evidence", "action_unverified"):
            with self.subTest(reason=reason):
                result = PipelineResult(reason, reason="Fixture stop")
                status, output, pipeline, _, _ = self.invoke(["Save document"], result=result)
                self.assertEqual(status, 2)
                self.assertEqual(output, {**result.to_dict(), "timing": self.report.timing})
                self.assertEqual(self.report.events[-1], {
                    "event": "run_finished", "result": result.to_dict(),
                })
                self.assertEqual(pipeline.call_args.kwargs, {"report": self.report})

    def test_removed_execution_and_limit_flags_fail_before_driver_or_network(self):
        for arguments in (
            ["--loop", "Save document"],
            ["--click", "Save document"],
            ["--max-iterations", "20", "Save document"],
        ):
            with self.subTest(arguments=arguments), contextlib.ExitStack() as stack:
                driver = stack.enter_context(patch.object(cli, "CuaDriver"))
                sdk = stack.enter_context(patch.object(cli, "TypeSafeClient"))
                http = stack.enter_context(patch.object(cli.httpx2, "Client"))
                pipeline = stack.enter_context(patch.object(cli, "run_pipeline"))
                report = stack.enter_context(patch.object(cli, "RunReport"))
                stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                with self.assertRaises(SystemExit) as raised:
                    cli.main(arguments)
                self.assertEqual(raised.exception.code, 2)
                for mocked in (driver, sdk, http, pipeline, report):
                    mocked.assert_not_called()

    def test_final_report_failure_retains_native_action_facts_and_loop_exit_code(self):
        native_result = {"effect": "unverifiable", "route": "accessibility",
                         "delivery": {"mode": "background"}}
        for result_status, expected_exit in (("action_verified", 0), ("action_unverified", 2)):
            with self.subTest(result_status=result_status):
                result = PipelineResult(result_status, action_result=native_result)
                with patch.object(test_pipeline.RecordingReport, "close",
                                  side_effect=OSError("private-report-path")) as close:
                    status, output, pipeline, _, _ = self.invoke(["Save document"], result=result)
                close.assert_called_once()
                pipeline.assert_called_once()
                self.assertEqual(status, expected_exit)
                self.assertEqual(output, result.to_dict())
                self.assertEqual(output["action_result"], native_result)
                self.assertIn("returning the native action result", self.stderr.getvalue())
                self.assertNotIn("private-report-path", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
