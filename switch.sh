#!/bin/bash
set -e

HERMES="$HOME/hermes-agent/venv/bin/hermes"

if [ $# -ge 2 ]; then
    PROVIDER="$1"
    MODEL="$2"
elif [ $# -eq 1 ]; then
    PROVIDER="$1"
    read -p "Model: " MODEL
else
    read -p "Provider (kilo / opencode): " PROVIDER
    read -p "Model: " MODEL
fi

case "$PROVIDER" in
    kilo)
        BASE_URL="http://127.0.0.1:9001/api/openrouter"
        ;;
    opencode|opencode-zen)
        BASE_URL="http://127.0.0.1:9000/zen/v1"
        PROVIDER="opencode-zen"
        ;;
    kiro)
        BASE_URL="http://127.0.0.1:8080/v1"
        ;;
    *)
        echo "Unknown provider: $PROVIDER"
        echo "Usage: $0 [kilo|opencode|kiro] [model]"
        exit 1
        ;;
esac

$HERMES config set model.provider "$PROVIDER"
$HERMES config set model.default "$MODEL"
$HERMES config set model.base_url "$BASE_URL"

echo "---"
echo "Switched to $PROVIDER / $MODEL"
