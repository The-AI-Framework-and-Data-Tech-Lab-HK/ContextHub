#!/usr/bin/env bash
# Source this file before running an EntCollabBench pilot.
#
# Usage:
#   cp scripts/entcollab_models.env.example secrets/entcollab_models.env
#   $EDITOR secrets/entcollab_models.env
#   source scripts/entcollab_env.sh weak
#   source scripts/entcollab_env.sh strong
#   source scripts/entcollab_env.sh strong --compose-env secrets/entcollab.compose.strong.env

if [[ -n "${BASH_VERSION:-}" ]]; then
  _entcollab_script="${BASH_SOURCE[0]}"
  if [[ "${_entcollab_script}" == "${0}" ]]; then
    echo "This script must be sourced so exports affect your current shell:" >&2
    echo "  source scripts/entcollab_env.sh weak" >&2
    exit 2
  fi
elif [[ -n "${ZSH_VERSION:-}" ]]; then
  _entcollab_script="${(%):-%x}"
  if [[ ":${ZSH_EVAL_CONTEXT:-}:" != *":file:"* ]]; then
    echo "This script must be sourced so exports affect your current shell:" >&2
    echo "  source scripts/entcollab_env.sh weak" >&2
    exit 2
  fi
else
  _entcollab_script="${0}"
fi

_entcollab_profile="${1:-weak}"
shift || true
_entcollab_root="$(cd "$(dirname "${_entcollab_script}")/.." && pwd)"
_entcollab_secrets="${ENTCOLLAB_SECRETS_FILE:-${_entcollab_root}/secrets/entcollab_models.env}"
_entcollab_compose_env=""
_entcollab_quiet=0

_entcollab_dotenv_quote() {
  local _value="${1}"
  _value="${_value//\\/\\\\}"
  _value="${_value//\"/\\\"}"
  printf '"%s"' "${_value}"
}

while (( $# > 0 )); do
  case "${1}" in
    --compose-env)
      if [[ -z "${2:-}" ]]; then
        echo "--compose-env requires a path" >&2
        return 2
      fi
      _entcollab_compose_env="${2}"
      shift 2
      ;;
    --quiet)
      _entcollab_quiet=1
      shift
      ;;
    *)
      echo "Unknown entcollab_env option: ${1}" >&2
      return 2
      ;;
  esac
done

if [[ ! -f "${_entcollab_secrets}" ]]; then
  echo "Missing secrets file: ${_entcollab_secrets}" >&2
  echo "Create it with:" >&2
  echo "  mkdir -p ${_entcollab_root}/secrets" >&2
  echo "  cp ${_entcollab_root}/scripts/entcollab_models.env.example ${_entcollab_secrets}" >&2
  return 2
fi

# shellcheck disable=SC1090
source "${_entcollab_secrets}"

case "${_entcollab_profile}" in
  weak)
    _entcollab_agent_model="${ENTCOLLAB_WEAK_MODEL:-}"
    ;;
  strong)
    _entcollab_agent_model="${ENTCOLLAB_STRONG_MODEL:-}"
    ;;
  *)
    # Custom profile: pass the model id directly as the first argument.
    _entcollab_agent_model="${_entcollab_profile}"
    ;;
esac

_entcollab_missing=()
[[ -n "${ENTCOLLAB_AGENT_API_KEY:-}" ]] || _entcollab_missing+=("ENTCOLLAB_AGENT_API_KEY")
[[ -n "${ENTCOLLAB_AGENT_BASE_URL:-}" ]] || _entcollab_missing+=("ENTCOLLAB_AGENT_BASE_URL")
_entcollab_profile_upper="$(printf '%s' "${_entcollab_profile}" | tr '[:lower:]' '[:upper:]')"
[[ -n "${_entcollab_agent_model:-}" ]] || _entcollab_missing+=("ENTCOLLAB_${_entcollab_profile_upper}_MODEL or model argument")
[[ -n "${ENTCOLLAB_JUDGE_MODELS:-}" ]] || _entcollab_missing+=("ENTCOLLAB_JUDGE_MODELS")

