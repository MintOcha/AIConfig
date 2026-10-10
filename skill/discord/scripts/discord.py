#!/usr/bin/env python3
"""
Discord minimal webhook client.
Sends messages and optional file attachments to a Discord webhook.
"""

import argparse
import json
import mimetypes
import os
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request


def get_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    config_paths = [
        os.path.join(parent_dir, "config.toml"),
        os.path.join(script_dir, "config.toml"),
        os.path.expanduser("~/.config/discord/config.toml"),
    ]
    for p in config_paths:
        if os.path.exists(p):
            with open(p, "rb") as f:
                data = tomllib.load(f)
                return data.get("discord", data)
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if webhook_url:
        return {"webhook_url": webhook_url}
    raise FileNotFoundError(
        f"config.toml not found in: {config_paths} and DISCORD_WEBHOOK_URL not set"
    )


def send_webhook(message=None, file_path=None, username=None, wait=False):
    if not message and not file_path:
        raise ValueError("Provide a message or --file to send.")

    config = get_config()
    webhook_url = config.get("webhook_url") or config.get("url")
    if not webhook_url or "YOUR_WEBHOOK" in webhook_url:
        raise ValueError("webhook_url missing in discord configuration.")

    if wait:
        parsed = urllib.parse.urlsplit(webhook_url)
        q = urllib.parse.parse_qs(parsed.query)
        q["wait"] = ["true"]
        webhook_url = urllib.parse.urlunsplit(
            parsed._replace(query=urllib.parse.urlencode(q, doseq=True))
        )

    payload = {}
    if message:
        payload["content"] = message
    if username or config.get("username"):
        payload["username"] = username or config.get("username")

    if file_path:
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
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

    req = urllib.request.Request(webhook_url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {"status": resp.status}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP Error {e.code}: {err_msg}")


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
        else:
            print("Sent to Discord webhook.")
    except Exception as e:
        print(f"Webhook failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
