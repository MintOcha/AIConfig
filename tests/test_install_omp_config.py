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
        self,
        home: Path,
        input_text: str,
        *arguments: str,
        config_dir: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        fake_bin = home / "bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        fake_omp = fake_bin / "omp"
        fake_omp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_omp.chmod(0o755)
        environment["PATH"] = f"{fake_bin}:{environment.get('PATH', '')}"
        if config_dir is not None:
            environment["AI_CONFIG_OMP_DIR"] = str(config_dir)
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

            result = self.run_installer(home, "4\n")

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

            result = self.run_installer(home, "4\n", "--dry-run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Preview: would install OMP setting config.yml", result.stdout)
            self.assertIn("Preview: would refresh OMP models catalog", result.stdout)
            self.assertFalse(target.exists())


    def test_omp_menu_copies_existing_configs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config_dir = root / "tracked"
            target = home / ".omp" / "agent"
            target.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            (target / "config.yml").write_text("steeringMode: all\n", encoding="utf-8")
            (target / "keybindings.yml").write_text(
                "app.model.select: Ctrl+Alt+M\n", encoding="utf-8"
            )
            (target / "keybindings.json").write_text(
                '{"app.model.select": "Ctrl+Alt+M"}', encoding="utf-8"
            )

            result = self.run_installer(home, "3\n4\n", config_dir=config_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Copied existing OMP configs", result.stdout)
            self.assertEqual(
                (config_dir / "config.yml").read_text(encoding="utf-8"),
                "steeringMode: all\n",
            )
            self.assertEqual(
                (config_dir / "keybindings.yml").read_text(encoding="utf-8"),
                "app.model.select: Ctrl+Alt+M\n",
            )
            self.assertEqual(
                (config_dir / "keybindings.json").read_text(encoding="utf-8"),
                '{"app.model.select": "Ctrl+Alt+M"}',
            )

    def test_omp_menu_apply_confirmation_overwrites_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config_dir = root / "tracked"
            target = home / ".omp" / "agent"
            target.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            (config_dir / "config.yml").write_text("steeringMode: all\n", encoding="utf-8")
            (target / "config.yml").write_text("steeringMode: one-at-a-time\n", encoding="utf-8")

            # Confirm with 'y'
            result = self.run_installer(home, "2\ny\n4\n", config_dir=config_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Overwrite existing OMP files", result.stdout)
            self.assertEqual(
                (target / "config.yml").read_text(encoding="utf-8"),
                "steeringMode: all\n",
            )

    def test_omp_menu_apply_cancellation_preserves_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config_dir = root / "tracked"
            target = home / ".omp" / "agent"
            target.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            (config_dir / "config.yml").write_text("steeringMode: all\n", encoding="utf-8")
            (target / "config.yml").write_text("steeringMode: one-at-a-time\n", encoding="utf-8")

            # Decline with 'n'
            result = self.run_installer(home, "2\nn\n4\n", config_dir=config_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Cancelled applying OMP settings", result.stdout)
            self.assertEqual(
                (target / "config.yml").read_text(encoding="utf-8"),
                "steeringMode: one-at-a-time\n",
            )

    def test_omp_menu_copy_cancellation_preserves_tracked_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config_dir = root / "tracked"
            target = home / ".omp" / "agent"
            target.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            (config_dir / "config.yml").write_text("steeringMode: original\n", encoding="utf-8")
            (target / "config.yml").write_text("steeringMode: modified\n", encoding="utf-8")

            # Decline with 'n'
            result = self.run_installer(home, "3\nn\n4\n", config_dir=config_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Cancelled copying OMP settings", result.stdout)
            self.assertEqual(
                (config_dir / "config.yml").read_text(encoding="utf-8"),
                "steeringMode: original\n",
            )

if __name__ == "__main__":
    unittest.main()
