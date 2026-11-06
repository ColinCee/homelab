#!/usr/bin/env bash
# Usage: pack.sh CACHE_DIR OUT [WORKSPACE_PATH...]
# Tars every readable top-level folder of CACHE_DIR (but lost+found) and the
# given workspace paths (relative to the current folder) into OUT, and fails
# rather than leave an archive the CI supervisor would refuse.
set -euo pipefail

cache_dir=$1 out=$2
shift 2

# scripts/ci_supervisor.py: CACHE_MAX_UNPACKED_BYTES.
max_unpacked=6442450944

# The cache disk holds the last cache's workspace paths at the same place (web/ for
# web/node_modules): pack the job's fresh ones, not both, or every cache would carry
# all earlier copies too.
tops=" "
for path in "$@"; do path=${path#./}; tops+="${path%%/*} "; done

members=() folders=()
for path in "$cache_dir"/*/; do
  name=$(basename "$path")
  [[ $name == lost+found || $tops == *" $name "* || ! -r $path || ! -x $path ]] && continue
  folders+=("$name")
done
((${#folders[@]})) && members+=(-C "$cache_dir" "${folders[@]}")

for path in "$@"; do
  [[ -e $path ]] || { echo "::warning::fc-cache: $path does not exist" >&2; exit 1; }
done
(($#)) && members+=(-C "$PWD" "$@")

# Files that cannot be read are left out (tar warns) rather than failing the job;
# -T /dev/null lets an empty /cache make an empty archive.
tar -cf "$out" --ignore-failed-read -T /dev/null "${members[@]}"

size=$(stat -c %s "$out")
if ((size > max_unpacked)); then
  echo "::warning::fc-cache: $out is $size bytes, over the supervisor's $max_unpacked" >&2
  exit 1
fi
echo "fc-cache: ${folders[*]} $* -> $size bytes"
