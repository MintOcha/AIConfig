import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kaggle.api.kaggle_api_extended import KaggleApi

SPEC = importlib.util.spec_from_file_location("kaggle_mcp", Path(__file__).parents[1] / "mcp/kaggle_mcp.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class RunSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_reports_each_active_version(self):
        runs = [dict(version=v, status="RUNNING", logs=[]) for v in (2, 4)]
        with patch.object(module, "_discover_runs", return_value=runs):
            report = await module.view_status("owner/notebook", fetch_logs=False)
        self.assertIn("version 2", report)
        self.assertIn("version 4", report)

    async def test_ambiguous_cancel_sends_nothing(self):
        runs = [dict(version=v, status="RUNNING") for v in (2, 4)]
        with patch.object(module, "_discover_runs", return_value=runs), patch.object(module, "_query_process") as query:
            with self.assertRaisesRegex(ValueError, "Multiple active"):
                await module.cancel_run("owner/notebook")
            query.assert_not_called()

    async def exercise_cancel(self, *, location="https://api.kaggle.com/v1/kernels/output/download_zip/987654", status="RUNNING", dry_run=False, backend_error=""):
        sent = []
        class Response:
            status_code = 302
            headers = {"Location": location}
            def close(self):
                pass
        class Session:
            def get(self, url, **kwargs):
                if not url.endswith("/owner/notebook") or kwargs["params"] != {"versionNumber": 3}:
                    raise AssertionError("Wrong notebook/version lookup")
                return Response()
        class Client:
            def __init__(self):
                self._http_client = SimpleNamespace(_init_session=lambda: None, _session=Session())
                self.kernels = SimpleNamespace(kernels_api_client=self)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def get_kernel_session_status(self, request):
                if (request.user_name, request.kernel_slug, request.version_label) != ("owner", "notebook", "v3"):
                    raise AssertionError("Wrong status target")
                return SimpleNamespace(status=SimpleNamespace(name=status))
            def cancel_kernel_session(self, request):
                sent.append(request.kernel_session_id)
                return SimpleNamespace(error_message=backend_error)
        async def execute(command, timeout):
            output = io.StringIO()
            with patch("sys.argv", ["-c", command[-1]]), patch.object(KaggleApi, "authenticate"), patch.object(KaggleApi, "build_kaggle_client", return_value=Client()), contextlib.redirect_stdout(output):
                exec(command[2], {})
            return output.getvalue()
        with patch.object(module, "_discover_runs", return_value=[dict(version=3, status="RUNNING")]), patch.object(module, "_query_process", side_effect=execute):
            result = await module.cancel_run("owner/notebook", dry_run=dry_run)
        return json.loads(result), sent

    async def test_cancel_uses_associated_session_not_version(self):
        result, sent = await self.exercise_cancel()
        self.assertEqual(sent, [987654])
        self.assertTrue(result["cancellation_sent"])

    async def test_dry_run_does_not_cancel(self):
        result, sent = await self.exercise_cancel(dry_run=True)
        self.assertEqual(sent, [])
        self.assertEqual(result["session_id"], 987654)

    async def test_finished_during_discovery_is_not_cancelled(self):
        result, sent = await self.exercise_cancel(status="COMPLETE")
        self.assertEqual(sent, [])
        self.assertFalse(result["cancellation_sent"])

    async def test_untrusted_redirect_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Cannot verify session identity"):
            await self.exercise_cancel(location="https://other.example/v1/kernels/output/download_zip/987654")

    async def test_backend_rejection_is_not_reported_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            await self.exercise_cancel(backend_error="permission denied")

    async def test_log_timeout_preserves_observed_status(self):
        baseline = dict(version=3, status="RUNNING", logs=[], logs_error=None)
        with patch.object(module, "_query_process", side_effect=[json.dumps(baseline), asyncio.TimeoutError()]):
            result = await module._run_snapshot("owner/notebook", logs=True, log_timeout=1)
        self.assertEqual(result["version"], 3)
        self.assertEqual(result["status"], "RUNNING")
        self.assertIn("timed out", result["logs_error"])

    async def test_request_timeout_does_not_report_wait_deadline(self):
        with patch.object(module, "_run_snapshot", side_effect=TimeoutError()):
            with self.assertRaisesRegex(RuntimeError, "deadline was not reached"):
                await module.wait("owner/notebook", version=3, timeout=21600)

    async def test_wait_deadline_preserves_last_observed_running_status(self):
        baseline = dict(version=3, status="RUNNING", logs=[], logs_error=None)
        with patch.object(module, "_run_snapshot", return_value=baseline):
            report = await module.wait("owner/notebook", version=3, timeout=1)
        self.assertIn("RUNNING", report)
        self.assertIn("· timeout", report)
        self.assertIn("requested condition was not observed", report)

    def test_run_kaggle_formats_cli_stderr_on_error(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = module.subprocess.CalledProcessError(
                1, ["kaggle", "cmd"], output="", stderr="Kaggle API Error: forbidden\n"
            )
            with self.assertRaisesRegex(RuntimeError, "Kaggle API Error: forbidden"):
                module._run_kaggle(["cmd"])

    def test_model_transfer_sanitizes_metadata(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            meta_file = tmp_path / "model-instance-metadata.json"
            meta_file.write_text(json.dumps({
                "trainingData": [{"datasetSlug": "owner/slug"}, "owner/slug2"],
                "modelInstanceType": "FineTuned"
            }))
            with patch.object(module, "_run_kaggle") as mock_run:
                mock_proc = SimpleNamespace(stdout="uploaded", stderr="")
                mock_run.return_value = mock_proc
                res = module._model_transfer("push-model", str(tmp_path), {})
                self.assertEqual(res, "uploaded")
                saved_meta = json.loads(meta_file.read_text())
                self.assertEqual(saved_meta["trainingData"], ["owner/slug", "owner/slug2"])
                self.assertEqual(saved_meta["modelInstanceType"], "Unspecified")


if __name__ == "__main__":
    unittest.main()
