from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bridge_jiuwen" / "src"))

from bootstrap import bootstrap_repo_paths

bootstrap_repo_paths()

import fastapi  
import httpx  
import uvicorn  
from contexthub_sdk import ContextHubClient as _ContextHubClient  # noqa: F401

SIDECAR = ROOT / "bridge_jiuwen" / "src" / "sidecar.py"
DEMO_E2E = ROOT / "scripts" / "demo_e2e.py"
VENV_PYTHON = Path(sys.executable).resolve()

BASE_URL = os.getenv("CONTEXTHUB_URL", "http://127.0.0.1:8000")
API_KEY = os.getenv("CONTEXTHUB_API_KEY", "changeme")
ACCOUNT_ID = os.getenv("CONTEXTHUB_ACCOUNT_ID", "acme")


def _clean_env(agent_id: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(key, None)
    env["CONTEXTHUB_URL"] = BASE_URL
    env["CONTEXTHUB_API_KEY"] = API_KEY
    env["CONTEXTHUB_ACCOUNT_ID"] = ACCOUNT_ID
    env["CONTEXTHUB_AGENT_ID"] = agent_id
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _choose_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str, timeout: float = 10.0) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _post(url: str, body: dict, timeout: float = 20.0) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _wait_health(url: str, deadline_s: float = 15.0) -> None:
    end = time.time() + deadline_s
    last_error: Exception | None = None
    while time.time() < end:
        try:
            payload = _get(url)
            if payload.get("status") == "ok":
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"Health check failed for {url}: {last_error}")


def _run_demo_seed() -> None:
    proc = subprocess.run(
        [str(VENV_PYTHON), str(DEMO_E2E)],
        cwd=str(ROOT),
        env=_clean_env("query-agent"),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if "already exists" in combined and "sql-generator" in combined:
            print("[INFO] Seed data already exists; continuing without reseeding")
            return
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise RuntimeError("scripts/demo_e2e.py failed")
    print("[INFO] Seeded skill/subscription data via scripts/demo_e2e.py")


def _print_json(label: str, payload: dict | str | None) -> None:
    print(f"[DATA] {label}:")
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    query_port = _choose_free_port()
    analysis_port = _choose_free_port()
    while analysis_port == query_port:
        analysis_port = _choose_free_port()

    health = _get(f"{BASE_URL}/health")
    _require(health.get("status") == "ok", f"ContextHub health check failed: {health}")
    print(f"[INFO] ContextHub healthy at {BASE_URL}")

    subprocess.run(["pkill", "-f", "bridge_jiuwen/src/sidecar.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print("[INFO] Stopped existing Jiuwen sidecar processes")
    _run_demo_seed()

    temp_dir = Path(tempfile.gettempdir())
    query_log = temp_dir / f"jiuwen_verify_query_{query_port}.log"
    analysis_log = temp_dir / f"jiuwen_verify_analysis_{analysis_port}.log"
    with query_log.open("w") as query_out, analysis_log.open("w") as analysis_out:
        query_proc = subprocess.Popen(
            [
                str(VENV_PYTHON),
                str(SIDECAR),
                "--port",
                str(query_port),
                "--contexthub-url",
                BASE_URL,
                "--api-key",
                API_KEY,
                "--account-id",
                ACCOUNT_ID,
                "--agent-id",
                "query-agent",
            ],
            cwd=str(ROOT),
            env=_clean_env("query-agent"),
            stdout=query_out,
            stderr=subprocess.STDOUT,
        )
        analysis_proc = subprocess.Popen(
            [
                str(VENV_PYTHON),
                str(SIDECAR),
                "--port",
                str(analysis_port),
                "--contexthub-url",
                BASE_URL,
                "--api-key",
                API_KEY,
                "--account-id",
                ACCOUNT_ID,
                "--agent-id",
                "analysis-agent",
            ],
            cwd=str(ROOT),
            env=_clean_env("analysis-agent"),
            stdout=analysis_out,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_health(f"http://127.0.0.1:{query_port}/health")
            _wait_health(f"http://127.0.0.1:{analysis_port}/health")
            print(f"[INFO] Query sidecar log: {query_log}")
            print(f"[INFO] Analysis sidecar log: {analysis_log}")

            step1 = _post(
                f"http://127.0.0.1:{query_port}/dispatch",
                {
                    "name": "contexthub_store",
                    "args": {
                        "content": "月度销售额查询要 JOIN orders 和 products 并按月份聚合",
                        "tags": ["sql", "monthly-sales"],
                    },
                },
            )
            step1_result = json.loads(step1["result"])
            memory_uri = step1_result.get("uri")
            _require(memory_uri, f"Step 1 failed: {step1_result}")
            print(f"[PASS] Step 1: stored memory {memory_uri}")
            _print_json("Step 1 response", step1_result)

            step2 = _post(
                f"http://127.0.0.1:{query_port}/dispatch",
                {
                    "name": "contexthub_promote",
                    "args": {"uri": memory_uri, "target_team": "engineering"},
                },
            )
            step2_result = json.loads(step2["result"])
            team_uri = step2_result.get("uri")
            _require(team_uri and "ctx://team/engineering/" in team_uri, f"Step 2 failed: {step2_result}")
            print(f"[PASS] Step 2: promoted to {team_uri}")
            _print_json("Step 2 response", step2_result)

            step3 = _post(
                f"http://127.0.0.1:{analysis_port}/assemble",
                {
                    "sessionId": "verify-001",
                    "messages": [{"role": "user", "content": "月度销售额应该怎么查？"}],
                    "tokenBudget": 1024,
                },
            )
            addition = step3.get("systemPromptAddition")
            _require(addition and "JOIN orders" in addition, f"Step 3 failed: {step3}")
            print("[PASS] Step 3: assemble returned a systemPromptAddition that answers the natural question")
            _print_json("Step 3 response", step3)

            step4_publish = _post(
                f"http://127.0.0.1:{query_port}/dispatch",
                {
                    "name": "contexthub_skill_publish",
                    "args": {
                        "skill_uri": "ctx://team/engineering/skills/sql-generator",
                        "content": "v3: Runtime-verified SQL generator with CTE support",
                        "changelog": "Breaking: new output format",
                        "is_breaking": True,
                    },
                },
            )
            publish_result = json.loads(step4_publish["result"])
            _require(isinstance(publish_result.get("version"), int), f"Step 4 publish failed: {publish_result}")
            _print_json("Step 4 publish response", publish_result)

            step4_read = _post(
                f"http://127.0.0.1:{analysis_port}/dispatch",
                {"name": "read", "args": {"uri": "ctx://team/engineering/skills/sql-generator"}},
            )
            read_result = json.loads(step4_read["result"])
            advisory = str(read_result.get("advisory", ""))
            _require(read_result.get("version") == 1 and "pinned to v1" in advisory and "available" in advisory, f"Step 4 read failed: {read_result}")
            print(f"[PASS] Step 4: publish + read returned pinned v1 with advisory: {advisory}")
            _print_json("Step 4 read response", read_result)

            print("\nAll 4 Jiuwen MVP HTTP verification steps passed.")
        finally:
            query_proc.terminate()
            analysis_proc.terminate()
            try:
                query_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                query_proc.kill()
            try:
                analysis_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                analysis_proc.kill()


if __name__ == "__main__":
    main()
