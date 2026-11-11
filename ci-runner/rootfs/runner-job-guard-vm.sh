#!/bin/sh
# Job-started hook in every CI VM (set by ci-boot). The runner runs only hooks with a known extension, so this
# hands over to the typed guard at once; -I keeps Python from reading PYTHON* variables or the working directory.
exec /usr/local/bin/python3 -I /usr/local/lib/homelab/runner_job_guard.py --policy vm
