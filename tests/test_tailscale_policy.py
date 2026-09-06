import importlib.util
import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tailscale_policy", Path(__file__).parents[1] / "scripts/tailscale_policy.py"
)
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)

CURRENT = policy.policy_bytes(b'{"grants": []}')
CANDIDATE = policy.policy_bytes(b'{"grants": [], "ssh": []}')


def responses(monkeypatch, validation=b"{}", etag='"original"'):
    api = Mock(side_effect=[(CURRENT, etag), (validation, ""), (CANDIDATE, ""), (CANDIDATE, "")])
    monkeypatch.setattr(policy, "request", api)
    return api


def test_apply_sends_exact_candidate_with_original_etag(monkeypatch, tmp_path):
    api = responses(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "APPLY")
    policy.apply_policy(CANDIDATE, "token", "-", tmp_path, False)
    assert api.call_args_list[2].kwargs == {
        "data": CANDIDATE,
        "token": "token",
        "etag": '"original"',
    }
    backup = next(tmp_path.glob("policy-*.json"))
    assert backup.read_bytes() == CURRENT
    assert backup.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("answer", ["", "yes", "apply"])
def test_non_exact_confirmation_does_not_write(monkeypatch, tmp_path, answer):
    api = responses(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: answer)
    policy.apply_policy(CANDIDATE, "token", "-", tmp_path, False)
    assert api.call_count == 2


def test_check_mode_never_prompts_or_writes(monkeypatch, tmp_path):
    api = responses(monkeypatch)
    prompt = Mock(side_effect=AssertionError("unexpected prompt"))
    monkeypatch.setattr("builtins.input", prompt)
    policy.apply_policy(CANDIDATE, "token", "-", tmp_path, True)
    assert api.call_count == 2
    prompt.assert_not_called()


@pytest.mark.parametrize(
    "validation", [b'{"message":"test(s) failed"}', b'{"warnings":["unknown user"]}']
)
def test_http_200_validation_failure_stops_before_approval(monkeypatch, tmp_path, validation):
    api = responses(monkeypatch, validation=validation)
    with pytest.raises(policy.PolicyError, match="validation needs attention"):
        policy.apply_policy(CANDIDATE, "token", "-", tmp_path, False)
    assert api.call_count == 2


def test_missing_etag_stops_before_validation(monkeypatch, tmp_path):
    api = responses(monkeypatch, etag="")
    with pytest.raises(policy.PolicyError, match="ETag"):
        policy.apply_policy(CANDIDATE, "token", "-", tmp_path, False)
    assert api.call_count == 1


def test_conflict_is_not_retried(monkeypatch, tmp_path):
    api = responses(monkeypatch)
    api.side_effect = [(CURRENT, '"original"'), (b"{}", ""), policy.PolicyError("HTTP 412")]
    monkeypatch.setattr("builtins.input", lambda _: "APPLY")
    with pytest.raises(policy.PolicyError, match="412"):
        policy.apply_policy(CANDIDATE, "token", "-", tmp_path, False)
    assert api.call_count == 3


def test_readback_mismatch_is_not_reported_as_success(monkeypatch, tmp_path):
    api = responses(monkeypatch)
    api.side_effect = [(CURRENT, '"original"'), (b"{}", ""), (CANDIDATE, ""), (CURRENT, "")]
    monkeypatch.setattr("builtins.input", lambda _: "APPLY")
    with pytest.raises(policy.PolicyError, match="differs after"):
        policy.apply_policy(CANDIDATE, "token", "-", tmp_path, False)


def test_noop_does_not_write(monkeypatch, tmp_path):
    api = responses(monkeypatch, validation=b"")
    policy.apply_policy(CURRENT, "token", "-", tmp_path, False)
    assert api.call_count == 2


def test_rejects_test_array_instead_of_policy():
    with pytest.raises(policy.PolicyError, match="JSON object"):
        policy.policy_bytes(b"[]")


