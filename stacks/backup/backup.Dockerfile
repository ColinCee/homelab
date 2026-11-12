# backup.py needs Python, which the restic image lacks: Alpine's own package, at the version ruff targets
# (pyproject.toml). Its patch release is whatever Alpine's repository holds at build time.
FROM restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510
RUN apk add --no-cache 'python3~3.14'
