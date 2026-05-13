#!/usr/bin/env python3
"""No-order deployment smoke runner for operator readiness checks."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:5000"
DEFAULT_AUTH_HEADER = "X-Operator-Token"

FORBIDDEN_ENDPOINTS = {
    "/api/auto-cycle",
    "/api/run-auto-cycle",
    "/api/control/emergency-stop",
    "/api/control/clear-emergency-stop",
    "/api/control/pause-auto-trading",
    "/api/control/resume-auto-trading",
    "/api/order",
    "/api/orders",
    "/api/position/close",
    "/api/positions/close",
}

REQUIRED_ENDPOINTS = [
    ("GET", "/api/operator-safe-endpoint-health", None, "required"),
    ("GET", "/api/operator-runbook", None, "required"),
    ("GET", "/api/paper-market-launch-gate", None, "required"),
    ("GET", "/api/market-open-command-center", None, "required"),
    ("GET", "/api/market-session-heartbeat", None, "required"),
    ("GET", "/api/auto-cycle-attempts?limit=5", None, "required"),
]

OPTIONAL_ENDPOINTS = [
    ("POST", "/api/pre-market-readiness-pipeline", {"include_live_scan_plan": False}, "optional_no_order_diagnostic"),
    ("POST", "/api/synthetic-auto-cycle-rehearsal", None, "optional_no_order_diagnostic"),
    ("POST", "/api/market-open-rehearsal", None, "optional_no_order_diagnostic"),
    ("POST", "/api/auto-cycle-plan", None, "optional_no_order_diagnostic"),
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run no-order operator smoke checks")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--token")
    p.add_argument("--auth-header", default=DEFAULT_AUTH_HEADER)
    p.add_argument("--timeout", default=10, type=float)
    p.add_argument("--include-market-plan", action="store_true")
    return p.parse_args(argv)


def resolve_token(cli_token: str | None) -> str | None:
    return cli_token if cli_token is not None else os.getenv("OPERATOR_AUTH_TOKEN")


def build_plan(include_market_plan: bool) -> list[tuple[str, str, Any, str]]:
    plan = list(REQUIRED_ENDPOINTS)
    if include_market_plan:
        plan.extend(OPTIONAL_ENDPOINTS)
    return plan


def assert_plan_safe(plan: list[tuple[str, str, Any, str]]) -> tuple[bool, list[str]]:
    bad = []
    for _method, path, _payload, _kind in plan:
        normalized = path.split("?", 1)[0]
        if normalized in FORBIDDEN_ENDPOINTS:
            bad.append(path)
    return (len(bad) == 0, bad)


def fetch_json(base_url: str, method: str, path: str, timeout: float, token: str | None, auth_header: str, payload: Any) -> tuple[int, dict[str, Any]]:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    headers: dict[str, str] = {"Accept": "application/json"}
    data = None
    if token:
        headers[auth_header] = token
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url=url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp else ""
        parsed = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
        return exc.code, parsed
    except Exception as exc:
        return 0, {"error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = resolve_token(args.token)
    plan = build_plan(args.include_market_plan)
    plan_ok, forbidden = assert_plan_safe(plan)
    if not plan_ok:
        print("FAIL forbidden endpoint(s) in plan:", ", ".join(forbidden))
        print(json.dumps({"ok": False, "overall_status": "FAIL", "forbidden_endpoint_check": forbidden}, sort_keys=True))
        return 1

    checked_endpoints, failed_endpoints, warnings = [], [], []
    launch_gate, command_center, heartbeat, safe_health, attempts = {}, {}, {}, {}, {}

    for method, path, payload, kind in plan:
        status, data = fetch_json(args.base_url, method, path, args.timeout, token, args.auth_header, payload)
        checked_endpoints.append(f"{method} {path}")
        if status == 401:
            print(f"FAIL auth failure at {method} {path}")
            print(json.dumps({"ok": False, "overall_status": "FAIL", "base_url": args.base_url, "checked_endpoints": checked_endpoints, "failed_endpoints": [f"{method} {path}"], "warnings": warnings, "forbidden_endpoint_check": []}, sort_keys=True))
            return 1
        if status >= 500 or status == 0:
            failed_endpoints.append(f"{method} {path}")
        elif status >= 400:
            warnings.append(f"{method} {path} returned {status}")

        payload_data = data.get("data") if isinstance(data, dict) else {}
        if path == "/api/operator-safe-endpoint-health":
            safe_health = payload_data or {}
        elif path == "/api/paper-market-launch-gate":
            launch_gate = payload_data or {}
        elif path == "/api/market-open-command-center":
            command_center = payload_data or {}
        elif path == "/api/market-session-heartbeat":
            heartbeat = payload_data or {}
        elif path.startswith("/api/auto-cycle-attempts"):
            attempts = payload_data or {}

    gate_status = launch_gate.get("launch_gate_status")
    go_for_paper = bool(launch_gate.get("go_for_paper_validation"))
    gate_pass = gate_status in {"GO_FOR_PAPER_MARKET_VALIDATION", "WAIT_FOR_MARKET_OPEN"} and go_for_paper

    if gate_status == "WAIT_FOR_MARKET_OPEN":
        warnings.append("Launch gate waiting for market open")

    ok = (not failed_endpoints) and gate_pass
    overall_status = "PASS" if ok and not warnings else ("WARN" if ok else "FAIL")

    latest_attempt = {}
    rows = attempts.get("attempts") if isinstance(attempts, dict) else []
    if rows:
        row = rows[0]
        latest_attempt = {
            "status": row.get("status"),
            "source": row.get("source"),
            "symbol": row.get("symbol"),
            "qty": row.get("qty"),
            "error": row.get("error"),
        }

    print("Operator Smoke Check Summary")
    print(f"- overall_status: {overall_status}")
    print(f"- safe_endpoint_health.ok: {safe_health.get('ok')}")
    print(f"- missing_expected_endpoints: {safe_health.get('missing_expected_endpoints', [])}")
    print(f"- unexpected_forbidden_present: {safe_health.get('unexpected_forbidden_present', [])}")
    print(f"- launch_gate_status: {gate_status}")
    print(f"- go_for_paper_validation: {go_for_paper}")
    print(f"- may_leave_scheduler_armed: {launch_gate.get('may_leave_scheduler_armed')}")
    print(f"- may_run_manual_auto_cycle_now: {launch_gate.get('may_run_manual_auto_cycle_now')}")
    print(f"- blocking_reasons: {launch_gate.get('blocking_reasons', [])}")
    print(f"- required_actions: {launch_gate.get('required_actions', [])}")
    print(f"- command_center_status: {command_center.get('command_center_status')}")
    print(f"- primary_action: {command_center.get('primary_action')}")
    print(f"- heartbeat_status: {heartbeat.get('heartbeat_status')}")
    print(f"- next_action_hint: {heartbeat.get('next_action_hint')}")
    print(f"- latest_attempt: {latest_attempt}")

    result = {
        "ok": ok,
        "overall_status": overall_status,
        "base_url": args.base_url,
        "checked_endpoints": checked_endpoints,
        "failed_endpoints": failed_endpoints,
        "launch_gate_status": gate_status,
        "go_for_paper_validation": go_for_paper,
        "may_leave_scheduler_armed": launch_gate.get("may_leave_scheduler_armed"),
        "required_actions": launch_gate.get("required_actions", []),
        "warnings": warnings,
        "forbidden_endpoint_check": forbidden,
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
