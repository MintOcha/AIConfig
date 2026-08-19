---
name: cliproxyapi-manage
description: Manage CLIProxyAPI instances, check Codex account quotas & 7-day usage, redeem banked rate-limit resets, inspect failure logs from Postgres/Docker, synchronize OpenCode/OpenAI-compatible models, and restart proxy server stacks. Use when inspecting proxy health, updating models, investigating request errors, or managing Codex account usage and resets.
---

# CLIProxyAPI Management Skill

This skill provides utilities to manage, monitor, diagnose, and configure the CLIProxyAPI proxy server and associated provider accounts.

## Utilities Overview

The scripts are located in `scripts/` under this skill directory:

1. **`codex_usage_reset.py`** — Inspect Codex accounts, 7-day rolling usage percentages, next reset countdowns, and interactively view or redeem banked rate-limit resets.
2. **`view-failure-logs.sh`** — Query Postgres `usage_records` and Docker logs to diagnose recent request failures, token usage, latency, and status codes.
3. **`update-server.py`** — Fetch latest OpenCode models, synchronize `config.yaml`, display recent errors, and restart both CLIProxyAPI and Dashboard compose stacks.
4. **`changemodels.py`** — Interactively add, sync, or remove custom OpenAI-compatible model providers in `config.yaml`.

---

## 1. Codex Usage & Banked Reset Management

Inspect usage for all Codex accounts configured in auth directories (or Docker container) and optionally consume banked rate-limit resets.

```bash
# Default (scans ./auth, ./auths, /root/.cli-proxy-api, or ~/.cli-proxy-api):
python3 scripts/codex_usage_reset.py

# Explicit auth directory or single auth file:
python3 scripts/codex_usage_reset.py /path/to/auths
# or:
python3 scripts/codex_usage_reset.py --auth-dir /path/to/auths
```

> **Note for Agent execution:** If you are running the script outside the proxy directory or against a specific directory other than `./auth` or `./auths`, always supply the path to the auth directory explicitly as an argument.

### Features:
- **7-Day Rolling Usage**: Shows progress bar and percentage used for each account's primary quota window.
- **Reset Countdown**: Shows exact time remaining until next natural quota reset.
- **Banked Resets**: Displays number of banked resets available, total earned, and expiration timestamps.
- **Interactive Actions**:
  - `[1]` Redeem / Trigger a usage reset on an account.
  - `[2]` Exit.
  - `[r]` Refresh live usage stats across all accounts.
- **OAuth Token Auto-Refresh**: Seamlessly handles token refreshes against OpenAI OAuth endpoints and updates auth files on disk.

---

## 2. Diagnosing Recent Failures

Inspect failed request logs from Postgres database and Docker container logs.

```bash
# View failures in the last 60 minutes (default 50 records):
scripts/view-failure-logs.sh

# Custom timeframe and record limit (e.g. last 120 minutes, max 10 records):
scripts/view-failure-logs.sh 120 10
```

### Environment Variables:
- `PROXY_CONTAINER`: Docker container name for proxy (default: `cli-proxy-api`).
- `POSTGRES_CONTAINER`: Docker container name for Postgres (default: `cliproxyapi-postgres`).
- `DISPLAY_TIMEZONE`: Display timezone (default: `Asia/Shanghai`).

---

## 3. Updating Models & Server Stacks

Synchronize model definitions from OpenCode and restart Docker Compose stacks.

```bash
# Synchronize models and restart stacks:
python3 scripts/update-server.py

# Update config only without restarting Docker containers:
python3 scripts/update-server.py --no-restart

# Custom project or management dashboard directory:
python3 scripts/update-server.py --project-dir /home/nas/Projects/CLIProxyAPI --management-dir /home/nas/Projects/Dashboard_CPA
```

---

## 4. Custom Model Provider Configuration

Add, sync, or remove custom OpenAI-compatible endpoints.

```bash
python3 scripts/changemodels.py
# or with explicit config:
python3 scripts/changemodels.py --config /path/to/config.yaml
```
