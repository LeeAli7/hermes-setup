#!/bin/bash
# provider-restore.sh — restores hermes provider state on boot
STATE_FILE="/home/ali/.config/hermes-switch.state"
CONFIG_FILE="/home/ali/projects/hermes/config.yaml"
PROVIDER_RESTORE_SERVICE="hermes-provider-restore.service"

if [ -f "$STATE_FILE" ]; then
    read -r provider model < "$STATE_FILE"
    if [ -n "$provider" ] && [ -n "$model" ]; then
        echo "[provider-restore] Restoring provider=$provider model=$model"
        # Update config.yaml
        sed -i "s/^  default: .*/  default: $model/" "$CONFIG_FILE"
        sed -i "s/^provider: .*/provider: $provider/" "$CONFIG_FILE"
        # Update switch state
        echo "$provider $model" > "$STATE_FILE"
        echo "[provider-restore] Done"
    fi
else
    echo "[provider-restore] No state file found"
fi
