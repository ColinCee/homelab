"""Tests for centralized trust validation."""

from trust import ALLOWED_ACTORS, is_trusted_actor, is_trusted_content_author


class TestIsTrustedActor:
    def test_allows_repo_owner(self):
        assert is_trusted_actor("ColinCee")

    def test_allows_bot(self):
        assert is_trusted_actor("colins-homelab-bot[bot]")

    def test_rejects_unknown_actor(self):
        assert not is_trusted_actor("attacker")

    def test_rejects_empty_string(self):
        assert not is_trusted_actor("")


class TestIsTrustedContentAuthor:
    def test_allows_trusted_user(self):
        assert is_trusted_content_author({"user": {"login": "ColinCee"}})

    def test_allows_bot_user(self):
        issue = {"user": {"login": "colins-homelab-bot[bot]"}}
        assert is_trusted_content_author(issue)

    def test_rejects_unknown_user(self):
        assert not is_trusted_content_author({"user": {"login": "attacker"}})

    def test_rejects_missing_user_field(self):
        assert not is_trusted_content_author({"title": "no user"})

    def test_rejects_null_user(self):
        assert not is_trusted_content_author({"user": None})

    def test_rejects_missing_login(self):
        assert not is_trusted_content_author({"user": {}})


class TestAllowedActorsImmutable:
    def test_is_frozenset(self):
        assert isinstance(ALLOWED_ACTORS, frozenset)
