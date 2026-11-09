# public_stats.py signs what it sends; the standard library has no Ed25519.
# Every package is pinned by hash (uv pip compile --generate-hashes for
# x86_64-unknown-linux-musl, Python 3.15), so a swapped wheel fails the build.
FROM python:3.15.2-alpine@sha256:a0f2e268f2ce8f3709611a90054ec6a8ae31f6251f5896b8648b9bb99835e30a
COPY public-stats-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt
