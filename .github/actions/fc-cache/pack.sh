#!/usr/bin/env bash
# Usage: pack.sh CACHE_DIR OUT [WORKSPACE_PATH...]
# Tars every readable top-level folder of CACHE_DIR (but lost+found) and the
# given workspace paths (relative to the current folder) into OUT, and fails
# rather than leave an archive the CI supervisor would refuse.
set -euo pipefail

cache_dir=$1 out=$2
shift 2

# scripts/ci_supervisor.py: CACHE_MAX_UNPACKED_BYTES and CACHE_MAX_BYTES (the zip).
max_unpacked=6442450944
max_zip=2147483648

# The cache disk holds the last cache's workspace paths at the same place (web/ for
# web/node_modules): pack the job's fresh ones, not both, or every cache would carry
# all earlier copies too.
tops=" "
for path in "$@"; do tops+="${path%%/*} "; done

members=() folders=()
for path in "$cache_dir"/*/; do
  name=$(basename "$path")
  [[ $name == lost+found || $tops == *" $name "* || ! -r $path || ! -x $path ]] && continue
  folders+=("$name")
done
((${#folders[@]})) && members+=(-C "$cache_dir" "${folders[@]}")

for path in "$@"; do
  [[ -e $path ]] || { echo "::error::fc-cache: $path does not exist" >&2; exit 1; }
done
(($#)) && members+=(-C "$PWD" "$@")

# Files that cannot be read are left out (tar warns) rather than failing the job;
# -T /dev/null lets an empty /cache make an empty archive.
tar -cf "$out" --ignore-failed-read -T /dev/null "${members[@]}"

size=$(stat -c %s "$out")
if ((size > max_unpacked)); then
  echo "::error::fc-cache: $out is $size bytes, over the supervisor's $max_unpacked" >&2
  exit 1
fi
# Only an archive over the zip limit can zip to more than it; deflate level 1
# as upload-artifact does, to measure.
if ((size > max_zip)); then
  zipped=$(gzip -1 -c "$out" | wc -c)
  if ((zipped > max_zip)); then
    echo "::error::fc-cache: $out zips to about $zipped bytes, over the supervisor's $max_zip" >&2
    exit 1
  fi
fi
echo "fc-cache: ${folders[*]} $* -> $size bytes"
