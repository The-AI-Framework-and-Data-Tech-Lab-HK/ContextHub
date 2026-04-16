#!/usr/bin/env python3
"""
Evaluate aggregated accuracy for OpenClaw FDAbench outputs.

Supports two modes:
- single: one-letter answer evaluation
- multiple: set-equivalence evaluation (prediction set == gold set)
"""

from __future__ import annotations

import ast
import argparse
import json
import re
from pathlib import Path

MODE_TO_OUTPUT_DIR = {
    "single": "/home/qchenax/.openclaw/workspace/outputs/single",
    "multiple": "/home/qchenax/.openclaw/workspace/outputs/multiple",
}
MODE_TO_JSONL_PATH = {
    "single": "/home/qchenax/FDAbench/fdabench-lite/single/data.jsonl",
    "multiple": "/home/qchenax/FDAbench/fdabench-lite/multiple/data.jsonl",
}
DEFAULT_SINGLE_CHOICES = {"A", "B", "C", "D"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate outputs against FDAbench ground truth.")
    parser.add_argument(
        "--mode",
        choices=["single", "multiple"],
        required=True,
        help="Evaluation mode (required): single or multiple",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory containing task output files (default depends on --mode)",
    )
    parser.add_argument(
        "--jsonl-path",
        default=None,
        help="Path to FDAbench jsonl file (default depends on --mode)",
    )
    return parser.parse_args()


def load_ground_truth(jsonl_path: Path) -> dict[str, dict]:
    ground_truth: dict[str, dict] = {}
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
            options = item.get("options", {})
            if not task_id:
                continue

            if isinstance(correct_answer, list):
                answers = {str(x).strip().upper() for x in correct_answer if str(x).strip()}
            else:
                token = str(correct_answer).strip().upper()
                answers = {token} if token else set()

            valid_choices = set()
            if isinstance(options, dict):
                valid_choices = {str(k).strip().upper() for k in options.keys() if str(k).strip()}

            ground_truth[task_id] = {
                "answers": answers,
                "valid_choices": valid_choices,
            }
    return ground_truth


def extract_single_prediction(text: str, valid_choices: set[str]) -> str | None:
    for ch in text.strip().upper():
        if ch in valid_choices:
            return ch
    return None


def _normalize_choice_set(values, valid_choices: set[str]) -> set[str]:
    result: set[str] = set()
    if isinstance(values, str):
        values = re.findall(r"\b[A-Z]\b", values.upper())
    for value in values:
        token = str(value).strip().upper()
        if token in valid_choices:
            result.add(token)
    return result


def extract_multiple_prediction(text: str, valid_choices: set[str]) -> set[str] | None:
    raw = text.strip()
    if not raw:
        return None

    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(raw)
        except (json.JSONDecodeError, SyntaxError, ValueError):
            continue
        if isinstance(parsed, (list, tuple, set)):
            return _normalize_choice_set(parsed, valid_choices)
        if isinstance(parsed, str):
            return _normalize_choice_set(parsed, valid_choices)

    fallback = _normalize_choice_set(raw, valid_choices)
    return fallback if fallback else None


def evaluate(outputs_dir: Path, ground_truth: dict[str, dict], mode: str) -> tuple[int, int, int, int]:
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
        gt_item = ground_truth[task_id]
        answers: set[str] = gt_item.get("answers", set())
        valid_choices: set[str] = gt_item.get("valid_choices", set())
        if not valid_choices and mode == "single":
            valid_choices = set(DEFAULT_SINGLE_CHOICES)

        content = path.read_text(encoding="utf-8")
        if mode == "single":
            prediction = extract_single_prediction(content, valid_choices)
            if prediction is None:
                invalid_pred += 1
                continue
            if prediction in answers:
                correct += 1
            elif not answers:
                missing_gt += 1
        else:
            prediction_set = extract_multiple_prediction(content, valid_choices)
            if prediction_set is None:
                invalid_pred += 1
                continue
            # Strict set equivalence for multiple-choice tasks.
            if prediction_set == answers:
                correct += 1
            elif not answers:
                missing_gt += 1

    return evaluated, correct, missing_gt, invalid_pred


def main() -> None:
    args = parse_args()
    outputs_dir = Path(args.output_dir or MODE_TO_OUTPUT_DIR[args.mode])
    jsonl_path = Path(args.jsonl_path or MODE_TO_JSONL_PATH[args.mode])

    if not outputs_dir.exists():
        raise FileNotFoundError(f"Outputs directory not found: {outputs_dir}")
    if not jsonl_path.exists():
        raise FileNotFoundError(f"JSONL file not found: {jsonl_path}")

    ground_truth = load_ground_truth(jsonl_path)
    evaluated, correct, missing_gt, invalid_pred = evaluate(outputs_dir, ground_truth, args.mode)

    accuracy = (correct / evaluated) if evaluated else 0.0
    print(f"mode: {args.mode}")
    print(f"output_dir: {outputs_dir}")
    print(f"jsonl_path: {jsonl_path}")
    print(f"evaluated: {evaluated}")
    print(f"correct: {correct}")
    print(f"accuracy: {accuracy:.4f} ({accuracy * 100:.2f}%)")
    print(f"invalid_predictions: {invalid_pred}")
    print(f"missing_ground_truth: {missing_gt}")


if __name__ == "__main__":
    main()
