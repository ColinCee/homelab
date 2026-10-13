import importlib.util
from pathlib import Path
from unittest.mock import Mock

SPEC = importlib.util.spec_from_file_location(
    "tailscale_credential_check",
    Path(__file__).parents[1] / "scripts/tailscale_credential_check.py",
)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

LIVE = b'{"grants": [{}], "tests": []}'
TOKEN = (b'{"access_token": "token", "scope": "policy_file"}', "")


def test_default_run_reads_and_validates_but_never_writes(monkeypatch, capsys):
    api = Mock(side_effect=[TOKEN, (LIVE, '"etag"'), (b"{}", "")])
    monkeypatch.setattr(check.policy, "request", api)
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("unexpected prompt")))
    assert check.check("id", "secret", "-", False)
    assert api.call_count == 3
    assert [call.args[0] for call in api.call_args_list][1:] == [
        "tailnet/-/acl",
        "tailnet/-/acl/validate",
    ]
    assert "secret" not in capsys.readouterr().out.replace("client ID and secret", "")


def test_rejected_credentials_fail_without_further_requests(monkeypatch, capsys):
    api = Mock(side_effect=check.policy.PolicyError("HTTP 401"))
    monkeypatch.setattr(check.policy, "request", api)
    assert not check.check("id", "secret", "-", True)
    assert api.call_count == 1 and "FAIL" in capsys.readouterr().out


def test_write_test_sends_the_live_policy_back_byte_for_byte_with_its_etag(monkeypatch):
    api = Mock(side_effect=[TOKEN, (LIVE, '"etag"'), (b"{}", ""), (LIVE, ""), (LIVE, "")])
    monkeypatch.setattr(check.policy, "request", api)
    monkeypatch.setattr("builtins.input", lambda _: "WRITE")
    assert check.check("id", "secret", "-", True)
    assert api.call_args_list[3].kwargs == {"data": LIVE, "token": "token", "etag": '"etag"'}


def test_write_test_needs_the_exact_word(monkeypatch):
    api = Mock(side_effect=[TOKEN, (LIVE, '"etag"'), (b"{}", "")])
    monkeypatch.setattr(check.policy, "request", api)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert check.check("id", "secret", "-", True)
    assert api.call_count == 3
