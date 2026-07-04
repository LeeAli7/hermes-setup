#!/bin/bash
set -e

HERMES="$HOME/hermes-agent/venv/bin/hermes"

declare -A PROVIDERS
PROVIDERS[opencode]="opencode-zen|http://127.0.0.1:9000/zen/v1"
PROVIDERS[kilo]="kilo|http://127.0.0.1:9001/api/openrouter"
PROVIDERS[kiro]="kiro|http://127.0.0.1:8080/v1"

OPencode_MODELS=(
  "deepseek-v4-flash-free"
  "north-mini-code-free"
  "nemotron-3-ultra-free"
  "mimo-v2.5-free"
)

KILO_MODELS=(
  "poolside/laguna-m.1:free"
  "poolside/laguna-xs.2:free"
  "stepfun/step-3.7-flash:free"
  "cohere/north-mini-code:free"
  "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
  "nvidia/nemotron-3-super-120b-a12b:free"
  "nvidia/nemotron-3-ultra-550b-a55b:free"
  "nvidia/nemotron-3.5-content-safety:free"
  "poolside/laguna-xs-2.1:free"
)

KIRO_MODELS=(
  "claude-sonnet-4"
  "claude-sonnet-4.5"
  "claude-haiku-4.5"
  "qwen3-coder-next"
  "deepseek-3.2"
  "minimax-m2.5"
  "minimax-m2.1"
  "glm-5"
)

menu() {
  local title="$1"
  shift
  local items=("$@")
  echo "  $title" >&2
  for i in "${!items[@]}"; do
    echo "  $((i+1)). ${items[$i]}" >&2
  done
  echo -n "  > " >&2
  read choice
  echo "${items[$((choice-1))]}"
}

if [ $# -ge 2 ]; then
  PROVIDER="$1"
  MODEL="$2"
elif [ $# -eq 1 ]; then
  PROVIDER="$1"
  echo "--- Provider: $PROVIDER ---"
  case "$PROVIDER" in
    opencode) MODEL=$(menu "Select model:" "${OPencode_MODELS[@]}") ;;
    kilo)     MODEL=$(menu "Select model:" "${KILO_MODELS[@]}") ;;
    kiro)     MODEL=$(menu "Select model:" "${KIRO_MODELS[@]}") ;;
    *) echo "Unknown: $PROVIDER"; exit 1 ;;
  esac
else
  echo "--- Select provider ---"
  echo "  1. opencode  — opencode.ai через Tor"
  echo "  2. kilo      — api.kilo.ai через Tor"
  echo "  3. kiro      — Amazon Q Developer (без лимитов)"
  echo -n "  > "
  read choice
  case "$choice" in
    1) PROVIDER="opencode"; MODEL=$(menu "Select model:" "${OPencode_MODELS[@]}") ;;
    2) PROVIDER="kilo";     MODEL=$(menu "Select model:" "${KILO_MODELS[@]}") ;;
    3) PROVIDER="kiro";     MODEL=$(menu "Select model:" "${KIRO_MODELS[@]}") ;;
    *) echo "Invalid"; exit 1 ;;
  esac
fi

IFS='|' read -r PROVIDER_NAME BASE_URL <<< "${PROVIDERS[$PROVIDER]}"
if [ -z "$PROVIDER_NAME" ]; then
  echo "Unknown provider: $PROVIDER"
  exit 1
fi

$HERMES config set model.provider "$PROVIDER_NAME"
$HERMES config set model.default "$MODEL"
$HERMES config set model.base_url "$BASE_URL"

echo "---"
echo "Switched to $PROVIDER_NAME / $MODEL"
