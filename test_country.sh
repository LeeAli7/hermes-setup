#!/bin/bash
# Test opencode.ai API through Tor exits from a specific country
# Usage: test_country.sh <CC> [countries...]
set -u

TORRC="/home/ali/projects/hermes/tor/torrc"
STATE="/home/ali/projects/hermes/proxy_state.json"

restart_tor() {
  systemctl --user restart hermes-tor.service
  sleep 15
  # wait for bootstrap
  for i in $(seq 1 12); do
    grep -q "Bootstrapped 100%" /home/ali/projects/hermes/tor/tor.log 2>/dev/null && break
    sleep 5
  done
}

get_exit_info() {
  curl -s --max-time 20 -x socks5h://127.0.0.1:9050 "http://ip-api.com/json/?fields=status,query,country,countryCode" 2>/dev/null
}

test_api() {
  curl -s -o /dev/null -w "%{http_code}" --max-time 90 -X POST http://127.0.0.1:9000/zen/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"deepseek-v4-flash-free","messages":[{"role":"user","content":"hi"}],"max_tokens":5,"stream":false}'
}

for cc in "$@"; do
  echo "=== Testing country: $cc ==="
  # save old config
  sed -i "s/^ExitNodes .*/ExitNodes {$cc}/" "$TORRC"
  restart_tor
  info=$(get_exit_info)
  echo "  exit info: $info"
  ip=$(echo "$info" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('query',''))" 2>/dev/null)
  code=$(test_api)
  echo "  API result: HTTP=$code (exit IP: $ip)"
  if [ "$code" = "200" ]; then
    echo "  >>> COUNTRY $cc WORKS! IP=$ip"
    # restore broad config
    sed -i "s/^ExitNodes .*/ExitNodes {de},{fr},{us},{nl},{gb},{ro},{ch},{se},{pl},{at},{es},{it},{ca},{no},{fi},{be},{cz}/" "$TORRC"
    exit 0
  fi
done

echo "NONE WORKED"
# restore broad config
sed -i "s/^ExitNodes .*/ExitNodes {de},{fr},{us},{nl},{gb},{ro},{ch},{se},{pl},{at},{es},{it},{ca},{no},{fi},{be},{cz}/" "$TORRC"
exit 1
