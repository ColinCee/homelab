"""Make the public-stats key pair, for the first time or to rotate it.

Run it on the Beelink, in the checkout:
`python3 stacks/observability/public-stats/public_stats_key.py`. It writes the private key to
`secrets/public_stats_key`, which the public-stats container reads, and prints the public key for
HOMELAB_PUBLIC_KEY in the blog's wrangler.json. Then copy the new file's line into the Bitwarden
note "Homelab secrets", commit the public key to the blog, and redeploy both. A stolen private
key could only post these numbers.
"""

import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "stacks/telemetry"))
import telemetry  # noqa: E402

KEY = ROOT / "secrets/public_stats_key"


def main(out: Path = KEY) -> str:
    """Write a new private key to `out`; return the public key, in hex."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    # Readable by all, as every file in secrets/ (docs/recovery.md): only the folder is closed.
    telemetry.replace_file(out, private + "\n", 0o444)
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


if __name__ == "__main__":
    print("HOMELAB_PUBLIC_KEY:", main())
