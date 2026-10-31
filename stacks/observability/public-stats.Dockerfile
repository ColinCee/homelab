# public_stats.py signs what it sends; the standard library has no Ed25519.
FROM python:3.15.2-alpine@sha256:20a82dbc2783112ddb91d1534603f802d222a3e63e593cd002eb2ac534e70211
RUN pip install --no-cache-dir --only-binary=:all: cryptography==50.0.2
