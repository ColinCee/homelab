#!/usr/bin/env bash
# Moved to stacks/deploy/deploy.sh. Kept one release: the deploy-main run that pulls this change was
# started by the old deploy_main.py, which calls this path. Delete with scripts/.
exec "$(dirname "$0")/../stacks/deploy/deploy.sh" "$@"
