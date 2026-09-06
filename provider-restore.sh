#!/bin/sh
# Boot-time restore: wake ONLY the last selected AI provider.
# State file is written by switch.sh on every successful switch.
STATE="$HOME/.config/hermes-switch.state"

if [ ! -f "$STATE" ]; then
  echo "provider-restore: no state file — nothing to restore (all providers asleep)"
  exit 0
fi

PROVIDER=""
MODEL=""
read -r PROVIDER MODEL < "$STATE"
if [ -z "$PROVIDER" ]; then
  echo "provider-restore: empty state file — nothing to restore"
  exit 0
fi

echo "provider-restore: waking '$PROVIDER' ($MODEL)"
exec "$HOME/projects/hermes/switch.sh" "$PROVIDER" "$MODEL"
