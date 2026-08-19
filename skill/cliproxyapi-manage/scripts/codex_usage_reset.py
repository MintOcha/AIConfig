#!/usr/bin/env python3
"""
Codex Usage & Reset Manager for CLIProxyAPI
===========================================
Displays 7-day usage metrics for all Codex accounts in the auth directory
and allows triggering / redeeming banked usage resets.

Zero external dependencies - runs on standard Python 3.8+.
Works directly inside the CLIProxyAPI Docker container or on the host machine.
"""

import os
import sys
import json
import time
import uuid
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

# OpenAI OAuth & Codex endpoints
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
CODEX_CONSUME_RESET_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"

USER_AGENT = "codex-tui/0.135.0 (Mac OS 26.5.0; arm64) iTerm.app/3.6.10 (codex-tui; 0.135.0)"
ORIGINATOR = "codex-tui"

# ANSI Color codes for clean terminal output
class Colors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    @classmethod
    def disable(cls):
        cls.HEADER = ""
        cls.BLUE = ""
        cls.CYAN = ""
        cls.GREEN = ""
        cls.YELLOW = ""
        cls.RED = ""
        cls.BOLD = ""
        cls.DIM = ""
        cls.RESET = ""

if not sys.stdout.isatty():
    Colors.disable()


def find_auth_directories() -> List[Path]:
    """Find plausible auth directory locations without hardcoded machine paths."""
    candidates = []
    
    # 1. Custom environment variables
    if "AUTH_DIR" in os.environ:
        candidates.append(Path(os.environ["AUTH_DIR"]))
    if "CLI_PROXY_AUTH_PATH" in os.environ:
        candidates.append(Path(os.environ["CLI_PROXY_AUTH_PATH"]))
        
    # 2. Docker default mount paths
    candidates.append(Path("/root/.cli-proxy-api"))
    candidates.append(Path("/CLIProxyAPI/auth"))
    candidates.append(Path("/CLIProxyAPI/auths"))
    
    # 3. Relative paths from current working directory and script directory
    cwd = Path.cwd()
    candidates.append(cwd / "auth")
    candidates.append(cwd / "auths")
    
    script_dir = Path(__file__).resolve().parent
    candidates.append(script_dir / "auth")
    candidates.append(script_dir / "auths")
    candidates.append(Path.home() / ".cli-proxy-api")
    
    # Remove duplicates while preserving order
    valid_dirs = []
    seen = set()
    for p in candidates:
        try:
            resolved = p.resolve()
            if resolved not in seen and resolved.is_dir():
                seen.add(resolved)
                valid_dirs.append(resolved)
        except Exception:
            pass
            
    return valid_dirs


