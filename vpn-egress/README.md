# VPN-only egress for Hermes

Portable VPNGate egress for a second Linux device. No Oracle/static IPv6, no
direct host IP, no Tor — only VPNGate OpenVPN tunnels in isolated netns.

```
Hermes -> forwarder.py :9000 -> egress pool :9200
                                      ├─ vpngate  -> relay :9120 -> netns vgprobe  -> OpenVPN -> Zen
                                      └─ vpngate2 -> relay :9121 -> netns vgprobe1 -> OpenVPN -> Zen
```

Two tunnels are kept connected: one serves, the other is a hot standby. On a
429/timeout the pool leases the next slot immediately; the burned slot is
rotated in the background.

## Quick start

Run as the Hermes user (not root):

```bash
cd vpn-egress
./install.sh --yes
hermes-egress status
hermes-egress doctor
```

The installer needs `sudo` for the netns/OpenVPN system unit and sets up:

- `/etc/hermes-egress/egress.env` and `sources.json`;
- `hermes-vpngate-tunnel@.service` (system, root, netns);
- user units for pool, relays, recovery timers and watchdog;
- a forwarder drop-in that enables the pool and forbids Tor/direct fallback.

Options:

```bash
./install.sh --slots 3      # 3 hot VPNGate slots
./install.sh --iface eth0   # override physical NIC
./install.sh --no-start     # install only
./install.sh --force        # rewrite egress.env
./uninstall.sh              # remove units/config
```

## Verify

```bash
hermes-egress status
hermes-egress doctor
curl -sS --proxy http://10.200.0.2:8897 https://api.ipify.org; echo
curl -sS http://127.0.0.1:9120/zen/v1/models -H 'Authorization: Bearer public' -o /dev/null -w '%{http_code}\n'
```

A real chat request through `127.0.0.1:9000` is the final test and consumes
one free-Zen request on that exit.

## Files and paths

| What | Where |
|---|---|
| Code | `$HOME/.hermes/ippool/` |
| Config | `/etc/hermes-egress/egress.env` |
| Pool sources | `/etc/hermes-egress/sources.json` |
| System unit | `/etc/systemd/system/hermes-vpngate-tunnel@.service` |
| User units | `$HOME/.config/systemd/user/` |
| CLI | `/usr/local/bin/hermes-egress` |
| Logs | `$HOME/.hermes/ippool/*.log` |

## Important limits

- VPNGate exits are shared and often already burned. Two slots reduce, but do
  not eliminate, 429s.
- Recovery checks tunnel liveness only; it does not spend chat quota. The
  first real agent request is the quota test.
- The pool and relays bind to `127.0.0.1`; CONNECT proxies bind only to their
  veth addresses.
- Full specification, failure modes and acceptance criteria: [TZ.md](TZ.md).
