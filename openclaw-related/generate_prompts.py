#!/usr/bin/env python3
"""
Generate one prompt .txt per task from FDAbench jsonl.

Usage:
    python3 generate_prompts.py
    python3 generate_prompts.py --work-dir /your/workdir
    python3 generate_prompts.py --jsonl-path /path/to/data.jsonl --output-dir /path/to/prompts
"""

import argparse
import json
from pathlib import Path

DEFAULT_WORK_DIR = "/home/qchenax/.openclaw/workspace"
DEFAULT_JSONL_PATH = "/home/qchenax/FDAbench/fdabench-lite/single/data.jsonl"
DEFAULT_OUTPUT_DIR = "/home/qchenax/.openclaw/workspace/prompts"
FDA_STRUCTURED_DATA_PATH = "/home/qchenax/FDAbench/fdabench-lite/sqlite"
FDA_UNSTRUCTURED_DATA_PATH = "/home/qchenax/FDAbench/fdabench-lite/Vector_Database"

PROMPT_TEMPLATE = """\
You are a professional data analyst. Your working directory is {work_dir}. You are given the following question to answer:

{query}

You should answer the question based on both structured and unstructured data sources.
The structured data is a SQLite database at {structured_data_path}/{db}.sqlite.
The unstructured data store is located at {unstructured_data_path}. The unstructured data store is very diverse and you need to find whatever is useful for the task.

You should return the answer within 180 seconds.If uncertain, return the best guess.
Choose EXACTLY ONE answer from the following options:

{options}

Output ONE SINGLE LETTER to {work_dir}/outputs/{task_id} the as the final answer. Do not include any other text in your output.
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate prompt .txt files from FDAbench jsonl records."
    )
    parser.add_argument(
        "--work-dir",
        default=DEFAULT_WORK_DIR,
        help=f"Working directory path used inside the prompts (default: {DEFAULT_WORK_DIR})",
    )
    parser.add_argument(
        "--jsonl-path",
        default=DEFAULT_JSONL_PATH,
        help=f"Path to the .jsonl file (default: {DEFAULT_JSONL_PATH})",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to write prompt .txt files (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--structured-data-path",
        default=FDA_STRUCTURED_DATA_PATH,
        help="Base directory containing SQLite files (<db>.sqlite).",
    )
    parser.add_argument(
        "--unstructured-data-path",
        default=FDA_UNSTRUCTURED_DATA_PATH,
        help="Directory for unstructured data store.",
    )
    return parser.parse_args()


def load_items(jsonl_path: Path) -> list[dict]:
    items: list[dict] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at line {line_no} in {jsonl_path}: {exc}") from exc
    return items


def format_options(options: dict) -> str:
    if not isinstance(options, dict):
        raise ValueError(f"'options' must be a dict, got: {type(options).__name__}")
    # Keep deterministic ordering (A, B, C, D...).
    sorted_keys = sorted(options.keys())
    return "\n".join(f"{key}. {options[key]}" for key in sorted_keys)


def main():
    args = parse_args()

    jsonl_path = Path(args.jsonl_path)
    output_dir = Path(args.output_dir)
    work_dir = args.work_dir
    structured_data_path = args.structured_data_path
    unstructured_data_path = args.unstructured_data_path

    output_dir.mkdir(parents=True, exist_ok=True)
    items = load_items(jsonl_path)

    generated = 0
    skipped = 0
    for item in items:
        task_id = item.get("task_id")
        query = item.get("query")
        db = item.get("db")
        options = item.get("options")

        if not task_id or not query or not db or not options:
            skipped += 1
            print(f"WARNING: missing required fields, skipping item: {task_id}")
            continue

        prompt = PROMPT_TEMPLATE.format(
            work_dir=work_dir,
            query=query,
            db=db,
            options=format_options(options),
            task_id=task_id,
            structured_data_path=structured_data_path,
            unstructured_data_path=unstructured_data_path,
        )

        output_path = output_dir / f"{task_id}.txt"
        output_path.write_text(prompt, encoding="utf-8")
        generated += 1

    print(f"Generated {generated} prompt files in {output_dir}")
    print(f"Skipped {skipped} items with missing fields.")
    print(f"Work dir used: {work_dir}")


if __name__ == "__main__":
    main()