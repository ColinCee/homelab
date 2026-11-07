#!/usr/bin/env bash
# Update both CI hosts to homelab's main at once, from Colin's PC (where agents run; it reaches both):
#   REASON="homelab#<number>: <why>" ci-runner/update-hosts.sh
# Each host fetches and checks the commit itself (update.sh); this only names it. The PC then takes the Beelink's
# job image, rebuilt by then if its files changed. A PC that is off catches up on its own timer.
set -euo pipefail
# The reason reaches a shell on each host: plain characters only.
reason=${REASON:?say why: REASON="homelab#<number>: <why>"}
reason=${reason//[^[:alnum:] #:._,()\/-]/}
commit=$(git ls-remote https://github.com/ColinCee/homelab.git refs/heads/main | cut -f1)
[[ $commit =~ ^[0-9a-f]{40}$ ]] || { echo "could not read main's commit" >&2; exit 1; }
guest=(ssh -i D:/kaizen-ci/ssh/id_ed25519 -o UserKnownHostsFile=D:/kaizen-ci/ssh/known_hosts -o ConnectTimeout=10
  kaizen@192.168.250.10)

# update.sh as a unit of its own, so a dropped connection cannot cut it off; its log follows when it ends.
update() {
  local unit
  unit="homelab-ci-update-$(date +%s%N)"
  echo "sudo systemd-run --unit=$unit --wait --collect --quiet /usr/local/lib/homelab-ci/update.sh $*; s=\$?;" \
    "sudo journalctl -u $unit -o cat --no-pager; exit \$s"
}

echo "updating both hosts to $commit"
# shellcheck disable=SC2029 # built here on purpose: the commit and reason are checked above
ssh colin@beelink "$(update "$commit" "'$reason'")" 2>&1 | sed 's/^/beelink: /' &
beelink=$!
pc=0
if "${guest[@]}" true 2>/dev/null; then
  "${guest[@]}" "$(update "$commit" "'$reason'")" 2>&1 | sed 's/^/pc: /' || pc=1
else
  echo "pc: off; it catches up on its own timer"
fi
wait "$beelink" || { echo "beelink: update failed" >&2; exit 1; }
# The Beelink's image is final now (rebuilt if its files changed): the PC takes it.
if [[ $pc == 0 ]] && "${guest[@]}" true 2>/dev/null; then
  "${guest[@]}" "$(update --follow)" 2>&1 | sed 's/^/pc: /' || pc=1
fi
exit "$pc"
