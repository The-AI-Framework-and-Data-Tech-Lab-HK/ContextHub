#!/usr/bin/env python3
"""
run_random_prompts.py — Pick k random prompts and run them via OpenClaw.

Usage:
    python3 run_random_prompts.py --k 5
    python3 run_random_prompts.py --k 10 --prompts-dir my_prompts/
"""

import argparse
import json
import random
import subprocess
import time
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Run k random prompts via OpenClaw.")
    parser.add_argument("--k", type=int, required=True, help="Number of random prompts to run")
    parser.add_argument("--prompts-dir", default="prompts", help="Directory containing prompt .txt files (default: prompts/)")
    parser.add_argument("--outputs-dir", default="outputs", help="Directory for output files (default: outputs/)")
    parser.add_argument("--agent", default="main", help="OpenClaw agent id (default: main)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="OpenClaw agent timeout in seconds (default: no timeout)",
    )
    parser.add_argument("--retries", type=int, default=0, help="Retries per prompt after first attempt (default: 0)")
    parser.add_argument("--seed", type=int, default=None, help="Optional random seed for reproducibility")
    parser.add_argument("--json-output", action="store_true", help="Force --json mode and parse payload text")
    parser.add_argument(
        "--session-mode",
        choices=["fresh", "shared"],
        default="fresh",
        help="Use fresh session per run (default) or shared agent session",
    )
    return parser.parse_args()


def extract_json_from_mixed_stdout(stdout: str):
    stdout = (stdout or "").strip()
    if not stdout:
        return None

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    candidates = []
    for i, ch in enumerate(stdout):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(stdout[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            candidates.append(obj)

    if not candidates:
        return None

    for obj in reversed(candidates):
        top_payloads = isinstance(obj.get("payloads"), list)
        nested_payloads = isinstance(obj.get("result"), dict) and isinstance(obj["result"].get("payloads"), list)
        if top_payloads or nested_payloads:
            return obj
    return candidates[-1]


def extract_payload_text(data: dict) -> str:
    payloads = []
    if isinstance(data.get("payloads"), list):
        payloads = data["payloads"]
    elif isinstance(data.get("result"), dict) and isinstance(data["result"].get("payloads"), list):
        payloads = data["result"]["payloads"]

    if payloads and isinstance(payloads[0], dict):
        return str(payloads[0].get("text") or "")
    return ""


def call_openclaw(message: str, agent: str, timeout_s: int | None, json_output: bool, session_id: str | None) -> dict:
    cmd = ["openclaw", "agent", "--agent", agent, "-m", message]
    if timeout_s is not None and timeout_s > 0:
        cmd.extend(["--timeout", str(timeout_s)])
    if session_id:
        cmd.extend(["--session-id", session_id])
    if json_output:
        cmd.append("--json")

    try:
        if json_output:
            subprocess_timeout = timeout_s + 30 if timeout_s is not None and timeout_s > 0 else None
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=subprocess_timeout)
        else:
            # Let OpenClaw run attached to the terminal to match TUI-like behavior.
            subprocess_timeout = timeout_s + 30 if timeout_s is not None and timeout_s > 0 else None
            result = subprocess.run(cmd, text=True, timeout=subprocess_timeout)
    except subprocess.TimeoutExpired as exc:
        return {
            "timed_out": True,
            "returncode": None,
            "response_text": "",
            "stop_reason": None,
            "stderr_preview": str(exc)[:200],
            "stdout_preview": ((exc.stdout or "")[:200] if isinstance(exc.stdout, str) else ""),
        }

    stdout_text = result.stdout if json_output else ""
    data = extract_json_from_mixed_stdout(stdout_text)
    response_text = ""
    stop_reason = None
    if isinstance(data, dict):
        response_text = extract_payload_text(data)
        if isinstance(data.get("meta"), dict):
            stop_reason = data["meta"].get("stopReason")

    stderr_preview = (result.stderr or "").strip()[:200] if json_output else ""
    stdout_preview = (stdout_text or "").strip()[:200]

    return {
        "timed_out": False,
        "returncode": result.returncode,
        "response_text": response_text,
        "stop_reason": stop_reason,
        "stderr_preview": stderr_preview,
        "stdout_preview": stdout_preview,
    }


def snapshot_output(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "size": 0, "mtime_ns": None, "text": ""}
    stat = path.stat()
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = ""
    return {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "text": text}


def main():
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    prompts_dir = Path(args.prompts_dir)
    outputs_dir = Path(args.outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    all_txts = sorted(prompts_dir.glob("*.txt"))
    if not all_txts:
        print(f"ERROR: No .txt files found in {prompts_dir}")
        return

    k = min(args.k, len(all_txts))
    selected = random.sample(all_txts, k)

    print(f"Selected {k} prompts: {[f.name for f in selected]}\n")

    for i, prompt_file in enumerate(selected, 1):
        output_file = outputs_dir / prompt_file.stem
        message = prompt_file.read_text(encoding="utf-8")
        print(f"[{i}/{k}] Running {prompt_file.name} ...")

        total_attempts = max(1, args.retries + 1)
        before = snapshot_output(output_file)

        for attempt in range(1, total_attempts + 1):
            if total_attempts > 1:
                print(f"  → Attempt {attempt}/{total_attempts}")

            session_id = None
            if args.session_mode == "fresh":
                session_id = f"batch-{prompt_file.stem}-{int(time.time() * 1000)}-{attempt}"
                print(f"  → Session: {session_id}")
            else:
                print("  → Session: shared")

            run = call_openclaw(
                message=message,
                agent=args.agent,
                timeout_s=args.timeout,
                json_output=args.json_output,
                session_id=session_id,
            )
            response = run["response_text"]
            if args.json_output:
                if response:
                    print(f"  → Response: {response[:200]}{'...' if len(response) > 200 else ''}")
                else:
                    print("  → Response: (empty)")

            if run["stop_reason"]:
                print(f"  → stopReason: {run['stop_reason']}")
            if run["timed_out"]:
                print("  → Runner timeout: subprocess timed out")
            elif run["returncode"] not in (0, None):
                print(f"  → openclaw exit code: {run['returncode']}")
            if run["stderr_preview"]:
                print(f"  → stderr: {run['stderr_preview']}")
            if args.json_output and run["stdout_preview"] and not response:
                print(f"  → stdout: {run['stdout_preview']}")

            after = snapshot_output(output_file)
            output_changed = (
                before["exists"] != after["exists"]
                or before["size"] != after["size"]
                or before["mtime_ns"] != after["mtime_ns"]
                or before["text"] != after["text"]
            )
            output_has_content = after["exists"] and bool(after["text"].strip())
            wrote_new_nonempty_output = output_has_content and (output_changed or not bool(before["text"].strip()))

            if wrote_new_nonempty_output:
                print(f"  → Output file updated with content: {output_file}\n")
                break

            print(f"  → Output file missing/empty/unchanged: {output_file}")
            if attempt < total_attempts:
                print("  → Retrying...\n")
            else:
                if total_attempts == 1:
                    print("  → No retry configured.\n")
                else:
                    print("  → Gave up after retries.\n")


if __name__ == "__main__":
    main()