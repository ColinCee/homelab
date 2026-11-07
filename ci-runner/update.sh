#!/usr/bin/env bash
# Bring this CI host (the Beelink, or Colin's PC guest with ROLE=pc) to one commit of homelab's main:
#   sudo ci-runner/update.sh <commit> [reason]      (update-hosts.sh runs it on both hosts at once)
#   sudo ci-runner/update.sh --follow               (the PC's timer: the commit and image the Beelink runs)
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
reason=${2:-${REASON:-update.sh}}
image_want=""
if [[ $want == --follow ]]; then
  [[ $role == pc ]] || { echo "--follow is for the PC" >&2; exit 2; }
  # Not while Colin games (0 slots): the image copy crosses Windows' network.
  [[ "$(cat /etc/homelab-ci/pc-slots 2>/dev/null)" != 0 ]] || { echo "paused; later"; exit 0; }
  read -r want image_want < <("${python[@]}" peer-version) || { echo "the Beelink did not answer"; exit 0; }
  [[ $want != - ]] || { echo "the Beelink names no commit yet"; exit 0; }
  [[ $image_want != - ]] || image_want=""
  reason="follow the Beelink"
fi
[[ $want =~ ^[0-9a-f]{40}$ ]] || { echo "not a commit: $want" >&2; exit 2; }
have=$(cat "$installed" 2>/dev/null || true)

if [[ $want != "$have" ]]; then
  # The same commit failing twice is not tried again until main moves on.
  if [[ "$(cat "$failed" 2>/dev/null)" == "$want 2" ]]; then echo "$want failed twice; skipped"; exit 0; fi
  [[ -d $src/.git ]] || git clone -q --no-checkout "$repo" "$src"
  git -C "$src" fetch -q origin main
  git -C "$src" cat-file -e "$want^{commit}" 2>/dev/null || git -C "$src" fetch -q origin "$want"
  git -C "$src" merge-base --is-ancestor "$want" origin/main || { echo "$want is not on main" >&2; exit 2; }
  if [[ -n $have ]] && ! git -C "$src" merge-base --is-ancestor "$have" "$want"; then
    echo "$want is older than the installed $have: roll back by hand with ROLLBACK=1" >&2
    [[ ${ROLLBACK:-} == 1 ]] || exit 2
  fi
  git -C "$src" checkout -q --detach --force "$want"
  if ! REASON="$reason" "$src/ci-runner/install-host.sh"; then
    n=$(awk -v w="$want" '$1 == w {print $2}' "$failed" 2>/dev/null || true)
    echo "$want $((${n:-0} + 1))" > "$failed"
    exit 1
  fi
  rm -f "$failed"
fi

inputs=(ci-runner/build-rootfs.sh ci-runner/rootfs ci-runner/versions.env)
if [[ $role == beelink ]]; then
  # Rebuilt only when what goes into it changed since the commit it was built from; below the jobs on the disk.
  built=$(cat "$images/commit" 2>/dev/null || true)
  if [[ -z $built ]] || ! git -C "$src" diff --quiet "$built" "$want" -- "${inputs[@]}"; then
    nice -n 10 ionice -c3 "$src/ci-runner/build-rootfs.sh"
    (cd "$images" && sha256sum rootfs.ext4 vmlinuz | sha256sum | cut -c1-64) > "$images/sha256.new"
    mv "$images/sha256.new" "$images/sha256"
    echo "$want" > "$images/commit"
  fi
elif [[ -n $image_want && $image_want != "$(cat "$images/sha256" 2>/dev/null || true)" ]]; then
  "${python[@]}" fetch-image "$image_want"
fi

# Last: a host whose update stopped half way says it still runs the old commit, and tries again.
echo "$want" > "$installed.new"
mv "$installed.new" "$installed"
echo "on $want (${role}), image $(cat "$images/sha256" 2>/dev/null || echo none)"
