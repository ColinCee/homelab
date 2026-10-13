#!/usr/bin/env python3
"""Interactively validate and apply one Tailscale policy using a scoped OAuth client."""

import argparse
import difflib
import getpass
import json
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

# The policy lives in the private notes checkout beside this one; its folder was renamed once.
NOTES = Path(__file__).resolve().parents[2] / "notes"
POLICY_FILES = (
    NOTES / "04-dev/homelab/tailscale-acl.jsonc",
    NOTES / "areas/homelab/tailscale-acl.jsonc",
)
ID_PROMPT = "Client ID (the Bitwarden username, ends in CNTRL): "
SECRET_PROMPT = "Client secret: paste it and press Enter (nothing shows as you paste): "


class PolicyError(Exception):
    pass


def default_policy():
    for path in POLICY_FILES:
        if path.exists():
            return path
    raise PolicyError(f"No policy file found at {POLICY_FILES[0]}. Give its path as an argument.")


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def tls_context():
    """Certificate checking that also works on Windows.

    Python's default there loads the Windows intermediate store too, and OpenSSL then prefers a
    stale intermediate from it over the valid one the server sends: an expired ISRG Root X2
    cross-sign made every request fail with "certificate has expired" (October 2026). Trust
    only the Windows root store and take intermediates from the server, as other platforms do.
    """
    if sys.platform == "win32":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        for der, encoding, trust in ssl.enum_certificates("ROOT"):
            if encoding == "x509_asn" and (
                trust is True or (trust and ssl.Purpose.SERVER_AUTH.oid in trust)
            ):
                try:
                    context.load_verify_locations(cadata=der)
                except ssl.SSLError:
                    continue  # one unreadable root must not hide the rest
        return context
    return ssl.create_default_context()


def request(path, *, data=None, token=None, etag=None, content_type="application/json"):
    headers = {"Accept": "application/json", "Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-Match"] = etag
    req = urllib.request.Request(
        f"https://api.tailscale.com/api/v2/{path}", data=data, headers=headers
    )
    # Never forward credentials to a redirect destination.
    opener = urllib.request.build_opener(
        NoRedirects(), urllib.request.HTTPSHandler(context=tls_context())
    )
    try:
        with opener.open(req, timeout=20) as response:
            return response.read(), response.headers.get("ETag", "")
    except urllib.error.HTTPError as exc:
        detail = ""
        if token:
            # Escape control characters and redact credentials from API diagnostics.
            detail = json.dumps(exc.read().decode(errors="replace").replace(token, "[REDACTED]"))
        hint = ""
        if path == "oauth/token" and exc.code == 401:
            hint = (
                "Tailscale rejected the client ID or secret. The ID is the part of the secret "
                "after 'tskey-client-'; check the client still exists under Settings > Trust "
                "credentials, or create a new one."
            )
        raise PolicyError(f"Tailscale {path}: HTTP {exc.code} {detail}{hint}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else "timeout"
        raise PolicyError(f"Tailscale request failed: {reason}") from None


def policy_bytes(raw):
    policy = json.loads(raw)
    if not isinstance(policy, dict):
        raise PolicyError("The policy must be a JSON object, not a list of tests.")
    return (json.dumps(policy, indent=2, sort_keys=True) + "\n").encode()


def backup_policy(current, directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix="policy-", suffix=".json", dir=directory)
    with os.fdopen(fd, "wb") as backup:
        backup.write(current)
    return Path(name)


def apply_policy(candidate, token, tailnet, backup_dir, check_only):
    path = f"tailnet/{urllib.parse.quote(tailnet, safe='')}/acl"
    raw, etag = request(path, token=token)
    current = policy_bytes(raw)
    if not etag:
        raise PolicyError(
            "No policy ETag returned; refusing an update without conflict protection."
        )
    backup = backup_policy(current, backup_dir)
    print(f"Live policy backup (JSON, without comments): {backup}")

    result, _ = request(f"{path}/validate", data=candidate, token=token)
    # This endpoint can report failed tests or warnings with HTTP 200.
    if result.strip() and json.loads(result) != {}:
        raise PolicyError(f"Policy validation needs attention: {json.dumps(json.loads(result))}")
    print("Tailscale policy validation passed.")
    if candidate == current:
        print("No policy change needed.")
        return
    print(
        "".join(
            difflib.unified_diff(
                current.decode().splitlines(keepends=True),
                candidate.decode().splitlines(keepends=True),
                fromfile="live policy",
                tofile="proposed policy",
            )
        ),
        end="",
    )
    if check_only:
        print("Check only: nothing applied.")
        return
    if input("\nReplace the live policy? Type APPLY: ") != "APPLY":
        print("Cancelled: nothing applied.")
        return

    # Send the in-memory policy shown above, not a file that could change after approval.
    print(
        "Applying. If interrupted or unsuccessful, inspect live state with --check before retrying."
    )
    request(path, data=candidate, token=token, etag=etag)
    live, _ = request(path, token=token)
    if policy_bytes(live) != candidate:
        raise PolicyError(
            "Live policy differs after the update. Inspect it before taking further action."
        )
    print("Applied and confirmed. Check SSH and service access from your devices.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "policy",
        type=Path,
        nargs="?",
        help="Candidate JSON policy, no comments (default: the one in the notes checkout)",
    )
    parser.add_argument(
        "--check", action="store_true", help="Back up, validate and show diff; no write"
    )
    parser.add_argument("--tailnet", default="-", help="Tailnet ID (default: credential's tailnet)")
    parser.add_argument(
        "--client-id", help="Use and remember a different OAuth client ID (not secret)"
    )
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error(
            "Run this yourself in an interactive terminal; piped approvals are not supported."
        )
    source = args.policy or default_policy()
    candidate = policy_bytes(source.read_bytes())
    print(f"Policy file: {source}")
    print("Nothing changes unless you type APPLY at the end." if not args.check else "Check only.")
    print(f"Target tailnet: {args.tailnet} (- means the OAuth credential's default tailnet)")
    client_id_file = Path.home() / ".config/tailscale-policy/client-id"
    if args.client_id is not None:
        client_id = args.client_id.strip()
    elif client_id_file.exists():
        client_id = client_id_file.read_text().strip()
        print("Using saved OAuth client ID. Use --client-id to replace it.")
    else:
        client_id = input(ID_PROMPT).strip()
    if not client_id:
        raise PolicyError("Client ID is empty. Supply a valid ID with --client-id.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        # A pasted secret often carries a trailing space or newline; a real one has none.
        secret = getpass.getpass(SECRET_PROMPT).strip()
    if not secret:
        raise PolicyError("Client secret is required.")
    response, _ = request(
        "oauth/token",
        data=urllib.parse.urlencode(
            {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret}
        ).encode(),
        content_type="application/x-www-form-urlencoded",
    )
    del secret
    auth = json.loads(response)
    if not isinstance(auth, dict) or not isinstance(auth.get("access_token"), str):
        raise PolicyError("OAuth response did not contain an access token.")
    token = auth["access_token"]
    if not token:
        raise PolicyError("OAuth returned an empty access token.")
    # Remember only the non-secret ID, and only after authentication succeeds.
    client_id_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client_id_file.write_text(client_id + "\n")
    apply_policy(
        candidate, token, args.tailnet, Path.home() / ".local/state/tailscale-policy", args.check
    )


if __name__ == "__main__":
    try:
        main()
    except (PolicyError, OSError, ValueError, getpass.GetPassWarning) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print(
            "\nInterrupted. If applying had started, inspect live state before retrying.",
            file=sys.stderr,
        )
        sys.exit(130)
