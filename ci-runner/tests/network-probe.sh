#!/bin/bash
# Runs inside a CI microVM. Prints one PASS or FAIL line per check; the host
# side (test-network.sh) counts them. Internet must work; nothing private may.
# Placeholders filled by test-network.sh: @HOST_LAN@ @TAILNET@ @OTHER_SLOT@.

# expect ok|blocked NAME COMMAND...
expect() {
  local want=$1 name=$2 got=blocked
  shift 2
  if "$@"; then got=ok; fi
  if [[ $got == "$want" ]]; then echo "PASS $want $name"; else echo "FAIL $want $name (got $got)"; fi
}
reach() { [[ $(curl -s -o /dev/null -m 5 -w '%{http_code}' "$1") == 200 ]]; }
tcp() { timeout 4 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }
pings() { ping -c1 -W2 "$1" >/dev/null 2>&1; }

# Must work
expect ok "github api" reach https://api.github.com/zen
expect ok pypi reach https://pypi.org/simple/pip/
expect ok npm reach https://registry.npmjs.org/-/ping
expect ok dns getent hosts github.com

# Must be blocked: Beelink, the LAN, the tailnet, Docker, the other slot
for target in \
  10.200.0.1:22 10.200.0.1:53 @HOST_LAN@:22 @HOST_LAN@:8123 \
  192.168.1.1:80 192.168.1.1:53 @TAILNET@:22 @TAILNET@:3001 \
  @TAILNET@:8123 100.100.100.100:53 172.17.0.1:80 172.21.0.1:3000 \
  172.22.0.1:9090 172.23.0.1:3100 @OTHER_SLOT@:22; do
  expect blocked "$target" tcp "${target%:*}" "${target#*:}"
done
expect blocked "ping LAN router" pings 192.168.1.1
expect blocked "ping host" pings 10.200.0.1

# Inbound: listen so the host side can try to connect from a container.
echo "LISTENING"
if timeout 15 nc -l -p 8080 >/dev/null 2>&1; then echo "INBOUND_CONNECTED"; else echo "INBOUND_NONE"; fi
