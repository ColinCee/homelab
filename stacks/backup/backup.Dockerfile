# backup.py needs Python and restic needs only its binary, so this is the Python image (the version
# mise.toml pins) with restic copied from its own image, whose tag is the restic version.
FROM python:3.15.2-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
COPY --from=restic/restic:0.20.0@sha256:93999df80d4ac6cb148e82d2ef1fe6a81996f17ff535e772723538211b31555a /usr/bin/restic /usr/bin/restic
RUN apk add --no-cache ca-certificates