class CodexAccount:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.raw_data: Dict[str, Any] = {}
        self.email: str = ""
        self.account_id: str = ""
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.plan_type: str = ""
        
        # Usage stats
        self.usage_data: Optional[Dict[str, Any]] = None
        self.reset_data: Optional[Dict[str, Any]] = None
        self.fetch_error: Optional[str] = None
        
        self.load_file()

    def load_file(self) -> bool:
        """Load and parse the auth JSON file."""
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                self.raw_data = json.load(f)
            self.access_token = self.raw_data.get("access_token", "")
            self.refresh_token = self.raw_data.get("refresh_token", "")
            self.account_id = self.raw_data.get("account_id", "")
            self.email = self.raw_data.get("email", self.file_path.stem)
            return True
        except Exception as e:
            self.fetch_error = f"Failed to load file: {e}"
            return False

    def save_file(self) -> bool:
        """Save updated tokens back to JSON file."""
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.raw_data, f, indent=2)
            return True
        except PermissionError:
            # File is read-only / owned by another user (e.g. root in docker)
            return False
        except Exception as e:
            print(f"{Colors.YELLOW}[!] Note: Could not write updated token to {self.file_path.name}: {e}{Colors.RESET}")
            return False

    def refresh_access_token(self) -> bool:
        """Refresh OAuth2 access token using refresh_token and save to disk."""
        if not self.refresh_token:
            return False

        data = urllib.parse.urlencode({
            "client_id": OPENAI_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "scope": "openid profile email"
        }).encode("utf-8")

        req = urllib.request.Request(
            OPENAI_TOKEN_URL,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    resp_body = json.loads(resp.read().decode("utf-8"))
                    new_access = resp_body.get("access_token")
                    new_refresh = resp_body.get("refresh_token")
                    new_id = resp_body.get("id_token")

                    if new_access:
                        self.access_token = new_access
                        self.raw_data["access_token"] = new_access
                    if new_refresh:
                        self.refresh_token = new_refresh
                        self.raw_data["refresh_token"] = new_refresh
                    if new_id:
                        self.raw_data["id_token"] = new_id

                    self.raw_data["last_refresh"] = datetime.now(timezone.utc).isoformat()
                    self.save_file()
                    return True
        except Exception as e:
            self.fetch_error = f"Token refresh failed: {e}"
        return False

    def _api_request(self, url: str, method: str = "GET", payload: Optional[dict] = None) -> Tuple[int, Any]:
        """Perform an authenticated request to Codex backend API with auto-refresh on 401."""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": USER_AGENT,
            "Originator": ORIGINATOR,
            "Accept": "application/json",
        }
        if self.account_id:
            headers["Chatgpt-Account-Id"] = self.account_id

        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        def _do_request():
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8")
                try:
                    return resp.status, json.loads(body)
                except Exception:
                    return resp.status, body

        try:
            return _do_request()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Token expired, try refreshing and retry once
                if self.refresh_access_token():
                    headers["Authorization"] = f"Bearer {self.access_token}"
                    try:
                        return _do_request()
                    except urllib.error.HTTPError as retry_err:
                        err_text = retry_err.read().decode("utf-8", errors="ignore")
                        return retry_err.code, err_text
                    except Exception as ex:
                        return 0, str(ex)
            err_text = e.read().decode("utf-8", errors="ignore")
            try:
                return e.code, json.loads(err_text)
            except Exception:
                return e.code, err_text
        except Exception as e:
            return 0, str(e)

    def fetch_status(self):
        """Fetch both 7-day usage and available reset credits."""
        self.fetch_error = None

        # 1. Fetch usage
        status_code, resp = self._api_request(CODEX_USAGE_URL)
        if status_code == 200 and isinstance(resp, dict):
            self.usage_data = resp
            if "email" in resp:
                self.email = resp["email"]
            if "plan_type" in resp:
                self.plan_type = resp["plan_type"]
            if "account_id" in resp:
                self.account_id = resp["account_id"]
        else:
            self.fetch_error = f"Usage fetch HTTP {status_code}: {resp}"

        # 2. Fetch reset credits
        status_code, resp = self._api_request(CODEX_RESET_CREDITS_URL)
        if status_code == 200 and isinstance(resp, dict):
            self.reset_data = resp
        else:
            # Not critical if reset endpoint fails, but note it
            if not self.fetch_error:
                self.fetch_error = f"Reset credits HTTP {status_code}"

    def trigger_reset(self) -> Tuple[bool, str]:
        """Trigger a usage reset on this account."""
        req_id = str(uuid.uuid4())
        status_code, resp = self._api_request(CODEX_CONSUME_RESET_URL, method="POST", payload={"redeem_request_id": req_id})
        
        if status_code == 200 and isinstance(resp, dict):
            code = resp.get("code")
            if code == "no_credit":
                return False, "No banked reset credits available for this account."
            
            windows_reset = resp.get("windows_reset", 0)
            credit_info = resp.get("credit", {})
            return True, f"Reset successfully applied! Windows reset: {windows_reset}. (Credit: {credit_info})"
        else:
            return False, f"Reset failed (HTTP {status_code}): {resp}"


def format_duration(seconds: int) -> str:
    """Format seconds into a human-readable duration."""
    if seconds <= 0:
        return "now"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    mins = (seconds % 3600) // 60
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


def make_progress_bar(percent: float, width: int = 20) -> str:
    """Create a colored progress bar for usage percent."""
    percent = max(0.0, min(100.0, float(percent)))
    filled_len = int(round(width * percent / 100))
    empty_len = width - filled_len
    
    bar_color = Colors.GREEN
    if percent >= 90:
        bar_color = Colors.RED
    elif percent >= 70:
        bar_color = Colors.YELLOW
        
    bar = f"{bar_color}{'█' * filled_len}{Colors.DIM}{'░' * empty_len}{Colors.RESET}"
    return f"[{bar}] {bar_color}{percent:5.1f}%{Colors.RESET}"


