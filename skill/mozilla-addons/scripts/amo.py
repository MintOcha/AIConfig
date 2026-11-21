# /// script
# requires-python = ">=3.11"
# dependencies = ["PyJWT>=2,<3", "requests>=2,<3"]
# ///

import argparse
import json
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import unquote, urlsplit

import jwt
import requests

API = "https://addons.mozilla.org/api/v5/"
CREDENTIALS = "aiconfig/mozilla-addons"


def credential(name):
    entry = f"{CREDENTIALS}/{name}"
    try:
        result = subprocess.run(["pass", "show", entry], check=True, stdout=subprocess.PIPE,
                                text=True)
    except FileNotFoundError:
        raise ValueError("Install and initialize pass; see the skill's credential setup.") from None
    except subprocess.CalledProcessError:
        raise ValueError(f"Could not read pass entry {entry}.") from None
    value = result.stdout.partition("\n")[0].strip()
    if not value:
        raise ValueError(f"Empty pass entry {entry}.")
    return value


def authorization():
    issuer = credential("jwt-issuer")
    secret = credential("jwt-secret")
    now = int(time.time())
    token = jwt.encode(
        {"iss": issuer, "iat": now, "exp": now + 60, "jti": uuid.uuid4().hex},
        secret,
        algorithm="HS256",
    )
    return {"Authorization": f"JWT {token}"}


def field(argument):
    name, separator, value = argument.partition("=")
    if not name or not separator:
        raise argparse.ArgumentTypeError("expected NAME=VALUE")
    return name, value


def main():
    parser = argparse.ArgumentParser(description="Call the Mozilla Add-ons (AMO) API v5.")
    parser.add_argument("method", choices=("GET", "POST", "PATCH"))
    parser.add_argument("endpoint", help="Path relative to /api/v5/, including its trailing slash")
    parser.add_argument("--public", action="store_true", help="Make an unauthenticated GET")
    parser.add_argument("--json", type=Path, metavar="PATH", help="Read the request JSON from a file")
    parser.add_argument("--field", type=field, action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--file", type=field, action="append", default=[], metavar="NAME=PATH")
    args = parser.parse_args()

    endpoint = urlsplit(args.endpoint)
    path = unquote(endpoint.path)
    if (endpoint.scheme or endpoint.netloc or endpoint.fragment or path.startswith("/")
            or ".." in path.split("/") or not path.endswith("/")):
        parser.error("provide a relative API endpoint ending in '/', not a URL")
    if args.method == "GET" and (args.json or args.field or args.file):
        parser.error("GET does not take a request body")
    if args.public and args.method != "GET":
        parser.error("--public is only for GET")
    if args.json and (args.field or args.file):
        parser.error("use --json or multipart fields, not both")

    try:
        headers = {} if args.public else authorization()
        with ExitStack() as stack:
            payload = None
            if args.json:
                with args.json.open(encoding="utf-8") as file:
                    payload = json.load(file)
            files = [(name, (None, value)) for name, value in args.field]
            files.extend((name, (Path(path).name, stack.enter_context(Path(path).open("rb"))))
                         for name, path in args.file)
            with requests.Session() as session:
                session.trust_env = False  # Do not let ~/.netrc replace JWT auth.
                response = session.request(args.method, API + args.endpoint, headers=headers,
                                           json=payload, files=files, timeout=(10, 120),
                                           allow_redirects=False)
    except (OSError, ValueError, requests.RequestException) as error:
        print(f"amo: {error}", file=sys.stderr)
        return 1
    if not 200 <= response.status_code < 300:
        print(f"amo: HTTP {response.status_code}\n{response.text}", file=sys.stderr)
        return 1
    if response.content:
        print(response.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
