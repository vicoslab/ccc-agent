"""Guided driver for acceptance runs performed in a real desktop/remote client.

This module does not launch or emulate a desktop client. It pauses while an
operator follows the product's own UI, captures what was observed, and writes
the atomic phase files consumed by :mod:`harness`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Mapping


def build_operator_script(scenario: Mapping) -> str:
    instructions = scenario.get("operator_instructions") or ()
    evidence = scenario.get("evidence") or {}
    lines = [
        "REAL OBSERVED-CLIENT ACCEPTANCE",
        "Client: %s %s" % (
            evidence.get("client_product", scenario.get("agent", "unknown")),
            evidence.get("client_version", "VERSION MUST BE RECORDED")),
        "",
        "This is a manual observation, not an app-server emulation.",
        "Do not run %s app-server yourself and do not replace the client UI "
        "with a CLI." % scenario.get("agent", "the vendor"),
        "Use the product's normal SSH/remote connection UI.",
        "",
        "Operator steps:",
    ]
    lines.extend("%d. %s" % (index, item)
                 for index, item in enumerate(instructions, 1))
    lines.extend([
        "%d. Open this workspace in the remote client: %s" %
        (len(instructions) + 1, scenario.get("workspace")),
        "%d. Capture a screenshot showing the client, target, and workspace." %
        (len(instructions) + 2),
        "%d. Paste the FIRST prompt below into the client exactly." %
        (len(instructions) + 3),
        "",
        "----- FIRST PROMPT -----",
        str(scenario.get("initial_prompt", "")),
        "----- END FIRST PROMPT -----",
        "",
        "After the client visibly asks for CCC commit/discard/keep review, return "
        "to this terminal and press Enter. You will then paste the observed response.",
        "The harness will verify files, CCC events, session kind, and cleanup on "
        "the server; your pasted transcript is supporting UI evidence.",
        "",
        "The SECOND prompt, shown again after the server-side first-turn checks, is:",
        "----- SECOND PROMPT -----",
        str(scenario.get("decision_prompt", "")),
        "----- END SECOND PROMPT -----",
    ])
    return "\n".join(lines)


def _read_multiline(label: str) -> str:
    print(label)
    print("Paste text now; finish with a line containing only CCC_END.")
    lines = []
    while True:
        line = input()
        if line == "CCC_END":
            return "\n".join(lines)
        lines.append(line)


def _confirm(question: str) -> bool:
    while True:
        answer = input(question + " [y/n]: ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def _atomic_json(path: str, value: Mapping) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _wait_for(path: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.isfile(path):
            return
        time.sleep(0.25)
    raise TimeoutError("timed out waiting for server-side continuation: %s" % path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Guide and record a real desktop/remote-client acceptance run")
    parser.add_argument("scenario_file", nargs="?", default=os.environ.get(
        "CCC_AGENT_ACCEPTANCE_SCENARIO"))
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    args = parser.parse_args(argv)
    if not args.scenario_file:
        parser.error("scenario_file or CCC_AGENT_ACCEPTANCE_SCENARIO is required")

    with open(args.scenario_file, encoding="utf-8") as handle:
        scenario = json.load(handle)
    result_path = os.environ.get("CCC_AGENT_ACCEPTANCE_RESULT") or scenario.get(
        "driver_result_file")
    continue_path = os.environ.get("CCC_AGENT_ACCEPTANCE_CONTINUE") or scenario.get(
        "driver_continue_file")
    if not result_path or not continue_path:
        parser.error("acceptance result/continue paths are unavailable")

    print(build_operator_script(scenario), flush=True)
    input("\nPress Enter only after the first real-client turn is ready: ")
    first_response = _read_multiline("Paste the client's visible first response.")
    _atomic_json(result_path, {
        "phase": "first-turn-ready",
        "first_response": first_response,
        "transcript": first_response,
        "evidence": scenario.get("evidence"),
    })

    print("Waiting for objective server-side first-turn verification...", flush=True)
    _wait_for(continue_path, args.timeout_seconds)
    print("\nPaste this SECOND prompt into the same real client:\n")
    print(scenario.get("decision_prompt", ""), flush=True)
    input("\nPress Enter after the second turn completes and then close the client: ")
    decision_response = _read_multiline("Paste the client's visible second response.")

    confirmations = {
        "used_official_client": _confirm("Did you use the named official GUI/remote client?"),
        "server_started_through_ssh_router": _confirm(
            "Did that client connect through the configured CCC SSH target/router?"),
        "protocol_clean": _confirm(
            "Did the client show no CCC plugin or MCP startup failure?"),
        "plugin_loaded": _confirm("Did the client show the CCC plugin as loaded?"),
        "plugin_used": _confirm("Did the observed turns use CCC review/status behavior?"),
        "workspace_registered": _confirm(
            "Did the client open the exact acceptance workspace?"),
        "asked_user": _confirm(
            "Did the client visibly ask commit/discard/keep before your answer?"),
        "status_used": _confirm("Did the client visibly return CCC status?"),
    }
    hooks = input("Observed CCC hook names (comma-separated): ").strip().split(",")
    skills = input("Observed CCC skill names (comma-separated): ").strip().split(",")
    result = {
        "phase": "complete",
        **confirmations,
        "first_response": first_response,
        "decision_response": decision_response,
        "transcript": first_response + "\n" + decision_response,
        "plugin_inventory": {
            "hooks": [item.strip() for item in hooks if item.strip()],
            "skills": [item.strip() for item in skills if item.strip()],
        },
        "evidence": scenario.get("evidence"),
        "observation_artifacts": input(
            "Screenshot/log artifact paths (comma-separated): ").strip().split(","),
    }
    _atomic_json(result_path, result)
    print("Observation recorded; the server-side harness will finish verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
