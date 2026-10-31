# public_stats.py signs what it sends; the standard library has no Ed25519.
# Every package is pinned by hash (uv pip compile --generate-hashes for
# x86_64-unknown-linux-musl, Python 3.15), so a swapped wheel fails the build.
FROM python:3.15.2-alpine@sha256:20a82dbc2783112ddb91d1534603f802d222a3e63e593cd002eb2ac534e70211
COPY public-stats-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt
