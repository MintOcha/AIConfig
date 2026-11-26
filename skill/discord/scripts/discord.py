#!/usr/bin/env python3
"""
Discord minimal webhook client.
Sends messages and optional file attachments to a Discord webhook.
"""

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DISCORD_MESSAGE_LIMIT = 2000


def get_webhook_url():
    entry = "aiconfig/discord/webhook-url"
    try:
        result = subprocess.run(["pass", "show", entry], check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise ValueError(f"Could not read pass entry {entry}.") from None
    return result.stdout.partition("\n")[0].strip()


def _next_code_fence(text, open_fence):
    fence = open_fence
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            fence = stripped[3:].strip() if fence is None else None
    return fence


def split_message(message, limit=DISCORD_MESSAGE_LIMIT):
    if len(message) <= limit:
        return [message]

    chunks = []
    remaining = message
    open_fence = None

    while remaining:
        prefix = f"```{open_fence}\n" if open_fence is not None else ""
        if len(prefix) >= limit - 8:
            prefix = ""
            open_fence = None

        available = limit - len(prefix)
        if len(remaining) <= available:
            tail = (prefix + remaining).rstrip()
            if tail:
                chunks.append(tail)
            break

        needs_fence_reserve = open_fence is not None or "```" in remaining[:available]
        max_body = available - 4 if needs_fence_reserve else available

        cut = -1
        delim_len = 0
        min_preferred = max_body // 2
        for start in (min_preferred, 1):
            for delim in ("\n\n", "\n", " "):
                pos = remaining.rfind(delim, start, max_body + len(delim))
                if pos > 0:
                    cut = pos
                    delim_len = len(delim)
                    break
            if cut > 0:
                break
        if cut <= 0:
            cut = max_body

        piece = remaining[:cut]
        rest = remaining[cut + delim_len :]

        next_fence = _next_code_fence(piece, open_fence)
        if next_fence is not None:
            chunk = prefix + piece.rstrip("\n") + "\n```"
        else:
            chunk = (prefix + piece).rstrip()

        if chunk:
            chunks.append(chunk)

        open_fence = next_fence
        remaining = rest if open_fence is not None else rest.lstrip("\n")

    return chunks


def _post_webhook(webhook_url, message=None, file_path=None, username=None):
    payload = {}
    if message:
        payload["content"] = message
    if username:
        payload["username"] = username

    if file_path:
        filename = os.path.basename(file_path)
        mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as f:
            file_bytes = f.read()

        boundary = "----DiscordWebhookBoundary" + os.urandom(16).hex()
        body = bytearray()
        if payload:
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                b'Content-Disposition: form-data; name="payload_json"\r\n'
                b"Content-Type: application/json\r\n\r\n"
            )
            body.extend(json.dumps(payload).encode("utf-8"))
            body.extend(b"\r\n")
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'.encode(
                "utf-8"
            )
        )
        body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
        body.extend(file_bytes)
        body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "DiscordBot (https://discord.com, 1.0)",
        }
        data = bytes(body)
    else:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://discord.com, 1.0)",
        }
        data = json.dumps(payload).encode("utf-8")

    for attempt in range(2):
        req = urllib.request.Request(webhook_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                result = json.loads(raw) if raw else {"status": resp.status}
                if resp.headers.get("X-RateLimit-Remaining") == "0":
                    reset_after = float(resp.headers.get("X-RateLimit-Reset-After", "0") or 0)
                    if reset_after > 0:
                        time.sleep(reset_after)
                return result
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="ignore")
            if e.code == 429 and attempt == 0:
                try:
                    retry_after = float(json.loads(err_msg).get("retry_after", 1))
                except (ValueError, TypeError):
                    retry_after = 1.0
                time.sleep(retry_after)
                continue
            raise RuntimeError(f"HTTP Error {e.code}: {err_msg}")


def send_webhook(message=None, file_path=None, username=None, wait=False):
    if not message and not file_path:
        raise ValueError("Provide a message or --file to send.")
    if file_path and not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    webhook_url = get_webhook_url()
    if not webhook_url or "YOUR_WEBHOOK" in webhook_url:
        raise ValueError("webhook_url missing in discord configuration.")

    if wait:
        parsed = urllib.parse.urlsplit(webhook_url)
        q = urllib.parse.parse_qs(parsed.query)
        q["wait"] = ["true"]
        webhook_url = urllib.parse.urlunsplit(
            parsed._replace(query=urllib.parse.urlencode(q, doseq=True))
        )

    chunks = split_message(message) if message else [None]
    results = []
    for idx, chunk in enumerate(chunks):
        chunk_file = file_path if idx == len(chunks) - 1 else None
        results.append(_post_webhook(webhook_url, chunk, chunk_file, username))

    return results[0] if len(results) == 1 else results


def main():
    parser = argparse.ArgumentParser(description="Send message or file to Discord webhook")
    parser.add_argument("message", nargs="*", help="Message content to send")
    parser.add_argument("-f", "--file", help="Optional file attachment path")
    parser.add_argument("-u", "--username", help="Override webhook username")
    parser.add_argument("--json", action="store_true", help="Output raw JSON response")
    args = parser.parse_args()

    msg = " ".join(args.message).strip()
    if not msg and not sys.stdin.isatty() and not args.file:
        msg = sys.stdin.read().strip()

    try:
        res = send_webhook(msg or None, args.file, args.username, wait=args.json)
        if args.json:
            print(json.dumps(res, indent=2))
        elif isinstance(res, list):
            print(f"Sent {len(res)} messages to Discord webhook.")
        else:
            print("Sent to Discord webhook.")
    except Exception as e:
        print(f"Webhook failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
