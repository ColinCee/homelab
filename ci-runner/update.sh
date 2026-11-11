#!/usr/bin/env bash
# Bring the Beelink's CI to one commit of homelab's main:
#   sudo ci-runner/update.sh --follow     (its timer, every minute: takes main)
#   sudo ci-runner/update.sh <commit>     (by hand)
# Code comes from git itself, never a tarball: the commit must be on main and not older than the one installed. A
# changed image is rebuilt.
# Runs as its own unit (homelab-ci-update): installing restarts the services that would otherwise run it.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
repo=https://github.com/ColinCee/homelab.git
src=/var/lib/homelab-ci/src
images=/var/lib/homelab-ci/images
installed=/etc/homelab-ci/commit
failed=/var/lib/homelab-ci/update-failed
pinned=/etc/homelab-ci/pinned
python=(/usr/bin/python3 -I /usr/local/lib/homelab-ci/ci_supervisor.py)

want=${1:?usage: update.sh <commit> [reason] | --follow}
# One update at a time: a named commit waits its turn; the timer's catch-up gives way.
exec 7>/run/homelab-ci-update.lock
if [[ $want == --follow ]]; then
  flock -n 7 || { echo "another update is running"; exit 0; }
else
  flock -w 900 7 || { echo "another update has run for 15 minutes" >&2; exit 1; }
fi
if [[ $want == --follow && -e $pinned ]]; then
  echo "rolled back by hand to $(cat "$pinned"); sudo rm $pinned to follow main again"
  exit 0
elif [[ $want == --follow ]]; then
  want=$(timeout 30 git ls-remote "$repo" refs/heads/main | cut -f1) || true
  [[ -n $want ]] || { echo "GitHub did not answer"; exit 0; }
fi
[[ $want =~ ^[0-9a-f]{40}$ ]] || { echo "not a commit: $want" >&2; exit 2; }
have=$(cat "$installed" 2>/dev/null || true)
# A change that fails twice (its install or its image) is tried again only an hour after its last failure.
gave_up() {
  awk -v c="$1" -v now="$(date +%s)" '$1 == c && $2 >= 2 && now - $3 < 3600 {found = 1} END {exit !found}'     "$failed" 2>/dev/null
}
failed_once() {
  local n
  n=$(awk -v c="$1" '$1 == c {print $2}' "$failed" 2>/dev/null || true)
  echo "$1 $((${n:-0} + 1)) $(date +%s)" > "$failed"
}

if [[ $want != "$have" ]]; then
  [[ -d $src/.git ]] || git clone -q --no-checkout "$repo" "$src"
  git -C "$src" fetch -q origin main
  git -C "$src" cat-file -e "$want^{commit}" 2>/dev/null || git -C "$src" fetch -q origin "$want"
  git -C "$src" merge-base --is-ancestor "$want" origin/main || { echo "$want is not on main" >&2; exit 2; }
  if [[ -n $have ]] && ! git -C "$src" merge-base --is-ancestor "$have" "$want"; then
    # Main moved back past the installed commit (a force push): the timer waits rather than failing every minute.
    [[ $1 != --follow ]] || { echo "main is at $want, older than the installed $have"; exit 0; }
    echo "$want is older than the installed $have: roll back by hand with ROLLBACK=1" >&2
    [[ ${ROLLBACK:-} == 1 ]] || exit 2
    # Held there until the pin is removed, or the timer would take main again within a minute.
    echo "$want" > "$pinned"
  fi
  git -C "$src" checkout -q --detach --force "$want"
  # Main moves for many reasons: only a change to what install-host.sh installs is installed.
  paths=(ci-runner scripts)
  if [[ -z $have ]] || ! git -C "$src" diff --quiet "$have" "$want" -- "${paths[@]}"; then
    change=$(git -C "$src" log -1 --format=%H "$want" -- "${paths[@]}")
    if gave_up "$change"; then echo "$change failed twice; tried again an hour after"; exit 0; fi
    # The pull requests it ships, for the Control room's restart line. It reaches a shell: plain characters only.
    reason=${2:-${REASON:-homelab $(git -C "$src" log --format=%s "${have:+$have..}$want" -- "${paths[@]}" |
      grep -o '(#[0-9]*)' | tr -d '()' | paste -sd ' ' - | cut -d ' ' -f 1-5 || true)}}
    reason=${reason//[^[:alnum:] #:._,()\/-]/}
    REASON="$reason" "$src/ci-runner/install-host.sh" || { failed_once "$change"; exit 1; }
  fi
fi

inputs=(ci-runner/build-rootfs.sh ci-runner/rootfs ci-runner/versions.env)
# Rebuilt only when what goes into it changed since the commit it was built from; below the jobs on the disk.
built=$(cat "$images/commit" 2>/dev/null || true)
if [[ -z $built ]] || ! git -C "$src" diff --quiet "$built" "$want" -- "${inputs[@]}"; then
  change=$(git -C "$src" log -1 --format=%H "$want" -- "${inputs[@]}")
  if gave_up "$change"; then
    echo "the image for $change failed twice; jobs keep the old one for now"
  elif nice -n 10 ionice -c3 "$src/ci-runner/build-rootfs.sh"; then
    echo "$want" > "$images/commit"
  else
    failed_once "$change"
    exit 1
  fi
fi

# Last: a host whose update stopped half way says it still runs the old commit, and tries again.
echo "$want" > "$installed.new"
mv "$installed.new" "$installed"
echo "on $want, image built at $(cat "$images/commit" 2>/dev/null || echo none)"
# Each slot count is tested once under full load, in its own unit (homelab-ci-load-test).
"${python[@]}" start-load-test-if-due || echo "could not start the load test; next pass"
