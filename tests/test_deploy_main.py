import http.client
import subprocess
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from stacks.deploy import deploy_main, doorbell


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


class Lab:
    """GitHub's main (a bare repo), a clone pushing to it, and the Beelink's checkout."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.dev = root / "dev"
        self.checkout = root / "checkout"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.dev))
        for key, value in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            git(self.dev, "config", key, value)
        self.merge("first")
        git(root, "clone", "-q", str(self.origin), str(self.checkout))
        self.marker = root / "last-deployed"
        self.metrics = root / "main-deploy.prom"
        self.deploys: list[tuple[str, str | None]] = []
        self.code = 0
        self.on_deploy = lambda: None

    def merge(self, name: str) -> str:
        (self.dev / name).write_text(name)
        git(self.dev, "add", name)
        git(self.dev, "commit", "-q", "-m", name)
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        return git(self.dev, "rev-parse", "HEAD")

    def deploy(self, since: str | None) -> int:
        head = git(self.checkout, "rev-parse", "HEAD")
        self.deploys.append((head, since))
        self.on_deploy()
        if self.code == 0:
            self.marker.write_text(head + "\n")
        return self.code

    def host(self) -> deploy_main.Host:
        return deploy_main.Host(
            run=deploy_main.run_in(self.checkout),
            deploy=self.deploy,
            marker=self.marker,
            metrics=self.metrics,
            sleep=lambda s: None,
        )


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return Lab(tmp_path)


def test_a_ring_deploys_main_from_the_last_deployed_commit(lab: Lab) -> None:
    first = git(lab.dev, "rev-parse", "HEAD")
    lab.marker.write_text(first + "\n")
    second = lab.merge("second")

    assert deploy_main.deploy_main(lab.host()) == 0
    assert lab.deploys == [(second, first)]
    assert git(lab.checkout, "rev-parse", "HEAD") == second
    assert "homelab_main_deploy_behind 0" in lab.metrics.read_text()


def test_a_merge_landing_mid_deploy_is_deployed_in_the_same_run(lab: Lab) -> None:
    second = lab.merge("second")
    third = ""

    def merge_once() -> None:
        nonlocal third
        if not third:
            third = lab.merge("third")

    lab.on_deploy = merge_once
    assert deploy_main.deploy_main(lab.host()) == 0
    assert lab.deploys == [(second, None), (third, second)]


def test_a_checkout_reset_to_an_older_commit_mid_deploy_is_deployed_again(lab: Lab) -> None:
    first = git(lab.dev, "rev-parse", "HEAD")
    second = lab.merge("second")
    resets = [first]

    def reset_once() -> None:
        if resets:
            git(lab.checkout, "reset", "-q", "--hard", resets.pop())

    lab.on_deploy = reset_once

    assert deploy_main.deploy_main(lab.host()) == 0
    assert [head for head, _ in lab.deploys] == [second, second]


def test_a_ring_with_nothing_new_still_runs_the_deploy_for_changed_secrets(lab: Lab) -> None:
    head = git(lab.dev, "rev-parse", "HEAD")
    lab.marker.write_text(head + "\n")

    assert deploy_main.deploy_main(lab.host()) == 0
    assert lab.deploys == [(head, head)]


def test_a_failed_deploy_says_behind_and_the_next_run_retries_from_the_same_commit(
    lab: Lab,
) -> None:
    first = git(lab.dev, "rev-parse", "HEAD")
    lab.marker.write_text(first + "\n")
    second = lab.merge("second")
    lab.code = 1

    assert deploy_main.deploy_main(lab.host()) == 1
    assert "homelab_main_deploy_behind 1" in lab.metrics.read_text()
    lab.code = 0
    assert deploy_main.deploy_main(lab.host()) == 0
    assert lab.deploys == [(second, first), (second, first)]


def test_a_force_pushed_main_is_refused(lab: Lab) -> None:
    lab.merge("second")
    lab.marker.write_text(git(lab.dev, "rev-parse", "HEAD") + "\n")
    git(lab.dev, "reset", "-q", "--hard", "HEAD~1")
    git(lab.dev, "commit", "-q", "--allow-empty", "-m", "rewritten")
    git(lab.dev, "push", "-q", "--force", "origin", "HEAD:main")

    assert deploy_main.deploy_main(lab.host()) == 1
    assert lab.deploys == []
    assert "homelab_main_deploy_behind 1" in lab.metrics.read_text()


@pytest.fixture
def bell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, Path]]:
    ring = tmp_path / "rung"
    monkeypatch.setattr(doorbell, "RING", ring)
    server = ThreadingHTTPServer(("127.0.0.1", 0), doorbell.Doorbell)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port, ring
    server.shutdown()


def ask(port: int, method: str, path: str) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    # As big as a real push payload: an unread body would reset the reply.
    connection.request(method, path, body=b"x" * 25_000)
    return connection.getresponse().status


def test_only_a_post_to_ring_rings(bell: tuple[int, Path]) -> None:
    port, ring = bell
    assert ask(port, "GET", "/ring") == 404
    assert ask(port, "POST", "/other") == 404
    assert not ring.exists()
    assert ask(port, "POST", "/ring") == 202
    first = ring.read_text()
    assert ask(port, "POST", "/ring") == 202
    assert ring.read_text() != first
