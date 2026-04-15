#!/usr/bin/env python3
"""
Generate one prompt .txt per task from FDAbench jsonl.

Usage:
    python3 generate_prompts.py --mode single
    python3 generate_prompts.py --mode multiple
    python3 generate_prompts.py --mode single --work-dir /your/workdir
    python3 generate_prompts.py --mode multiple --jsonl-path /path/to/data.jsonl --output-dir /path/to/prompts
"""

import argparse
import json
from pathlib import Path

DEFAULT_WORK_DIR = "/home/qchenax/.openclaw/workspace"
DEFAULT_MODE = "single"
MODE_TO_JSONL_PATH = {
    "single": "/home/qchenax/FDAbench/fdabench-lite/single/data.jsonl",
    "multiple": "/home/qchenax/FDAbench/fdabench-lite/multiple/data.jsonl",
}
MODE_TO_OUTPUT_DIR = {
    "single": "/home/qchenax/.openclaw/workspace/prompts/single",
    "multiple": "/home/qchenax/.openclaw/workspace/prompts/multiple",
}
MODE_TO_GROUND_TRUTH_DIR = {
    "single": "/home/qchenax/.openclaw/workspace/ground_truth/single",
    "multiple": "/home/qchenax/.openclaw/workspace/ground_truth/multiple",
}
FDA_STRUCTURED_DATA_PATH = "/home/qchenax/FDAbench/fdabench-lite/sqlite"
FDA_UNSTRUCTURED_DATA_PATH = "/home/qchenax/FDAbench/fdabench-lite/Vector_Database"

PROMPT_TEMPLATE_SINGLE = """\
You are a professional data analyst. Your working directory is {work_dir}. You are given the following question to answer:

{query}

You should answer the question based on both structured and unstructured data sources.
The structured data is a SQLite database at {structured_data_path}/{db}.sqlite.
The unstructured data store is located at {unstructured_data_path}. The unstructured data store is very diverse and you need to find whatever is useful for the task.

You should return the answer within 180 seconds.If uncertain, return the best guess.
Choose EXACTLY ONE answer from the following options:

{options}

Output ONE SINGLE LETTER to {work_dir}/outputs/single/{task_id} as the final answer. Do not include any other text in your output.
Do not include any other text in your output.
Once you output the final answer, you are STRICTLY prohibited to update it!

After you output the final answer, go to {work_dir}/ground_truth/single/{task_id} to check the ground truth. 
You are STRICTLY prohibited to update the final answer at this stage!
If your answer is incorrect, you should reflect on the reason and consider how you could improve next time.
"""

PROMPT_TEMPLATE_MULTIPLE = """\
You are a professional data analyst. Your working directory is {work_dir}. You are given the following question to answer:

{query}

You should answer the question based on both structured and unstructured data sources.
The structured data is a SQLite database at {structured_data_path}/{db}.sqlite.
The unstructured data store is located at {unstructured_data_path}. The unstructured data store is very diverse and you need to find whatever is useful for the task.

You should return the answer within 180 seconds.If uncertain, return the best guess.
Choose ALL correct options from the following candidates:

{options}

Output ALL correct options in a list to {work_dir}/outputs/multiple/{task_id} as the final answer. For example, if the correct options are A and C, you should output ["A", "C"]. 
Do not include any other text in your output.
Once you output the final answer, you are STRICTLY prohibited to update it!

After you output the final answer, go to {work_dir}/ground_truth/multiple/{task_id} to check the ground truth. 
You are STRICTLY prohibited to update the final answer at this stage!
If your answer is incorrect, you should reflect on the reason and consider how you could improve next time.
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate prompt .txt files from FDAbench jsonl records."
    )
    parser.add_argument(
        "--mode",
        choices=["single", "multiple"],
        default=DEFAULT_MODE,
        help=f"Prompt generation mode (default: {DEFAULT_MODE})",
    )
    parser.add_argument(
        "--work-dir",
        default=DEFAULT_WORK_DIR,
        help=f"Working directory path used inside the prompts (default: {DEFAULT_WORK_DIR})",
    )
    parser.add_argument(
        "--jsonl-path",
        default=None,
        help="Path to the .jsonl file (default depends on --mode).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory to write prompt .txt files (default depends on --mode).",
    )
    parser.add_argument(
        "--ground-truth-dir",
        default=None,
        help="Directory to write per-task ground truth files (default depends on --mode).",
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


def normalize_answers(correct_answer, mode: str) -> list[str]:
    if isinstance(correct_answer, list):
        tokens = [str(x).strip().upper() for x in correct_answer if str(x).strip()]
    else:
        token = str(correct_answer).strip().upper()
        tokens = [token] if token else []

    if mode == "single":
        return tokens[:1]
    # multiple: deduplicate and keep deterministic order
    return sorted(set(tokens))


def main():
    args = parse_args()

    jsonl_path = Path(args.jsonl_path or MODE_TO_JSONL_PATH[args.mode])
    output_dir = Path(args.output_dir or MODE_TO_OUTPUT_DIR[args.mode])
    ground_truth_dir = Path(args.ground_truth_dir or MODE_TO_GROUND_TRUTH_DIR[args.mode])
    work_dir = args.work_dir
    structured_data_path = args.structured_data_path
    unstructured_data_path = args.unstructured_data_path
    prompt_template = PROMPT_TEMPLATE_SINGLE if args.mode == "single" else PROMPT_TEMPLATE_MULTIPLE

    output_dir.mkdir(parents=True, exist_ok=True)
    ground_truth_dir.mkdir(parents=True, exist_ok=True)
    items = load_items(jsonl_path)

    generated = 0
    generated_ground_truth = 0
    skipped = 0
    for item in items:
        task_id = item.get("task_id")
        query = item.get("query")
        db = item.get("db")
        options = item.get("options")
        correct_answer = item.get("correct_answer", [])

        if not task_id or not query or not db or not options:
            skipped += 1
            print(f"WARNING: missing required fields, skipping item: {task_id}")
            continue

        prompt = prompt_template.format(
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

        answers = normalize_answers(correct_answer, args.mode)
        if not answers:
            skipped += 1
            print(f"WARNING: missing/invalid correct_answer, skipping ground truth: {task_id}")
            continue

        gt_path = ground_truth_dir / task_id
        if args.mode == "single":
            gt_path.write_text(f"{answers[0]}\n", encoding="utf-8")
        else:
            gt_path.write_text(json.dumps(answers, ensure_ascii=False) + "\n", encoding="utf-8")
        generated_ground_truth += 1

    print(f"Mode: {args.mode}")
    print(f"Source jsonl: {jsonl_path}")
    print(f"Generated {generated} prompt files in {output_dir}")
    print(f"Generated {generated_ground_truth} ground truth files in {ground_truth_dir}")
    print(f"Skipped {skipped} items with missing fields.")
    print(f"Work dir used: {work_dir}")


if __name__ == "__main__":
    main()