def test_piped_invocation_fails_before_credentials(monkeypatch):
    monkeypatch.setattr("sys.argv", ["tailscale_policy.py", "not-read.json"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    prompt = Mock(side_effect=AssertionError("unexpected credential prompt"))
    monkeypatch.setattr("builtins.input", prompt)
    with pytest.raises(SystemExit) as error:
        policy.main()
    assert error.value.code == 2
    prompt.assert_not_called()


def test_redirects_are_not_followed():
    assert (
        policy.NoRedirects().redirect_request(None, None, 302, "", {}, "https://elsewhere") is None
    )


def test_json_output_escapes_terminal_controls():
    candidate = policy.policy_bytes(json.dumps({"hosts": {"\x1b[2J": "100.1.2.3"}}))
    assert b"\x1b" not in candidate


def test_oauth_error_does_not_expose_secret(monkeypatch):
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError(
        "https://api.tailscale.com/api/v2/oauth/token",
        400,
        "error",
        {},
        io.BytesIO(b"secret-in-response"),
    )
    monkeypatch.setattr(policy.urllib.request, "build_opener", lambda _: opener)
    with pytest.raises(policy.PolicyError) as error:
        policy.request("oauth/token", data=b"client_secret=secret-in-response")
    assert "secret-in-response" not in str(error.value)


def test_file_changes_during_approval_do_not_change_uploaded_policy(monkeypatch, tmp_path):
    candidate_file = tmp_path / "candidate.json"
    candidate_file.write_bytes(CANDIDATE)
    api = Mock(
        side_effect=[
            (b'{"access_token":"secret-token"}', ""),
            (CURRENT, '"original"'),
            (b"{}", ""),
            (CANDIDATE, ""),
            (CANDIDATE, ""),
        ]
    )
    monkeypatch.setattr(policy, "request", api)
    monkeypatch.setattr("sys.argv", ["tailscale_policy.py", str(candidate_file)])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(policy.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(policy.getpass, "getpass", lambda _: "client-secret")

    def answer(prompt):
        if prompt.startswith("Tailscale OAuth client ID"):
            return "client-id"
        candidate_file.write_bytes(b'{"acls": []}')
        return "APPLY"

    monkeypatch.setattr("builtins.input", answer)
    policy.main()
    assert api.call_args_list[3].kwargs["data"] == CANDIDATE


@pytest.mark.parametrize(
    ("saved", "override", "expected"),
    [(None, None, "entered-id"), ("saved-id", None, "saved-id"), ("saved-id", "new-id", "new-id")],
)
def test_remembers_only_authenticated_client_id(monkeypatch, tmp_path, saved, override, expected):
    candidate_file = tmp_path / "candidate.json"
    candidate_file.write_bytes(CANDIDATE)
    client_id_file = tmp_path / ".config/tailscale-policy/client-id"
    if saved is not None:
        client_id_file.parent.mkdir(parents=True)
        client_id_file.write_text(saved)
    args = ["tailscale_policy.py", str(candidate_file)]
    if override is not None:
        args += ["--client-id", override]
    monkeypatch.setattr("sys.argv", args)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(policy.Path, "home", lambda: tmp_path)
    prompt = Mock(return_value="entered-id")
    monkeypatch.setattr("builtins.input", prompt)
    secret_prompt = Mock(return_value="not-saved-secret")
    monkeypatch.setattr(policy.getpass, "getpass", secret_prompt)
    api = Mock(return_value=(b'{"access_token":"not-saved-token"}', ""))
    monkeypatch.setattr(policy, "request", api)
    monkeypatch.setattr(policy, "apply_policy", Mock())

    policy.main()

    assert client_id_file.read_text() == expected + "\n"
    assert prompt.call_count == (1 if saved is None and override is None else 0)
    secret_prompt.assert_called_once()
    payload = policy.urllib.parse.parse_qs(api.call_args.kwargs["data"].decode())
    assert payload["client_id"] == [expected]
    assert {p for p in tmp_path.rglob("*") if p.is_file()} == {candidate_file, client_id_file}

    # A failed replacement login must not poison the remembered ID.
    monkeypatch.setattr("sys.argv", [*args[:2], "--client-id", "invalid-id"])
    api.side_effect = policy.PolicyError("HTTP 401")
    with pytest.raises(policy.PolicyError, match="401"):
        policy.main()
    assert client_id_file.read_text() == expected + "\n"
