#!/usr/bin/env bash
# Remove the VPN-only egress pack.  Keeps Hermes, the forwarder and its config.
set -euo pipefail

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1
if [[ "$EUID" -eq 0 ]]; then
  echo "Run as the Hermes user, not root (it calls sudo)." >&2
  exit 1
fi
ETC_DIR="/etc/hermes-egress"
INSTALL_DIR="${HERMES_EGRESS_HOME:-$HOME/.hermes/ippool}"
SLOTS="${VPNGATE_SLOTS:-2}"

for ((i = 0; i < SLOTS; i++)); do
  systemctl --user disable --now "hermes-vpngate-recovery@$i.timer" 2>/dev/null || true
  systemctl --user disable --now "hermes-egress-vpngate-relay@$i.service" 2>/dev/null || true
  sudo systemctl disable --now "hermes-vpngate-tunnel@$i.service" 2>/dev/null || true
done
systemctl --user disable --now hermes-egress-watchdog.timer 2>/dev/null || true
systemctl --user disable --now hermes-egress-pool.service 2>/dev/null || true

sudo rm -f /etc/systemd/system/hermes-vpngate-tunnel@.service
sudo systemctl daemon-reload
rm -f "$HOME/.config/systemd/user"/hermes-egress-*.service \
      "$HOME/.config/systemd/user"/hermes-egress-*.timer \
      "$HOME/.config/systemd/user"/hermes-vpngate-recovery@.*
rm -f "$HOME/.config/systemd/user/hermes-opencode-forwarder.service.d/egress-vpn.conf"
systemctl --user daemon-reload
systemctl --user restart hermes-opencode-forwarder.service 2>/dev/null || true

sudo rm -f /usr/local/bin/hermes-egress /usr/local/bin/hermes-egress-run
sudo rm -rf /usr/local/share/hermes-egress "$ETC_DIR"

if [[ "$PURGE" == "1" ]]; then
  read -r -p "Delete $INSTALL_DIR (VPN configs, logs, state)? [y/N] " ans
  if [[ "${ans:-}" == "y" || "${ans:-}" == "Y" ]]; then
    rm -rf "$INSTALL_DIR"
  fi
fi
echo "Removed. The forwarder drop-in is gone; restart Hermes if it was running."
