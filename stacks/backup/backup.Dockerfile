# backup.py needs Python and restic needs only its binary, so this is the Python image (the version
# mise.toml pins) with restic copied from its own image, whose tag is the restic version.
FROM python:3.15.2-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
COPY --from=restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 /usr/bin/restic /usr/bin/restic
RUN apk add --no-cache ca-certificates
