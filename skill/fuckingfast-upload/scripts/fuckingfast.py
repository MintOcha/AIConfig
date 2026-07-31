#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API = "https://fuckingfast.net/api"
UPLOAD = "https://w.fuckingfast.net"


def credential(args: argparse.Namespace, required: bool = True) -> str | None:
    value = args.account_id or os.environ.get("FUCKINGFAST_ACCOUNT_ID")
    if required and not value:
        raise SystemExit("Set FUCKINGFAST_ACCOUNT_ID for authenticated operations.")
    return value


def request(method: str, url: str, account_id: str | None = None, data=None, content_type=None):
    headers = {"Accept": "application/json, text/plain, */*"}
    if account_id:
        headers["Authorization"] = f"Bearer {account_id}"
    if content_type:
        headers["Content-Type"] = content_type
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {error.code}: {detail or error.reason}") from None
    except URLError as error:
        raise SystemExit(f"Request failed: {error.reason}") from None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body.strip()


def json_body(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def upload(args: argparse.Namespace):
    path = Path(args.path).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")
    name = args.name or path.name
    if len(name) > 500:
        raise SystemExit("Remote file name exceeds 500 characters.")
    account_id = credential(args, required=bool(args.parent_id))
    prefix = f"/{quote(args.parent_id, safe='')}" if args.parent_id else ""
    query = {}
    if args.location_id:
        query["locationId"] = args.location_id
    if args.note is not None:
        query["note"] = base64.b64encode(args.note.encode()).decode()
    url = f"{UPLOAD}{prefix}/{quote(name, safe='')}"
    if query:
        url += "?" + urlencode(query)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return request("PUT", url, account_id, path.read_bytes(), content_type)


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload and manage files on FuckingFast.")
    parser.add_argument("--account-id", help=argparse.SUPPRESS)
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("upload")
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--parent-id")
    p.add_argument("--location-id")
    p.add_argument("--note")

    subs.add_parser("locations")
    subs.add_parser("account")
    p = subs.add_parser("ls")
    p.add_argument("directory_id", nargs="?")
    p = subs.add_parser("mkdir")
    p.add_argument("parent_id")
    p.add_argument("name")
    p = subs.add_parser("rename")
    p.add_argument("item_id")
    p.add_argument("name")
    p = subs.add_parser("move")
    p.add_argument("item_id")
    p.add_argument("parent_id")
    p = subs.add_parser("note")
    p.add_argument("item_id")
    p.add_argument("text")
    p = subs.add_parser("delete")
    p.add_argument("directory_id")
    p.add_argument("--yes", action="store_true")

    args = parser.parse_args()
    if args.command == "upload":
        result = upload(args)
    elif args.command == "locations":
        result = request("GET", f"{API}/locations", credential(args, False))
    elif args.command == "account":
        result = request("GET", f"{API}/account", credential(args))
    elif args.command == "ls":
        suffix = f"/{quote(args.directory_id, safe='')}" if args.directory_id else ""
        result = request("GET", f"{API}/fs{suffix}", credential(args))
    elif args.command == "mkdir":
        result = request("POST", f"{API}/fs/{quote(args.parent_id, safe='')}", credential(args), json_body({"name": args.name}), "application/json")
    elif args.command == "rename":
        result = request("PATCH", f"{API}/fs/{quote(args.item_id, safe='')}", credential(args), json_body({"name": args.name}), "application/json")
    elif args.command == "move":
        result = request("PUT", f"{API}/fs/{quote(args.item_id, safe='')}", credential(args), json_body({"parentId": args.parent_id}), "application/json")
    elif args.command == "note":
        result = request("PUT", f"{API}/fs/{quote(args.item_id, safe='')}", credential(args), json_body({"note": args.text}), "application/json")
    else:
        if not args.yes:
            raise SystemExit("Refusing deletion without --yes.")
        result = request("DELETE", f"{API}/fs/{quote(args.directory_id, safe='')}", credential(args))

    print(json.dumps(result, indent=2) if not isinstance(result, str) else result)


if __name__ == "__main__":
    main()
