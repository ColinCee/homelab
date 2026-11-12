#!/bin/sh
# Job-started hook in every CI VM (set by ci-boot). The runner runs only hooks with a known extension, so this
# hands over to the typed guard at once; -I keeps Python from reading PYTHON* variables or the working directory.
/usr/local/bin/python3 -I /usr/local/lib/homelab/runner_job_guard.py --policy vm && exit 0
# Refused, or the guard itself failed: a refused job is only marked failed, and its `if: always()` steps would still
# run, so the VM ends now. It serves one job only.
sudo -n poweroff -f
exit 1
