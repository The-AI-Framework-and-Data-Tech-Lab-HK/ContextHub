#!/usr/bin/env python3
"""
run_random_prompts.py — Pick k random prompts and run them via OpenClaw.

Usage:
    python3 run_random_prompts.py --mode single --k 5
    python3 run_random_prompts.py --mode multiple --k 10
    python3 run_random_prompts.py --mode single --k 10 --prompts-dir my_prompts/
"""

import argparse
import json
import random
import subprocess
import time
from pathlib import Path

DEFAULT_MODE = "single"
MODE_TO_PROMPTS_DIR = {
    "single": "/home/qchenax/.openclaw/workspace/prompts/single",
    "multiple": "/home/qchenax/.openclaw/workspace/prompts/multiple",
}
MODE_TO_OUTPUTS_DIR = {
    "single": "/home/qchenax/.openclaw/workspace/outputs/single",
    "multiple": "/home/qchenax/.openclaw/workspace/outputs/multiple",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Run k random prompts via OpenClaw.")
    parser.add_argument(
        "--mode",
        choices=["single", "multiple"],
        default=DEFAULT_MODE,
        help=f"Run mode (default: {DEFAULT_MODE})",
    )
    parser.add_argument("--k", type=int, required=True, help="Number of random prompts to run")
    parser.add_argument(
        "--prompts-dir",
        default=None,
        help="Directory containing prompt .txt files (default depends on --mode).",
    )
    parser.add_argument(
        "--outputs-dir",
        default=None,
        help="Directory for output files (default depends on --mode).",
    )
    parser.add_argument("--agent", default="main", help="OpenClaw agent id (default: main)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="OpenClaw agent timeout in seconds (default: no timeout)",
    )
    parser.add_argument("--seed", type=int, default=None, help="Optional random seed for reproducibility")
    parser.add_argument("--json-output", action="store_true", help="Force --json mode and parse payload text")
    parser.add_argument(
        "--session-mode",
        choices=["fresh", "shared"],
        default="fresh",
        help="Use fresh session per run (default) or shared agent session",
    )
    parser.add_argument(
        "--delete-unexpected-outputs",
        action="store_true",
        help="Delete files unexpectedly created in the output dir during a task run.",
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


def list_output_names(outputs_dir: Path) -> set[str]:
    return {p.name for p in outputs_dir.iterdir() if p.is_file()}


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

    prompts_dir = Path(args.prompts_dir or MODE_TO_PROMPTS_DIR[args.mode])
    outputs_dir = Path(args.outputs_dir or MODE_TO_OUTPUTS_DIR[args.mode])
    outputs_dir.mkdir(parents=True, exist_ok=True)

    all_txts = sorted(prompts_dir.glob("*.txt"))
    if not all_txts:
        print(f"ERROR: No .txt files found in {prompts_dir}")
        return

    k = min(args.k, len(all_txts))
    selected = random.sample(all_txts, k)

    print(f"Mode: {args.mode}")
    print(f"Prompts dir: {prompts_dir}")
    print(f"Outputs dir: {outputs_dir}")
    existing_output_names = list_output_names(outputs_dir)
    if existing_output_names:
        print(f"WARNING: outputs dir already has {len(existing_output_names)} files before this run.")
    print(f"Selected {k} prompts: {[f.name for f in selected]}\n")

    for i, prompt_file in enumerate(selected, 1):
        output_file = outputs_dir / prompt_file.stem
        message = prompt_file.read_text(encoding="utf-8")
        before_names = list_output_names(outputs_dir)
        print(f"[{i}/{k}] Running {prompt_file.name} ...")

        session_id = None
        if args.session_mode == "fresh":
            session_id = f"batch-{prompt_file.stem}-{int(time.time() * 1000)}"
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

        after_names = list_output_names(outputs_dir)
        unexpected_new = sorted((after_names - before_names) - {output_file.name})
        if unexpected_new:
            print(
                f"  → WARNING: detected {len(unexpected_new)} unexpected new output file(s): "
                f"{unexpected_new[:8]}{'...' if len(unexpected_new) > 8 else ''}"
            )
            if args.delete_unexpected_outputs:
                for name in unexpected_new:
                    try:
                        (outputs_dir / name).unlink()
                    except OSError:
                        pass
                print("  → Deleted unexpected output file(s)")

        after = snapshot_output(output_file)
        if not after["exists"]:
            output_file.write_text("", encoding="utf-8")
            print(f"  → Output file not found, created empty file: {output_file}\n")
            continue

        if after["text"].strip():
            print(f"  → Output file updated with content: {output_file}\n")
        else:
            print(f"  → Output file exists but empty: {output_file}\n")


if __name__ == "__main__":
    main()