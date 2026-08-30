import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sync-omp-config.py"


class SyncOmpConfigTests(unittest.TestCase):
    def run_sync(self, source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--source",
                str(source),
                "--destination",
                str(destination),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_copies_only_allowlisted_settings_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "agent"
            destination = root / "tracked"
            source.mkdir()
            (source / "config.yml").write_text("steeringMode: all\n", encoding="utf-8")
            (source / "keybindings.yml").write_text(
                "app.model.select: Ctrl+Alt+M\n", encoding="utf-8"
            )
            (source / "keybindings.json").write_text(
                json.dumps({"app.model.select": "Ctrl+Alt+M"}), encoding="utf-8"
            )
            (source / "models.yml").write_text("apiKey: private\n", encoding="utf-8")
            (source / "history.db").write_bytes(b"private history")

            result = self.run_sync(source, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                sorted(path.name for path in destination.iterdir()),
                ["config.yml", "keybindings.json", "keybindings.yml"],
            )
            self.assertEqual(
                (destination / "config.yml").read_text(encoding="utf-8"),
                "steeringMode: all\n",
            )

    def test_rejects_sensitive_keys_before_writing_any_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "agent"
            destination = root / "tracked"
            source.mkdir()
            destination.mkdir()
            (destination / "config.yml").write_text("old: value\n", encoding="utf-8")
            (source / "config.yml").write_text(
                "steeringMode: all\napiKey: private\n", encoding="utf-8"
            )

            result = self.run_sync(source, destination)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("sensitive key", result.stderr)
            self.assertEqual(
                (destination / "config.yml").read_text(encoding="utf-8"),
                "old: value\n",
            )

    def test_removes_stale_allowlisted_file_missing_from_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "agent"
            destination = root / "tracked"
            source.mkdir()
            destination.mkdir()
            (source / "config.yml").write_text("steeringMode: all\n", encoding="utf-8")
            (destination / "config.yml").write_text("old: value\n", encoding="utf-8")
            (destination / "keybindings.yml").write_text("old: binding\n", encoding="utf-8")

            result = self.run_sync(source, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((destination / "keybindings.yml").exists())


if __name__ == "__main__":
    unittest.main()
