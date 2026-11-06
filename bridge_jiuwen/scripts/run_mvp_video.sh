#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/demo_common.sh"

clear_proxy_env
require_services

# Cleaning up memory
"${SCRIPT_DIR}/clear_demo_cache.sh"

# D1-D4
switch_agent "query-agent"
run_phase "query"

# D5-D9
switch_agent "analysis-agent"
run_phase "analysis"

# D10
switch_agent "query-agent"
run_phase "query-return"

echo "Sequential prompt-step demo completed."
