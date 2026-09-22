from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/entcollab_env.sh"


def _write_fake_secrets(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                'ENTCOLLAB_AGENT_API_KEY="sk-test-agent-secret"',
                'ENTCOLLAB_AGENT_BASE_URL="https://models.example/v1"',
                'ENTCOLLAB_JUDGE_API_KEY="sk-test-judge-secret"',
                'ENTCOLLAB_JUDGE_BASE_URL="https://judge.example/v1"',
                'ENTCOLLAB_JUDGE_MODELS="judge-model"',
                'ENTCOLLAB_WEAK_MODEL="weak-model"',
                'ENTCOLLAB_STRONG_MODEL="strong-model"',
                'ENTCOLLAB_SUMMARY_MODEL="summary-model"',
                'ENTCOLLAB_TASK_TIMEOUT_SECONDS="111"',
                'ENTCOLLAB_AGENT_HTTP_TIMEOUT_SECONDS="222"',
                'ENTCOLLAB_JUDGE_TIMEOUT_SECONDS="333"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_entcollab_env_loads_profile_without_printing_keys(tmp_path: Path) -> None:
    secrets = tmp_path / "entcollab_models.env"
    compose_env = tmp_path / "compose.env"
    _write_fake_secrets(secrets)

    proc = subprocess.run(
        [
            "bash",
            "-lc",
            (
                f"ENTCOLLAB_SECRETS_FILE={secrets} "
                f"source {SCRIPT} strong --compose-env {compose_env}"
            ),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )

    assert "AGENT_LLM_MODEL=strong-model" in proc.stdout
    assert "JUDGE_MODELS=judge-model" in proc.stdout
    assert "sk-test-agent-secret" not in proc.stdout
    assert "sk-test-judge-secret" not in proc.stdout

    env_text = compose_env.read_text(encoding="utf-8")
    assert 'OPENAI_API_KEY="sk-test-agent-secret"' in env_text
    assert 'OPENAI_BASE_URL="https://models.example/v1"' in env_text
    assert 'AGENT_LLM_MODEL="strong-model"' in env_text
    assert 'JUDGE_MODELS="judge-model"' in env_text
    assert 'NO_PROXY="' in env_text


def test_entcollab_env_quiet_custom_model(tmp_path: Path) -> None:
    secrets = tmp_path / "entcollab_models.env"
    _write_fake_secrets(secrets)

    proc = subprocess.run(
        [
            "bash",
            "-lc",
            (
                f"ENTCOLLAB_SECRETS_FILE={secrets} "
                f"source {SCRIPT} custom-fast-model --quiet && "
                'printf "%s\\n" "${AGENT_LLM_MODEL}:${JUDGE_MODELS}"'
            ),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )

    assert proc.stdout.strip() == "custom-fast-model:judge-model"
