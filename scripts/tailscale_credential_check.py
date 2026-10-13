#!/usr/bin/env python3
"""Check that a Tailscale OAuth client can sign in and read and validate the policy. Writes nothing.

Run it yourself in a terminal: the secret is typed at a hidden prompt and never saved.
With --write-test it also proves the write scope, by sending the live policy back unchanged.
"""

import argparse
import getpass
import importlib.util
import json
import sys
import urllib.parse
import warnings
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "tailscale_policy", Path(__file__).with_name("tailscale_policy.py")
)
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


def step(name, call):
    """Run one check and say how it went; a failure is reported and returned as None."""
    try:
        result = call()
    except policy.PolicyError as exc:
        print(f"FAIL  {name}: {exc}")
        return None
    print(f"ok    {name}")
    return result


def check(client_id, secret, tailnet, write_test):
    """Sign in, read, validate, and optionally write the same policy back. True when all passed."""
    form = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret}
    signed_in = step(
        "sign in with the client ID and secret",
        lambda: policy.request(
            "oauth/token",
            data=urllib.parse.urlencode(form).encode(),
            content_type="application/x-www-form-urlencoded",
        ),
    )
    if signed_in is None:
        return False
    auth = json.loads(signed_in[0])
    token = auth.get("access_token") if isinstance(auth, dict) else None
    if not isinstance(token, str) or not token:
        print("FAIL  the sign-in answer held no access token")
        return False
    print(f"      scopes granted: {auth.get('scope') or 'not reported'}")

    path = f"tailnet/{urllib.parse.quote(tailnet, safe='')}/acl"
    read = step("read the live policy", lambda: policy.request(path, token=token))
    if read is None:
        return False
    raw, etag = read
    live = json.loads(raw)
    print(f"      {len(live.get('grants', []))} grants, {len(live.get('tests', []))} tests")

    def validate():
        result, _ = policy.request(f"{path}/validate", data=raw, token=token)
        if result.strip() and json.loads(result) != {}:
            raise policy.PolicyError(json.dumps(json.loads(result)))
        return True

    if step("validate the live policy (its own tests run)", validate) is None:
        return False
    if not write_test:
        print("Not tested: writing. Add --write-test to send the same policy back unchanged.")
        return True
    if not etag:
        print("FAIL  no ETag on the live policy, so a safe write is not possible")
        return False
    print("The write sends the same rules back as plain JSON: comments in the console are dropped.")
    if input("Send the live policy back to prove write access? Type WRITE: ") != "WRITE":
        print("Skipped: nothing written.")
        return True
    wrote = step(
        "write the same policy back",
        lambda: policy.request(path, data=raw, token=token, etag=etag),
    )
    if wrote is None:
        return False
    after = step("read it again", lambda: policy.request(path, token=token))
    if after is None:
        return False
    if policy.policy_bytes(after[0]) != policy.policy_bytes(raw):
        print("FAIL  the live policy differs after the write: inspect it now")
        return False
    print("ok    the live policy is unchanged")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", help="OAuth client ID (not secret); default: the saved one")
    parser.add_argument("--tailnet", default="-", help="Tailnet ID (default: credential's tailnet)")
    parser.add_argument("--write-test", action="store_true", help="Also write the live policy back")
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("Run this yourself in an interactive terminal.")
    saved = Path.home() / ".config/tailscale-policy/client-id"
    client_id = (args.client_id or (saved.read_text() if saved.exists() else "")).strip()
    if not client_id:
        client_id = input(policy.ID_PROMPT).strip()
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        secret = getpass.getpass(policy.SECRET_PROMPT).strip()
    if not client_id or not secret:
        raise policy.PolicyError("Both the client ID and the secret are required.")
    if not secret.startswith(f"tskey-client-{client_id}-"):
        print(
            f"Note: the secret does not start with tskey-client-{client_id}-; they may not match."
        )
    passed = check(client_id, secret, args.tailnet, args.write_test)
    print("\nThe credential works." if passed else "\nThe credential does not work yet.")
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (policy.PolicyError, OSError, ValueError, getpass.GetPassWarning) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
