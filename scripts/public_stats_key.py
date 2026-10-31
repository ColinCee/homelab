"""Make the public-stats key pair, for the first time or to rotate it.

Stores the private key as the PUBLIC_STATS_KEY GitHub secret, its only copy,
and prints the public key for HOMELAB_PUBLIC_KEY in the blog's wrangler.json.
Run it yourself: `uv run python scripts/public_stats_key.py`. See
docs/public-stats.md.
"""

import subprocess

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


def main() -> None:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    # Through stdin, so the key never appears in a process list.
    subprocess.run(
        ["gh", "secret", "set", "PUBLIC_STATS_KEY", "-R", "ColinCee/homelab"],
        input=private.encode(),
        check=True,
    )
    print(
        "HOMELAB_PUBLIC_KEY:", key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    )


if __name__ == "__main__":
    main()
