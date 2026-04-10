#!/usr/bin/env python3
"""
Evaluate aggregated accuracy for OpenClaw FDAbench outputs.

It compares each output file in the outputs directory against ground-truth
answers in the FDAbench jsonl (matched by task_id / filename).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_OUTPUT_DIR = "/home/qchenax/.openclaw/workspace/outputs"
DEFAULT_JSONL_PATH = "/home/qchenax/FDAbench/fdabench-lite/single/data.jsonl"

VALID_CHOICES = {"A", "B", "C", "D"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate outputs against FDAbench ground truth.")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory containing task output files (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--jsonl-path",
        default=DEFAULT_JSONL_PATH,
        help=f"Path to FDAbench jsonl file (default: {DEFAULT_JSONL_PATH})",
    )
    return parser.parse_args()


def load_ground_truth(jsonl_path: Path) -> dict[str, set[str]]:
    ground_truth: dict[str, set[str]] = {}
    with jsonl_path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at line {line_no}: {exc}") from exc

            task_id = item.get("task_id")
            correct_answer = item.get("correct_answer", [])
            if not task_id:
                continue
            if isinstance(correct_answer, list):
                answers = {str(x).strip().upper() for x in correct_answer if str(x).strip()}
            else:
                answers = {str(correct_answer).strip().upper()} if str(correct_answer).strip() else set()
            ground_truth[task_id] = answers
    return ground_truth


def extract_prediction(text: str) -> str | None:
    for ch in text.strip().upper():
        if ch in VALID_CHOICES:
            return ch
    return None


def evaluate(outputs_dir: Path, ground_truth: dict[str, set[str]]) -> tuple[int, int, int, int]:
    evaluated = 0
    correct = 0
    missing_gt = 0
    invalid_pred = 0

    for path in sorted(outputs_dir.iterdir()):
        if not path.is_file():
            continue

        task_id = path.name
        if task_id not in ground_truth:
            # Ignore files that are not task outputs, e.g. result.md
            continue

        evaluated += 1
        prediction = extract_prediction(path.read_text(encoding="utf-8"))
        if prediction is None:
            invalid_pred += 1
            continue

        if prediction in ground_truth.get(task_id, set()):
            correct += 1
        elif not ground_truth.get(task_id):
            missing_gt += 1

    return evaluated, correct, missing_gt, invalid_pred


def main() -> None:
    args = parse_args()
    outputs_dir = Path(args.output_dir)
    jsonl_path = Path(args.jsonl_path)

    if not outputs_dir.exists():
        raise FileNotFoundError(f"Outputs directory not found: {outputs_dir}")
    if not jsonl_path.exists():
        raise FileNotFoundError(f"JSONL file not found: {jsonl_path}")

    ground_truth = load_ground_truth(jsonl_path)
    evaluated, correct, missing_gt, invalid_pred = evaluate(outputs_dir, ground_truth)

    accuracy = (correct / evaluated) if evaluated else 0.0
    print(f"evaluated: {evaluated}")
    print(f"correct: {correct}")
    print(f"accuracy: {accuracy:.4f} ({accuracy * 100:.2f}%)")
    print(f"invalid_predictions: {invalid_pred}")
    print(f"missing_ground_truth: {missing_gt}")


if __name__ == "__main__":
    main()
