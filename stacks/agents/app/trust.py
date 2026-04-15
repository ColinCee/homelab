"""Centralized trust validation for all agent input channels.

Every piece of untrusted content that enters a CLI prompt must pass through
a check here. Co-locating these makes it hard to miss a channel.

Trust layers (outer to inner):
  1. Workflow YAML — gates on commenter author_association (free, prevents run)
  2. ALLOWED_ACTORS — endpoint rejects unknown triggering actors (403)
  3. Content trust — validates authorship of content injected into prompts
"""

from models import GitHubIssue, GitHubPullRequest

# Only these GitHub users can trigger agent tasks or author content that
# gets injected into CLI prompts. Single source of truth for all trust decisions.
ALLOWED_ACTORS = frozenset({"ColinCee", "colins-homelab-bot[bot]"})


def is_trusted_actor(actor: str) -> bool:
    """Check if a GitHub actor is allowed to trigger agent tasks."""
    return actor in ALLOWED_ACTORS


def is_trusted_content_author(issue_or_pr: GitHubIssue | GitHubPullRequest) -> bool:
    """Check if the author of this issue/PR is trusted for prompt injection.

    Looks up the author's login from the GitHub API response and checks
    against ALLOWED_ACTORS. Prevents prompt injection via attacker-controlled
    content (e.g. malicious issue bodies).
    """
    login = issue_or_pr.user.login if issue_or_pr.user else ""
    return login in ALLOWED_ACTORS
