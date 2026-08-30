from pathlib import Path
import os
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install.sh"
TRACKED_CONFIG = REPO_ROOT / "config" / "omp"


class InstallOmpConfigTests(unittest.TestCase):
    def run_installer(
        self, home: Path, input_text: str, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        return subprocess.run(
            [str(INSTALLER), "--omp", *arguments],
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_omp_startup_installs_tracked_settings_without_overwriting_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            target = home / ".omp" / "agent"
            target.mkdir(parents=True)
            (target / "mcp.json").write_text('{"existing": true}\n', encoding="utf-8")

            result = self.run_installer(home, "3\n")

            self.assertEqual(result.returncode, 0, result.stderr)
            for source in TRACKED_CONFIG.iterdir():
                self.assertEqual(
                    (target / source.name).read_bytes(),
                    source.read_bytes(),
                )
            self.assertEqual(
                (target / "mcp.json").read_text(encoding="utf-8"),
                '{"existing": true}\n',
            )

    def test_dry_run_reports_settings_without_creating_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            target = home / ".omp" / "agent"

            result = self.run_installer(home, "3\n", "--dry-run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Preview: would install OMP setting config.yml", result.stdout)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