if (( ${#_entcollab_missing[@]} > 0 )); then
  echo "Missing required EntCollabBench env values:" >&2
  printf '  - %s\n' "${_entcollab_missing[@]}" >&2
  return 2
fi

export OPENAI_API_KEY="${ENTCOLLAB_AGENT_API_KEY}"
export OPENAI_BASE_URL="${ENTCOLLAB_AGENT_BASE_URL}"
export AGENT_LLM_MODEL="${_entcollab_agent_model}"
export AGENT_SUMMARY_MODEL="${ENTCOLLAB_SUMMARY_MODEL:-${_entcollab_agent_model}}"

export JUDGE_OPENAI_API_KEY="${ENTCOLLAB_JUDGE_API_KEY:-${ENTCOLLAB_AGENT_API_KEY}}"
export JUDGE_OPENAI_BASE_URL="${ENTCOLLAB_JUDGE_BASE_URL:-${ENTCOLLAB_AGENT_BASE_URL}}"
export JUDGE_MODELS="${ENTCOLLAB_JUDGE_MODELS}"

export TASK_TIMEOUT_SECONDS="${ENTCOLLAB_TASK_TIMEOUT_SECONDS:-1000}"
export AGENT_HTTP_TIMEOUT_SECONDS="${ENTCOLLAB_AGENT_HTTP_TIMEOUT_SECONDS:-400}"
export JUDGE_TIMEOUT_SECONDS="${ENTCOLLAB_JUDGE_TIMEOUT_SECONDS:-500}"

# urllib-based MCP probes otherwise may route localhost traffic through a proxy.
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}127.0.0.1,localhost"
export no_proxy="${no_proxy:+${no_proxy},}127.0.0.1,localhost"

if [[ -n "${_entcollab_compose_env}" ]]; then
  mkdir -p "$(dirname "${_entcollab_compose_env}")"
  {
    printf 'OPENAI_API_KEY=%s\n' "$(_entcollab_dotenv_quote "${OPENAI_API_KEY}")"
    printf 'OPENAI_BASE_URL=%s\n' "$(_entcollab_dotenv_quote "${OPENAI_BASE_URL}")"
    printf 'AGENT_LLM_MODEL=%s\n' "$(_entcollab_dotenv_quote "${AGENT_LLM_MODEL}")"
    printf 'AGENT_SUMMARY_MODEL=%s\n' "$(_entcollab_dotenv_quote "${AGENT_SUMMARY_MODEL}")"
    printf 'JUDGE_OPENAI_API_KEY=%s\n' "$(_entcollab_dotenv_quote "${JUDGE_OPENAI_API_KEY}")"
    printf 'JUDGE_OPENAI_BASE_URL=%s\n' "$(_entcollab_dotenv_quote "${JUDGE_OPENAI_BASE_URL}")"
    printf 'JUDGE_MODELS=%s\n' "$(_entcollab_dotenv_quote "${JUDGE_MODELS}")"
    printf 'TASK_TIMEOUT_SECONDS=%s\n' "$(_entcollab_dotenv_quote "${TASK_TIMEOUT_SECONDS}")"
    printf 'AGENT_HTTP_TIMEOUT_SECONDS=%s\n' "$(_entcollab_dotenv_quote "${AGENT_HTTP_TIMEOUT_SECONDS}")"
    printf 'JUDGE_TIMEOUT_SECONDS=%s\n' "$(_entcollab_dotenv_quote "${JUDGE_TIMEOUT_SECONDS}")"
    printf 'NO_PROXY=%s\n' "$(_entcollab_dotenv_quote "${NO_PROXY}")"
    printf 'no_proxy=%s\n' "$(_entcollab_dotenv_quote "${no_proxy}")"
  } > "${_entcollab_compose_env}"
  chmod 600 "${_entcollab_compose_env}"
fi

if [[ "${_entcollab_quiet}" != "1" ]]; then
  echo "EntCollabBench env loaded:"
  echo "  profile=${_entcollab_profile}"
  echo "  AGENT_LLM_MODEL=${AGENT_LLM_MODEL}"
  echo "  AGENT_SUMMARY_MODEL=${AGENT_SUMMARY_MODEL}"
  echo "  OPENAI_BASE_URL=${OPENAI_BASE_URL}"
  echo "  JUDGE_MODELS=${JUDGE_MODELS}"
  echo "  JUDGE_OPENAI_BASE_URL=${JUDGE_OPENAI_BASE_URL}"
  echo "  TASK_TIMEOUT_SECONDS=${TASK_TIMEOUT_SECONDS}"
  echo "  AGENT_HTTP_TIMEOUT_SECONDS=${AGENT_HTTP_TIMEOUT_SECONDS}"
  echo "  JUDGE_TIMEOUT_SECONDS=${JUDGE_TIMEOUT_SECONDS}"
  if [[ -n "${_entcollab_compose_env}" ]]; then
    echo "  compose_env=${_entcollab_compose_env}"
  fi
fi

unset _entcollab_profile _entcollab_profile_upper _entcollab_root _entcollab_secrets _entcollab_agent_model _entcollab_missing _entcollab_script _entcollab_compose_env _entcollab_quiet
unset -f _entcollab_dotenv_quote
