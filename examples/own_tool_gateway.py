"""Executable integration sample; no persistence, retries or tool execution."""

import argparse
import json
import os
import re
import sys
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 10 * 1024 * 1024


def validate_origin(origin):
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or any(c.isspace() for c in origin)):
        raise ValueError("Set an HTTPS gateway origin only, without credentials, path or query.")
    return origin.rstrip("/")


def validate_operation(operation_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", operation_id):
        raise ValueError("Use a stable, non-sensitive business operation ID (1-120 ASCII characters).")


def bounded_body(response):
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > MAX_BYTES:
            raise ValueError("Response exceeds sample limit.")
        body.extend(chunk)
    return bytes(body)


def stream_output(body):
    """Parse complete SSE frames; return partial events without claiming success."""
    events, data, done = [], [], False
    for line in body.decode("utf-8").splitlines():
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line == "" and data:
            value = "\n".join(data)
            data = []
            if done:
                raise ValueError("Unexpected data after DONE.")
            if value == "[DONE]":
                done = True
            else:
                event = json.loads(value)
                if not isinstance(event, dict):
                    raise ValueError("Invalid SSE event.")
                events.append(event)
    return events, done and not data


class GatewayTool:
    def __init__(self, origin, api_key, *, client):
        self.origin = validate_origin(origin)
        if not api_key or any(c.isspace() for c in api_key):
            raise ValueError("A gateway site Key is required; never use a supplier Key here.")
        self.api_key = api_key
        self.client = client

    def headers(self, operation_id):
        validate_operation(operation_id)
        return {"Authorization": "Bearer " + self.api_key, "Idempotency-Key": operation_id}

    def lookup(self, operation_id):
        headers = self.headers(operation_id)
        try:
            with self.client.stream("POST", self.origin + "/v1/requests/lookup",
                                    headers=headers, follow_redirects=False, timeout=15) as response:
                status = response.status_code
                body = bounded_body(response)
            task = json.loads(body) if status == 200 else None
            if task is not None and not isinstance(task, dict):
                raise ValueError("Invalid task metadata.")
            return {"lookup_http_status": status, "task": task, "automatic_resubmit_allowed": False}
        except (httpx.HTTPError, ValueError, UnicodeError):
            return {"lookup_http_status": None, "task": None, "automatic_resubmit_allowed": False}

    def submit(self, operation_id, payload):
        headers = self.headers(operation_id)
        if not isinstance(payload, dict):
            raise ValueError("Request must be a JSON object.")
        output, complete, status = None, False, None
        try:
            with self.client.stream("POST", self.origin + "/v1/chat/completions", headers=headers,
                                    json=payload, follow_redirects=False, timeout=100) as response:
                status = response.status_code
                body = bounded_body(response)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if status == 200:
                if payload.get("stream") is True:
                    if content_type != "text/event-stream":
                        raise ValueError("Expected SSE.")
                    output, complete = stream_output(body)
                else:
                    output = json.loads(body)
                    complete = isinstance(output, dict) and isinstance(output.get("choices"), list)
        except (httpx.HTTPError, ValueError, UnicodeError):
            complete = False
        # This is a read-only endpoint despite its POST verb. Never POST a new
        # completion on timeout, HTTP error, a missing answer, or a lookup 404.
        recovery = self.lookup(operation_id)
        settled = (recovery["task"] or {}).get("status") == "settled"
        return {"delivery": "complete" if complete and settled else "needs_review",
                "http_status": status, "output": output, **recovery}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation-id", required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--execute", action="store_true", help="Submit stdin JSON; may incur a charge.")
    actions.add_argument("--lookup", action="store_true", help="Read original task metadata only.")
    args = parser.parse_args(argv)
    try:
        origin = validate_origin(os.environ.get("KUNLUN_TOOL_ORIGIN", ""))
        validate_operation(args.operation_id)
        if not args.execute and not args.lookup:
            print(json.dumps({"mode": "preview", "origin": origin, "operation_id": args.operation_id,
                              "network_calls": 0, "next_step": "Use --execute with JSON stdin, or --lookup."}))
            return 0
        # trust_env=False avoids inherited proxy/auth settings. No transport
        # retries, redirects, cookies or supplier credentials are configured.
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            tool = GatewayTool(origin, os.environ.get("KUNLUN_TOOL_API_KEY", ""), client=client)
            if args.lookup:
                result = tool.lookup(args.operation_id)
            else:
                raw = sys.stdin.buffer.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("Input exceeds the sample's 1 MiB limit.")
                result = tool.submit(args.operation_id, json.loads(raw))
        # Output deliberately returns business content to the invoking tool.
        # Do not direct stdout into telemetry, shared logs or version control.
        print(json.dumps(result, ensure_ascii=False))
        return 0 if (result.get("delivery") == "complete" or
                     args.lookup and result.get("lookup_http_status") == 200) else 2
    except (ValueError, UnicodeError, httpx.HTTPError):
        print("Invalid configuration/input or transport failure; no automatic resubmit. Check the original task.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
