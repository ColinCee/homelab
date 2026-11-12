# One image for the bot and the worker. Every package is pinned by hash (uv pip compile
# --generate-hashes for x86_64-unknown-linux-musl, Python 3.15), so a swapped wheel fails the build.
# The agent's bash runs in the worker: the SDK needs /bin/bash, and the agent reads the repository
# with git and queries the gateway with curl and jq.
FROM python:3.15.2-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
RUN apk add --no-cache bash ca-certificates curl git jq
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt
COPY bot.py worker.py /app/
# A new volume takes this owner, so the bot, which runs as nobody, can write its state.
RUN mkdir /state && chown 65534:65534 /state
