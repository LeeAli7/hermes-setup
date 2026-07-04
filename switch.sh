#!/bin/sh
set -e

HERMES="$HOME/hermes-agent/venv/bin/hermes"

opencode_models="deepseek-v4-flash-free north-mini-code-free nemotron-3-ultra-free mimo-v2.5-free"
kilo_models="poolside/laguna-m.1:free poolside/laguna-xs.2:free stepfun/step-3.7-flash:free cohere/north-mini-code:free nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free nvidia/nemotron-3-super-120b-a12b:free nvidia/nemotron-3-ultra-550b-a55b:free nvidia/nemotron-3.5-content-safety:free poolside/laguna-xs-2.1:free"
kiro_models="claude-sonnet-4 claude-sonnet-4.5 claude-haiku-4.5 qwen3-coder-next deepseek-3.2 minimax-m2.5 minimax-m2.1 glm-5"

menu() {
  title="$1"
  items="$2"
  echo "  $title" >&2
  i=1
  for m in $items; do
    echo "  $i. $m" >&2
    i=$((i+1))
  done
  echo -n "  > " >&2
  read choice
  i=1
  for m in $items; do
    if [ "$i" = "$choice" ]; then
      echo "$m"
      return
    fi
    i=$((i+1))
  done
  echo "" >&2
  echo "Invalid choice" >&2
  exit 1
}

if [ $# -ge 2 ]; then
  PROVIDER="$1"
  MODEL="$2"
elif [ $# -eq 1 ]; then
  PROVIDER="$1"
  echo "--- Provider: $PROVIDER ---" >&2
  case "$PROVIDER" in
    opencode) MODEL=$(menu "Select model:" "$opencode_models") ;;
    kilo)     MODEL=$(menu "Select model:" "$kilo_models") ;;
    kiro)     MODEL=$(menu "Select model:" "$kiro_models") ;;
    *) echo "Unknown: $PROVIDER (use: opencode | kilo | kiro)"; exit 1 ;;
  esac
else
  echo "--- Select provider ---" >&2
  echo "  1. opencode  — opencode.ai через Tor" >&2
  echo "  2. kilo      — api.kilo.ai через Tor" >&2
  echo "  3. kiro      — Amazon Q Developer (без Tor)" >&2
  echo -n "  > " >&2
  read choice
  case "$choice" in
    1) PROVIDER="opencode"; MODEL=$(menu "Select model:" "$opencode_models") ;;
    2) PROVIDER="kilo";     MODEL=$(menu "Select model:" "$kilo_models") ;;
    3) PROVIDER="kiro";     MODEL=$(menu "Select model:" "$kiro_models") ;;
    *) echo "Invalid"; exit 1 ;;
  esac
fi

case "$PROVIDER" in
  opencode) PROVIDER_NAME="opencode-zen"; BASE_URL="http://127.0.0.1:9000/zen/v1" ;;
  kilo)     PROVIDER_NAME="kilo";         BASE_URL="http://127.0.0.1:9001/api/openrouter" ;;
  kiro)     PROVIDER_NAME="kiro";         BASE_URL="http://127.0.0.1:8080/v1" ;;
  *) echo "Unknown provider: $PROVIDER"; exit 1 ;;
esac

$HERMES config set model.provider "$PROVIDER_NAME"
$HERMES config set model.default "$MODEL"
$HERMES config set model.base_url "$BASE_URL"

echo "---"
echo "Switched to $PROVIDER_NAME / $MODEL"
