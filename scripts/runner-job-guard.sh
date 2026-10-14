#!/bin/sh
# Job-started hook for the Beelink deploy runner. The runner only runs hooks with a
# known extension, so this wrapper hands over to the typed guard at once. -I keeps
# Python from reading PYTHON* variables or the job's working directory.
# Installed root-owned by scripts/install-runner-guard.sh; MODE is set at install time.
MODE=enforce
exec /usr/bin/python3 -I /usr/local/lib/homelab/runner_job_guard.py --mode "$MODE"
