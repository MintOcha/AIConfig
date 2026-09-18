#!/usr/bin/env python3
"""Refresh CLIProxyAPI configuration, keep Postgres alive, and update CLIProxyAPI and Dashboard stacks."""

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

import yaml


MODELS_URL = "https://opencode.ai/zen/go/v1/models"
OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"

CLI_PROXY_CONTAINER = "cli-proxy-api"
POSTGRES_CONTAINER = "cliproxyapi-postgres"
DASHBOARD_CONTAINER = "cliproxyapi-dashboard"


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
        project_dir / "scripts" / "view-failure-logs.sh",
        project_dir / "view-failure-logs.sh",
        Path(__file__).resolve().parent / "view-failure-logs.sh",
    ]
    script_path = next((p for p in candidates if p.is_file()), None)
    if not script_path:
        return

    print("=== Top 2 recent errors ===", flush=True)
    try:
        subprocess.run([str(script_path), "60", "2"], cwd=project_dir, check=True)
    except Exception as exc:
        print(f"Failed to fetch recent errors: {exc}")
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
        fallback = subprocess.run(
            ["docker", "ps", "-q", "-f", f"name={service}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if fallback:
            container = fallback.split()[0]
        else:
            return "not running"
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.version"}}|{{.Image}}',
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


def running_container_names() -> set[str]:
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.split())


def wait_until_healthy(container: str, timeout_seconds: int = 60) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip() == "healthy":
            return True
        time.sleep(3)
    return False


def ensure_postgres_running() -> None:
    if POSTGRES_CONTAINER in running_container_names():
        return
    print(f"{POSTGRES_CONTAINER} is stopped; starting it...", flush=True)
    subprocess.run(["docker", "start", POSTGRES_CONTAINER], check=True)
    if not wait_until_healthy(POSTGRES_CONTAINER):
        raise RuntimeError(f"{POSTGRES_CONTAINER} did not become healthy in time")
    print(f"{POSTGRES_CONTAINER} is healthy.", flush=True)


def discover_dashboard_dir(project_dir: Path) -> Path | None:
    candidates = [
        project_dir.parent / "Dashboard_CPA",
        Path("/home/nas/Projects/Dashboard_CPA"),
        Path.cwd(),
    ]
    for c in candidates:
        if (c / "compose.yaml").is_file() or (c / "docker-compose.yml").is_file():
            return c
    return None


def resolve_compose_file(directory: Path) -> Path | None:
    for name in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
        p = directory / name
        if p.is_file():
            return p
    return None


def update_dashboard(dashboard_dir: Path) -> tuple[str, str]:
    compose_file = resolve_compose_file(dashboard_dir)
    before_version = deployed_version(dashboard_dir, compose_file, "dashboard")
    print("=== Updating Dashboard ===", flush=True)
    compose_command(dashboard_dir, compose_file, "pull", "dashboard")
    compose_command(dashboard_dir, compose_file, "up", "-d", "dashboard")
    after_version = deployed_version(dashboard_dir, compose_file, "dashboard")
    return before_version, after_version


def restart_stacks(project_dir: Path) -> None:
    print("=== Restarting CLIProxyAPI ===", flush=True)
    compose_command(project_dir, None, "up", "-d")


def verify_containers_running(include_dashboard: bool = True) -> None:
    running = running_container_names()
    required = [CLI_PROXY_CONTAINER, POSTGRES_CONTAINER]
    if include_dashboard:
        required.append(DASHBOARD_CONTAINER)
    missing = [name for name in required if name not in running]
    if missing:
        raise RuntimeError(f"container(s) not running after update: {', '.join(missing)}")
    print(f"Verified running: {', '.join(required)}.", flush=True)


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
    parser.add_argument("--dashboard-dir", type=Path, default=None, help="Dashboard_CPA project directory")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Update config without restarting Docker Compose",
    )
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    config_path = (args.config or (project_dir / "config.yaml")).resolve()
    dashboard_dir = (args.dashboard_dir.resolve() if args.dashboard_dir else discover_dashboard_dir(project_dir))

    if not config_path.is_file():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1

    dashboard_before = "unknown"
    dashboard_after = "unknown"

    try:
        run_recent_errors(project_dir)
        model_ids = fetch_models()
        updated_providers = update_config(config_path, model_ids)
        if updated_providers == 0:
            print("No OpenCode provider configuration changed.")
        else:
            print(f"Synchronized models for {updated_providers} OpenCode provider(s).")

        ensure_postgres_running()

        if not args.no_restart:
            project_before = deployed_version(project_dir, None, "cli-proxy-api")
            restart_stacks(project_dir)
            project_after = deployed_version(project_dir, None, "cli-proxy-api")

            if dashboard_dir:
                dashboard_before, dashboard_after = update_dashboard(dashboard_dir)
            else:
                print("Dashboard directory not found; skipping Dashboard update.", flush=True)

        verify_containers_running(include_dashboard=dashboard_dir is not None and not args.no_restart)
    except (OSError, subprocess.CalledProcessError, ValueError, RuntimeError) as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return 1

    if args.no_restart:
        print("Successfully updated the configuration.")
    else:
        print()
        print_version_result("CLIProxyAPI", project_before, project_after)
        if dashboard_dir:
            print_version_result("Dashboard", dashboard_before, dashboard_after)
        print("Successfully updated and restarted CLIProxyAPI and Dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
