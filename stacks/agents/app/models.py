"""Typed models for data crossing agent service boundaries."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class GitHubUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    login: str = ""


class GitHubRepository(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str = ""


class GitHubPullRequestBranch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str | None = None
    repo: GitHubRepository | None = None


class GitHubAutoMerge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled_by: GitHubUser | None = None
    merge_method: str | None = None


class GitHubIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    body: str | None = None
    user: GitHubUser | None = None


class GitHubPullRequest(GitHubIssue):
    number: int
    html_url: str | None = None
    state: str | None = None
    merged_at: str | None = None
    merged: bool = False
    auto_merge: GitHubAutoMerge | None = None
    base: GitHubPullRequestBranch | None = None
    head: GitHubPullRequestBranch | None = None


class GitHubIssueComment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    body: str | None = None
    user: GitHubUser | None = None


class ReviewThread(BaseModel):
    """A review thread on a PR, from the GraphQL API."""

    id: str
    is_resolved: bool
    is_outdated: bool
    body: str = ""


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["complete", "failed", "partial", "rejected"]
    repo: str | None = None
    model: str | None = None
    elapsed_seconds: float | None = None
    api_time_seconds: float | None = None
    reasoning_effort: str | None = None
    premium_requests: int = 0
    models: dict[str, str] | None = None
    tokens_line: str | None = None
    session_id: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    merged: bool | None = None
    auto_merge: bool | None = None
    error: str | None = None
