"""Runtime environment configuration via Pydantic Settings."""

from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class ApiSettings(BaseSettings):
    """Required environment for the API container."""

    github_app_id: str
    github_app_installation_id: str
    github_app_key_file: str
    copilot_github_token: str
    agent_api_key: str
    model: str = "gpt-5.4"
    log_format: Literal["json", "text"] = "json"
    reasoning_effort: str = "high"


class WorkerSettings(BaseSettings):
    """Required environment for ephemeral worker containers."""

    task_type: str = Field(validation_alias=AliasChoices("TASK_TYPE", "WORKER_TASK"))
    repo: str = Field(validation_alias=AliasChoices("REPO", "WORKER_REPO"))
    number: int = Field(
        validation_alias=AliasChoices("NUMBER", "WORKER_ISSUE_NUMBER", "WORKER_PR_NUMBER")
    )
    gh_token: str
    copilot_github_token: str = ""
    model: str = "gpt-5.4"
    log_format: Literal["json", "text"] = "json"
    reasoning_effort: str = "high"
    session_id: str | None = Field(
        default=None, validation_alias=AliasChoices("SESSION_ID", "WORKER_SESSION_ID")
    )
