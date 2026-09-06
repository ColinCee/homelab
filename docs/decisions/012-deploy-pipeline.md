# ADR-012: Deploy on the Beelink runner

**Status:** Accepted

Use a self-hosted GitHub Actions runner for trusted `main` deployments and
GitHub-hosted runners for pull-request CI. This avoids giving a hosted CI machine
tailnet access or maintaining a separate deployment platform.

The workflow owns Git checkout and secret rendering. `scripts/deploy.sh` applies
the current checkout and installs the service timers. Reconcile all stacks rather
than maintaining changed-file detection; Compose leaves unchanged containers
running.

Repository write access can become host-level execution through workflows and
Compose. Protect `main`, require CI before merge, and keep merging a human
decision. An in-file branch condition is not a substitute for GitHub access
controls.

The [runbook](../runbooks/deploying-services.md) owns operator commands and
retirement steps. Generated `.env` files are Compose data, never shell scripts.