def display_dashboard(accounts: List[CodexAccount]):
    """Print the formatted dashboard of all Codex accounts."""
    print("\n" + f"{Colors.BOLD}{Colors.CYAN}══════════════════════════════════════════════════════════════════════════════════════{Colors.RESET}")
    print(f" {Colors.BOLD}{Colors.HEADER}CODEX USAGE & RESET DASHBOARD{Colors.RESET}   {Colors.DIM}(Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}){Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}══════════════════════════════════════════════════════════════════════════════════════{Colors.RESET}")

    if not accounts:
        print(f"\n  {Colors.YELLOW}No Codex accounts found in auth directories.{Colors.RESET}\n")
        return

    for idx, acc in enumerate(accounts, 1):
        print(f"\n {Colors.BOLD}[Account #{idx}] {Colors.CYAN}{acc.email}{Colors.RESET} {Colors.DIM}({acc.file_path.name}){Colors.RESET}")
        print(f"  Plan: {Colors.BOLD}{acc.plan_type.upper() if acc.plan_type else 'UNKNOWN'}{Colors.RESET} | ID: {Colors.DIM}{acc.account_id or 'N/A'}{Colors.RESET}")

        if acc.fetch_error and not acc.usage_data:
            print(f"  {Colors.RED}Error fetching account status: {acc.fetch_error}{Colors.RESET}")
            continue

        # 7-Day / Primary Window Usage
        rate_limit = (acc.usage_data.get("rate_limit") or {}) if acc.usage_data else {}
        primary = rate_limit.get("primary_window") if rate_limit else None
        
        if primary:
            used_pct = primary.get("used_percent", 0) or 0
            limit_reached = rate_limit.get("limit_reached", False) or False
            reset_after = primary.get("reset_after_seconds", 0) or 0
            reset_at_ts = primary.get("reset_at", 0) or 0
            
            reset_str = format_duration(reset_after)
            reset_time_str = ""
            if reset_at_ts:
                try:
                    dt = datetime.fromtimestamp(reset_at_ts)
                    reset_time_str = f" ({dt.strftime('%b %d %H:%M')})"
                except Exception:
                    pass

            status_tag = f" {Colors.RED}{Colors.BOLD}[LIMIT REACHED]{Colors.RESET}" if limit_reached else ""
            print(f"  {Colors.BOLD}7-Day Usage:{Colors.RESET}  {make_progress_bar(used_pct, 24)}{status_tag}")
            print(f"  {Colors.BOLD}Next Reset:{Colors.RESET}   in {Colors.GREEN}{reset_str}{Colors.RESET}{reset_time_str}")
        else:
            print(f"  {Colors.BOLD}7-Day Usage:{Colors.RESET}  {Colors.DIM}No primary window rate limit data{Colors.RESET}")

        # Secondary Window Usage (e.g. 5-hour limit if present)
        secondary = rate_limit.get("secondary_window") if rate_limit else None
        if secondary:
            sec_pct = secondary.get("used_percent", 0) or 0
            sec_rst = format_duration(secondary.get("reset_after_seconds", 0) or 0)
            print(f"  {Colors.BOLD}5-Hour Usage:{Colors.RESET} {make_progress_bar(sec_pct, 24)} (resets in {sec_rst})")

        # Additional rate limits (e.g. Spark)
        add_limits = (acc.usage_data.get("additional_rate_limits") or []) if acc.usage_data else []
        for extra in add_limits:
            if not isinstance(extra, dict):
                continue
            lname = extra.get("limit_name", "Extra Limit")
            eprim = (extra.get("rate_limit") or {}).get("primary_window")
            if eprim and isinstance(eprim, dict):
                e_pct = eprim.get("used_percent", 0) or 0
                e_rst = format_duration(eprim.get("reset_after_seconds", 0) or 0)
                print(f"  {Colors.DIM}↳ {lname}:{Colors.RESET} {make_progress_bar(e_pct, 14)} (resets in {e_rst})")

        # Banked Resets
        resets_info = acc.reset_data or {}
        avail_count = resets_info.get("available_count", 0) or 0
        total_count = resets_info.get("total_earned_count", 0) or 0
        credits_list = resets_info.get("credits") or []

        if avail_count > 0:
            reset_badge = f"{Colors.GREEN}{Colors.BOLD}{avail_count} AVAILABLE{Colors.RESET}"
        else:
            reset_badge = f"{Colors.DIM}0 available{Colors.RESET}"

        print(f"  {Colors.BOLD}Banked Resets:{Colors.RESET} {reset_badge} {Colors.DIM}(Total Earned: {total_count}){Colors.RESET}")

        if isinstance(credits_list, list) and credits_list:
            for c in credits_list:
                if not isinstance(c, dict):
                    continue
                cid = c.get("id", "credit")
                c_status = c.get("status", "unknown")
                c_exp = c.get("expires_at")
                exp_str = ""
                if c_exp:
                    try:
                        exp_dt = datetime.fromisoformat(c_exp.replace("Z", "+00:00"))
                        exp_str = f" | Expires: {exp_dt.strftime('%Y-%m-%d %H:%M')}"
                    except Exception:
                        exp_str = f" | Expires: {c_exp}"
                print(f"    - Credit [{c_status}]: {cid}{exp_str}")

    print(f"\n{Colors.BOLD}{Colors.CYAN}══════════════════════════════════════════════════════════════════════════════════════{Colors.RESET}")


