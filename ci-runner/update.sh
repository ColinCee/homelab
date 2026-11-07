#!/usr/bin/env bash
# Bring this CI host (the Beelink, or Colin's PC guest with ROLE=pc) to one commit of homelab's main:
#   sudo ci-runner/update.sh --follow     (each host's timer, every minute: the Beelink takes main, the PC the commit
#                                          and image the Beelink runs, so it never runs code ahead of its image)
#   sudo ci-runner/update.sh <commit>     (by hand)
# Code comes from git itself, never a tarball: the commit must be on main and not older than the one installed. On
# the Beelink a changed image is rebuilt; the PC copies the Beelink's through the door, so both run the same image.
# Runs as its own unit (homelab-ci-update): installing restarts the services that would otherwise run it.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
repo=https://github.com/ColinCee/homelab.git
src=/var/lib/homelab-ci/src
images=/var/lib/homelab-ci/images
installed=/etc/homelab-ci/commit
failed=/var/lib/homelab-ci/update-failed
pinned=/etc/homelab-ci/pinned
role=$(cat /etc/homelab-ci/role 2>/dev/null || echo beelink)
python=(/usr/bin/python3 -I /usr/local/lib/homelab-ci/ci_supervisor.py)

want=${1:?usage: update.sh <commit> [reason] | --follow}
# One update at a time: a named commit waits its turn; the timer's catch-up gives way.
exec 7>/run/homelab-ci-update.lock
if [[ $want == --follow ]]; then
  flock -n 7 || { echo "another update is running"; exit 0; }
else
  flock -w 900 7 || { echo "another update has run for 15 minutes" >&2; exit 1; }
fi
image_want=""
if [[ $want == --follow && -e $pinned ]]; then
  echo "rolled back by hand to $(cat "$pinned"); sudo rm $pinned to follow main again"
  exit 0
elif [[ $want == --follow && $role == pc ]]; then
  # Not while Colin games (0 slots): the image copy crosses Windows' network.
  [[ "$(cat /etc/homelab-ci/pc-slots 2>/dev/null)" != 0 ]] || { echo "paused; later"; exit 0; }
  read -r want image_want < <("${python[@]}" peer-version) || { echo "the Beelink did not answer"; exit 0; }
  [[ $want != - ]] || { echo "the Beelink names no commit yet"; exit 0; }
  [[ $image_want != - ]] || image_want=""
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
    # The PC following a Beelink that was rolled back waits for it.
    [[ $1 != --follow ]] || { echo "the Beelink runs $want, older than this host's $have"; exit 0; }
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
if [[ $role == beelink ]]; then
  # Rebuilt only when what goes into it changed since the commit it was built from; below the jobs on the disk.
  built=$(cat "$images/commit" 2>/dev/null || true)
  if [[ -z $built ]] || ! git -C "$src" diff --quiet "$built" "$want" -- "${inputs[@]}"; then
    change=$(git -C "$src" log -1 --format=%H "$want" -- "${inputs[@]}")
    if gave_up "$change"; then
      echo "the image for $change failed twice; jobs keep the old one for now"
    elif nice -n 10 ionice -c3 "$src/ci-runner/build-rootfs.sh"; then
      (cd "$images" && sha256sum rootfs.ext4 vmlinuz | sha256sum | cut -c1-64) > "$images/sha256.new"
      mv "$images/sha256.new" "$images/sha256"
      echo "$want" > "$images/commit"
    else
      failed_once "$change"
      exit 1
    fi
  fi
elif [[ -n $image_want && $image_want != "$(cat "$images/sha256" 2>/dev/null || true)" ]]; then
  # A copy refused (busy, or the hourly limit) or cut off is tried again on the next pass.
  "${python[@]}" fetch-image "$image_want" || echo "the Beelink's image is not copied yet; next pass"
fi

# Last: a host whose update stopped half way says it still runs the old commit, and tries again.
echo "$want" > "$installed.new"
mv "$installed.new" "$installed"
echo "on $want (${role}), image $(cat "$images/sha256" 2>/dev/null || echo none)"
