# public_stats.py signs what it sends; the standard library has no Ed25519.
# Every package is pinned by hash (uv pip compile --generate-hashes for
# x86_64-unknown-linux-musl, Python 3.15), so a swapped wheel fails the build.
FROM python:3.15.2-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
COPY public-stats-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt
