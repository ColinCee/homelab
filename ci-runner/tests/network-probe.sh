#!/bin/bash
# Runs inside a CI microVM. Prints one PASS or FAIL line per check; the host
# side (test-network.sh) counts them. Internet must work; nothing private may.
# Placeholders filled by test-network.sh: @HOST_LAN@ @TAILNET@.

total=0
# expect ok|blocked NAME COMMAND...
expect() {
  local want=$1 name=$2 got=blocked
  shift 2
  total=$((total + 1))
  if "$@"; then got=ok; fi
  if [[ $got == "$want" ]]; then echo "PASS $want $name"; else echo "FAIL $want $name (got $got)"; fi
}
reach() { [[ $(curl -s -o /dev/null -m 5 -w '%{http_code}' "$@") == 200 ]]; }
# A dropped packet times out. "Connection refused" means the target answered,
# so only a timeout counts as blocked.
tcp() {
  timeout 4 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null
  [[ $? -ne 124 ]]
}
pings() { ping -c1 -W2 "$1" >/dev/null 2>&1; }

# Must work
expect ok "github api" reach https://api.github.com/zen
expect ok pypi reach https://pypi.org/simple/pip/
expect ok npm reach https://registry.npmjs.org/-/ping
expect ok dns getent hosts github.com

# Must be blocked: Beelink, the LAN, the tailnet, Docker
for target in \
  10.200.0.1:22 10.200.0.1:53 @HOST_LAN@:22 @HOST_LAN@:8123 \
  192.168.1.1:80 192.168.1.1:53 @TAILNET@:22 @TAILNET@:3001 \
  @TAILNET@:8123 100.100.100.100:53 172.17.0.1:80 172.21.0.1:3000 \
  172.22.0.1:9090 172.23.0.1:3100; do
  expect blocked "$target" tcp "${target%:*}" "${target#*:}"
done
expect blocked "ping LAN router" pings 192.168.1.1
expect blocked "ping host" pings 10.200.0.1

# The home's public address: a router forwarding or looping back ports would
# reach the LAN through it.
wan=$(curl -s -m 5 https://api.ipify.org)
for port in 22 80 443 8080 8443; do
  expect blocked "public address :$port" tcp "$wan" "$port"
done

# Posing as another address: the other slot's, and a public one.
ip addr add 10.200.1.2/32 dev eth0
ip addr add 203.0.113.9/32 dev eth0
expect blocked "spoof other slot" reach --interface 10.200.1.2 https://api.github.com/zen
expect blocked "spoof public address" reach --interface 203.0.113.9 https://api.github.com/zen
ip addr del 10.200.1.2/32 dev eth0
ip addr del 203.0.113.9/32 dev eth0

echo "TOTAL $total"

# Inbound: listen so the host side can try to connect from a container.
echo "LISTENING"
if timeout 15 nc -l -p 8080 >/dev/null 2>&1; then echo "INBOUND_CONNECTED"; else echo "INBOUND_NONE"; fi