def load_all_accounts(auth_dir: Optional[str] = None) -> List[CodexAccount]:
    """Scan directories (or single file) and load all Codex accounts."""
    if auth_dir:
        p = Path(auth_dir).resolve()
        if p.is_file():
            acc = CodexAccount(p)
            acc.fetch_status()
            return [acc]
        dirs = [p]
    else:
        dirs = find_auth_directories()
    
    accounts = []
    seen_files = set()

    for d in dirs:
        if not d.is_dir():
            continue
        for file in sorted(d.glob("*.json")):
            if file.resolve() in seen_files:
                continue
            seen_files.add(file.resolve())

            # Check if this is a codex file
            is_codex = False
            if file.name.startswith("codex-"):
                is_codex = True
            else:
                try:
                    with open(file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("type") == "codex" or ("access_token" in data and "account_id" in data):
                        is_codex = True
                except Exception:
                    pass

            if is_codex:
                acc = CodexAccount(file)
                accounts.append(acc)

    # Fetch status for all loaded accounts
    for acc in accounts:
        acc.fetch_status()

    return accounts


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Codex Usage & Reset Manager")
    parser.add_argument("path", nargs="?", default=None, help="Optional path to auth directory or file")
    parser.add_argument("--auth-dir", "-d", "--auth", dest="auth_dir", type=str, default=None, help="Path to auth files directory")
    args = parser.parse_args()

    target_auth = args.auth_dir or args.path
    print(f"{Colors.BOLD}{Colors.CYAN}Searching for Codex accounts...{Colors.RESET}")
    accounts = load_all_accounts(target_auth)

    while True:
        display_dashboard(accounts)

        # Count total resets available across all accounts
        total_resets = sum((acc.reset_data or {}).get("available_count", 0) for acc in accounts)

        print(f"\n{Colors.BOLD}Actions:{Colors.RESET}")
        print(f"  {Colors.BOLD}{Colors.GREEN}[1]{Colors.RESET} Use / Trigger a Usage Reset {Colors.DIM}(Resets available: {total_resets}){Colors.RESET}")
        print(f"  {Colors.BOLD}{Colors.RED}[2]{Colors.RESET} Exit")
        print(f"  {Colors.BOLD}{Colors.CYAN}[r]{Colors.RESET} Refresh Usage Stats")

        try:
            choice = input(f"\n{Colors.BOLD}Select an option [1-2, r]: {Colors.RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if choice == "2" or choice == "exit" or choice == "q":
            print("Goodbye!")
            break
        elif choice == "r" or choice == "refresh":
            print(f"\n{Colors.CYAN}Refreshing status for all accounts...{Colors.RESET}")
            for acc in accounts:
                acc.load_file()
                acc.fetch_status()
        elif choice == "1":
            if not accounts:
                print(f"{Colors.RED}No accounts available.{Colors.RESET}")
                continue

            target_acc = None
            if len(accounts) == 1:
                target_acc = accounts[0]
            else:
                print(f"\n{Colors.BOLD}Select account to reset:{Colors.RESET}")
                for i, acc in enumerate(accounts, 1):
                    avail = (acc.reset_data or {}).get("available_count", 0)
                    print(f"  [{i}] {acc.email} ({avail} resets available)")
                
                try:
                    acc_idx_str = input(f"Account number (1-{len(accounts)}) or 'c' to cancel: ").strip()
                    if acc_idx_str.lower() == "c":
                        continue
                    acc_idx = int(acc_idx_str) - 1
                    if 0 <= acc_idx < len(accounts):
                        target_acc = accounts[acc_idx]
                    else:
                        print(f"{Colors.RED}Invalid account number.{Colors.RESET}")
                        continue
                except ValueError:
                    print(f"{Colors.RED}Invalid input.{Colors.RESET}")
                    continue

            avail = (target_acc.reset_data or {}).get("available_count", 0)
            print(f"\n{Colors.YELLOW}Triggering reset for {Colors.BOLD}{target_acc.email}{Colors.RESET} {Colors.YELLOW}(Available resets: {avail})...{Colors.RESET}")
            confirm = input(f"Are you sure you want to consume 1 banked reset? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("Reset cancelled.")
                continue

            print(f"{Colors.CYAN}Sending reset request to OpenAI Codex backend...{Colors.RESET}")
            success, msg = target_acc.trigger_reset()
            if success:
                print(f"{Colors.GREEN}{Colors.BOLD}[✓] SUCCESS: {msg}{Colors.RESET}")
            else:
                print(f"{Colors.RED}[✗] {msg}{Colors.RESET}")

            # Refresh status after action
            time.sleep(1)
            target_acc.fetch_status()
        else:
            print(f"{Colors.RED}Invalid option '{choice}'. Please choose 1, 2, or r.{Colors.RESET}")


if __name__ == "__main__":
    main()
