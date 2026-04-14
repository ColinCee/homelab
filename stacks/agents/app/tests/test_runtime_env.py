"""Tests for runtime environment validation helpers."""

import pytest

from runtime_env import RequiredEnvironmentError, missing_required_env, validate_required_env


def test_missing_required_env_lists_missing_and_empty_values():
    missing = missing_required_env(
        ("GITHUB_APP_ID", "COPILOT_GITHUB_TOKEN", "MODEL"),
        {
            "GITHUB_APP_ID": "",
            "COPILOT_GITHUB_TOKEN": "token",
            "MODEL": "gpt-5.4",
        },
    )

    assert missing == ("GITHUB_APP_ID",)


def test_validate_required_env_raises_clear_error_for_multiple_names():
    with pytest.raises(RequiredEnvironmentError) as exc_info:
        validate_required_env(
            ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID"),
            {"GITHUB_APP_ID": "", "GITHUB_APP_INSTALLATION_ID": ""},
        )

    assert exc_info.value.names == ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID")
    assert (
        str(exc_info.value) == "Required environment variables are missing or empty: "
        "GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID"
    )


def test_validate_required_env_accepts_populated_values():
    validate_required_env(
        ("TASK_TYPE", "REPO", "NUMBER", "GH_TOKEN"),
        {
            "TASK_TYPE": "review",
            "REPO": "user/repo",
            "NUMBER": "42",
            "GH_TOKEN": "ghs_test",
        },
    )
