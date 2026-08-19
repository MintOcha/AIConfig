#!/usr/bin/env python3
"""Refresh CLIProxyAPI configuration and restart both Docker Compose stacks."""

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen

import yaml


MODELS_URL = "https://opencode.ai/zen/go/v1/models"
OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"


def fetch_models() -> list[str]:
    request = Request(
        MODELS_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "CLIProxyAPI-model-updater/1.0",
        },
    )
    with urlopen(request) as response:
        payload = yaml.safe_load(response.read())

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("OpenCode returned an unexpected model response")

    models = sorted(
        {
            item["id"].strip()
            for item in payload["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
        }
    )
    if not models:
        raise ValueError("OpenCode returned no model IDs")
    return models


def normalize_url(value: object) -> str:
    return str(value).strip().rstrip("/")


def update_config(config_path: Path, model_ids: list[str]) -> int:
    with config_path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)

    if not isinstance(config, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping")

    providers = config.get("openai-compatibility")
    if not isinstance(providers, list):
        return 0

    updates = 0
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if normalize_url(provider.get("base-url")) != OPENCODE_BASE_URL:
            continue
        api_key_entries = provider.get("api-key-entries")
        api_configured = isinstance(api_key_entries, list) and any(
            isinstance(entry, dict) and str(entry.get("api-key", "")).strip()
            for entry in api_key_entries
        )
        expected_models = [{"name": model_id} for model_id in model_ids] if api_configured else []
        if provider.get("models") != expected_models:
            provider["models"] = expected_models
            updates += 1

    if updates == 0:
        return 0

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=config_path.parent, delete=False
    ) as temp_file:
        temp_path = Path(temp_file.name)
        yaml.safe_dump(config, temp_file, allow_unicode=True, default_flow_style=False, sort_keys=False)

    temp_path.chmod(config_path.stat().st_mode)
    temp_path.replace(config_path)
    return updates


def run_recent_errors(project_dir: Path) -> None:
    candidates = [
        project_dir / "view-failure-logs.sh",
        Path(__file__).resolve().parent / "view-failure-logs.sh",
    ]
    script_path = next((p for p in candidates if p.is_file()), None)
    if not script_path:
        return

    print("=== Top 2 recent errors ===", flush=True)
    subprocess.run([str(script_path), "60", "2"], cwd=project_dir, check=True)
    print(flush=True)


def compose_command(project_dir: Path, compose_file: Path | None, *args: str) -> None:
    command = ["docker", "compose"]
    if compose_file is not None:
        command.extend(["-f", str(compose_file)])
    command.extend(args)
    subprocess.run(command, cwd=project_dir, check=True)


def deployed_version(
    project_dir: Path,
    compose_file: Path | None,
    service: str,
) -> str:
    command = ["docker", "compose"]
    if compose_file is not None:
        command.extend(["-f", str(compose_file)])
    command.extend(["ps", "-q", service])
    container = subprocess.run(
        command,
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not container:
        return "not running"

    result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{index .Config.Labels \"org.opencontainers.image.version\"}}|{{.Image}}",
            container,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    label, _, image_id = result.partition("|")
    if label and label != "<no value>":
        return label
    return image_id.removeprefix("sha256:")[:12] or "unknown"


def restart_stacks(
    project_dir: Path,
    management_dir: Path,
    management_compose_file: Path,
) -> None:
    print("=== Restarting Docker Compose stacks ===", flush=True)
    compose_command(project_dir, None, "down")
    compose_command(management_dir, management_compose_file, "down")
    compose_command(
        management_dir,
        management_compose_file,
        "up",
        "-d",
        "postgres",
        "docker-proxy",
        "cliproxyapi",
    )
    compose_command(project_dir, None, "up", "-d")
    compose_command(management_dir, management_compose_file, "up", "-d")


def print_version_result(name: str, before: str, after: str) -> None:
    if before == after:
        print(f"{name}: no version bump ({after}).")
    else:
        print(f"{name}: bumped from {before} to {after}.")


def resolve_default_project_dir() -> Path:
    candidates = [
        Path.cwd(),
        Path("/home/nas/Projects/CLIProxyAPI"),
        Path(__file__).resolve().parent,
    ]
    for c in candidates:
        if (c / "docker-compose.yml").is_file():
            return c
    return candidates[0]


def main() -> int:
    project_dir = resolve_default_project_dir()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=project_dir, help="CLIProxyAPI project directory")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument(
        "--management-dir",
        type=Path,
        default=project_dir.parent / "Dashboard_CPA",
        help="Path to the CLIProxyAPI management dashboard repository",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Update config without restarting Docker Compose",
    )
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    config_path = (args.config or (project_dir / "config.yaml")).resolve()
    management_dir = args.management_dir.resolve()
    management_compose_file = management_dir / "docker-compose.local.yml"
    if not config_path.is_file():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1
    if not management_compose_file.is_file():
        print(f"Management Compose file not found: {management_compose_file}", file=sys.stderr)
        return 1

    try:
        run_recent_errors(project_dir)
        model_ids = fetch_models()
        updated_providers = update_config(config_path, model_ids)
        if updated_providers == 0:
            print("No OpenCode provider configuration changed.")
        else:
            print(f"Synchronized models for {updated_providers} OpenCode provider(s).")

        if not args.no_restart:
            project_before = deployed_version(project_dir, None, "cli-proxy-api")
            management_before = deployed_version(
                management_dir, management_compose_file, "dashboard"
            )
            restart_stacks(project_dir, management_dir, management_compose_file)
            project_after = deployed_version(project_dir, None, "cli-proxy-api")
            management_after = deployed_version(
                management_dir, management_compose_file, "dashboard"
            )
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return 1

    if args.no_restart:
        print("Successfully updated the configuration.")
    else:
        print()
        print_version_result("CLIProxyAPI", project_before, project_after)
        print_version_result("CLIProxyAPI management", management_before, management_after)
        print("Successfully updated and restarted both services.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
