"""The on-call agent's side that holds the credentials (docs/architecture.md, "On-call agent").

An incident is one Discord forum post and one Managed Agents session. The agent's custom tools run
here, not in the worker, because they need the Discord, Docker and GitHub access the worker must
never hold.
"""

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import anthropic
import discord
from anthropic import AsyncAnthropic

# The forum's own tags. A post with neither "Fixing" nor "Needs you" is done, and closed.
FIRING, WORKING, NEEDS_OWNER = "firing", "fixing", "needs you"
DISCORD_LIMIT = 2000
# How often a firing `level="now"` alert mentions the owner again, as Alertmanager's posts did.
REMIND_EVERY = 4 * 3600
SWEEP_EVERY = 6 * 3600
POLL_ALERTS_EVERY = 30
# Closed incidents are forgotten after this; their sessions and posts stay where they are.
KEEP_CLOSED = 14 * 86400
# Hard spend caps per session, in US cents at list price: a looping session stops there.
BUDGET_CENTS = {"alert": 500, "owner": 500, "sweep": 150}
# Always firing: Alertmanager sends it to Healthchecks.io, never to a person.
IGNORED_ALERTS = frozenset({"watchdog"})
APPROVE_WORDS = frozenset({"yes", "y", "approve", "allow", "go", "go ahead", "ok"})
NO_DATA = re.compile(r"-1(\.0*)?")
BRANCH_CHARS = re.compile(r"[^a-z0-9-]+")
CONTAINER_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
# A container ID or its prefix would get past the check on the on-call containers' own names.
CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")
SECRET_FILES = (
    "anthropic_api_key",
    "discord_bot_token",
    "oncall_github_token",
    "oncall_healthchecks_url",
    "agents_password",
)


def log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


def secret(name: str) -> str:
    return Path(f"/run/secrets/{name}").read_text(encoding="utf-8").strip()


def failure(error: Exception) -> str:
    """An error for the logs, without its message: an HTTP error's carries its URL, and the ping
    URL is a secret."""
    status = getattr(error, "status", None)
    return f"{type(error).__name__} {status}" if status else type(error).__name__


@dataclass
class Incident:
    session_id: str
    thread_id: int | None = None
    fingerprint: str | None = None
    # The alert's labels, annotations and start, as Alertmanager gave them.
    alert: dict[str, Any] = field(default_factory=dict)
    firing: bool = False
    reminded_at: float = 0.0
    closed: bool = False
    closed_at: float = 0.0
    # Deleted as noise: kept open with no post until its alert clears, so it is not posted again.
    muted: bool = False
    answered: bool = False
    # Event IDs already handled: the event history is replayed after every reconnect and restart.
    seen: list[str] = field(default_factory=list)
    # The paused tool call that the owner's next reply answers.
    pending_confirmation: str | None = None


@dataclass
class State:
    incidents: dict[str, Incident] = field(default_factory=dict)
    last_sweep: float = 0.0

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        incidents = {sid: Incident(**inc) for sid, inc in raw.get("incidents", {}).items()}
        return cls(incidents=incidents, last_sweep=raw.get("last_sweep", 0.0))

    def save(self, path: Path, now: float) -> None:
        self.incidents = {
            sid: i
            for sid, i in self.incidents.items()
            if not (i.closed and i.closed_at and i.closed_at < now - KEEP_CLOSED)
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self)), encoding="utf-8")
        tmp.replace(path)

    def by_thread(self, thread_id: int) -> Incident | None:
        return next((i for i in self.incidents.values() if i.thread_id == thread_id), None)

    def open_for(self, fingerprint: str) -> Incident | None:
        return next(
            (i for i in self.incidents.values() if i.fingerprint == fingerprint and not i.closed),
            None,
        )


def reconcile(
    state: State, alerts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[Incident], list[Incident]]:
    """The alerts with no open incident, the open incidents whose alert fires again, and those
    whose alert has stopped firing."""
    firing = {
        a["fingerprint"]: a for a in alerts if a["labels"].get("alertname") not in IGNORED_ALERTS
    }
    new = [a for fp, a in firing.items() if state.open_for(fp) is None]
    alerting = [i for i in state.incidents.values() if i.fingerprint and not i.closed]
    again = [i for i in alerting if not i.firing and i.fingerprint in firing]
    cleared = [i for i in alerting if i.firing and i.fingerprint not in firing]
    return new, again, cleared


def alert_title(alert: dict[str, Any]) -> str:
    labels = alert["labels"]
    rest = [str(v) for k, v in sorted(labels.items()) if k not in ("alertname", "level")]
    title = str(alert["annotations"].get("title") or labels.get("alertname", "Alert"))
    return " · ".join([title, *rest])[:100]


def alert_post(alert: dict[str, Any], owner_id: int) -> str:
    notes = alert["annotations"]
    value = str(notes.get("value", ""))
    # Rules with a no-data arm report -1 when their metric is gone (rules.yml).
    shown = "no data" if NO_DATA.fullmatch(value) else f"{value}{notes.get('unit', '')}"
    lines = [
        f"{notes.get('measure', 'Value')}: **{shown}**",
        f"**Why it matters:** {notes.get('harm', '')}",
        f"**What to do:** {notes.get('action', '')}",
        f"[Dashboards]({notes['dashboard']})" if notes.get("dashboard") else "",
        "Checking on it.",
    ]
    if alert["labels"].get("level") == "now":
        lines.append(f"<@{owner_id}>")
    return "\n".join(line for line in lines if line)


def chunks(text: str, size: int = DISCORD_LIMIT) -> list[str]:
    out: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        out.append(text)
    return out


def owner_intent(text: str, pending: str | None) -> str:
    words = text.strip().lower().rstrip(".!")
    if words == "stop":
        return "interrupt"
    if pending:
        return "allow" if words in APPROVE_WORDS else "deny"
    return "message"


def pr_branch(name: str, now: float) -> str:
    """Under oncall/ whatever the agent asks, so this program never writes main."""
    slug = BRANCH_CHARS.sub("-", name.lower()).strip("-")[:50] or "fix"
    return f"oncall/{slug}-{int(now)}"


def check_path(path: str) -> str:
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"not a repository path: {path}")
    return path


def redact(text: str, secrets: list[str]) -> str:
    for value in secrets:
        text = text.replace(value, "[redacted]")
    return text


def holds_secret(text: str, secrets: list[str]) -> bool:
    return any(value in text for value in secrets)


class Oncall(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False),
        )
        self.owner_id = int(os.environ["OWNER_DISCORD_ID"])
        self.forum_id = int(os.environ["FORUM_CHANNEL_ID"])
        self.repo = os.environ["GITHUB_REPO"]
        self.gateway = os.environ["GATEWAY_URL"]
        self.docker = os.environ["DOCKER_URL"]
        self.state_path = Path(os.environ["STATE_FILE"])
        lock = json.loads(Path(os.environ["CLAUDE_LOCK"]).read_text(encoding="utf-8"))
        self.agent_id = lock["resources"]["./agent/agent.md"]["id"]
        self.environment_id = lock["resources"]["./agent/environment.yaml"]["id"]
        self.anthropic = AsyncAnthropic(api_key=secret("anthropic_api_key"))
        self.gateway_auth = aiohttp.BasicAuth("agents@homelab.internal", secret("agents_password"))
        self.github_token = secret("oncall_github_token")
        self.ping_url = secret("oncall_healthchecks_url")
        # Never posted to Discord or a pull request, whatever the agent read in logs or files.
        self.secrets = [secret(name) for name in SECRET_FILES]
        self.owner = discord.AllowedMentions(
            everyone=False, roles=False, users=[discord.Object(self.owner_id)]
        )
        self.quiet = discord.AllowedMentions.none()
        self.state = State.load(self.state_path)
        self.polled_at = 0.0
        self.followers: dict[str, asyncio.Task[None]] = {}
        self.loops: list[asyncio.Task[None]] = []
        self.web: aiohttp.ClientSession

    async def setup_hook(self) -> None:
        self.web = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        self.loops = [
            self.loop.create_task(self.every("alerts", POLL_ALERTS_EVERY, self.poll_alerts)),
            self.loop.create_task(self.every("sweep", 60, self.maybe_sweep)),
            self.loop.create_task(self.every("ping", 60, self.ping)),
        ]

    async def on_ready(self) -> None:
        log("ready", user=str(self.user))
        for sid, incident in self.state.incidents.items():
            if not incident.closed:
                self.follow(sid)

    def save(self) -> None:
        self.state.save(self.state_path, time.time())

    def finish(self, incident: Incident) -> None:
        incident.closed = True
        incident.closed_at = time.time()
        self.save()

    def forum(self) -> discord.ForumChannel:
        channel = self.get_channel(self.forum_id)
        assert isinstance(channel, discord.ForumChannel), "FORUM_CHANNEL_ID is not a forum"
        return channel

    def tags(self, *names: str) -> list[discord.ForumTag]:
        return [t for t in self.forum().available_tags if t.name.lower() in names]

    async def thread(self, incident: Incident) -> discord.Thread | None:
        if incident.thread_id is None:
            return None
        try:
            channel = self.get_channel(incident.thread_id) or await self.fetch_channel(
                incident.thread_id
            )
        except discord.NotFound:
            return None
        return channel if isinstance(channel, discord.Thread) else None

    async def say(self, incident: Incident, text: str, mention: bool = False) -> None:
        thread = await self.thread(incident)
        if thread is None:
            return
        for part in chunks(redact(text, self.secrets)):
            await thread.send(part, allowed_mentions=self.owner if mention else self.quiet)

    async def every(self, name: str, seconds: int, step: Any) -> None:  # noqa: ANN401
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await step()
            except Exception as error:
                log("loop_failed", loop=name, error=failure(error))
            await asyncio.sleep(seconds)

    async def ping(self) -> None:
        # Only while alerts reach the bot: Healthchecks.io posts once the pings stop.
        if time.time() - self.polled_at > 5 * POLL_ALERTS_EVERY:
            return
        async with self.web.get(self.ping_url) as response:
            response.raise_for_status()

    async def new_incident(self, title: str, kind: str, **fields: Any) -> Incident:  # noqa: ANN401
        """A session, recorded before anything is posted or sent, so a failure after this point
        is retried on the same incident rather than opening another."""
        session = await self.anthropic.beta.sessions.create(
            agent=self.agent_id,
            environment_id=self.environment_id,
            title=title,
            metadata={"kind": kind},
            budget={
                "type": "limit",
                "max_list_cost": {"amount": str(BUDGET_CENTS[kind]), "currency": "USD"},
            },
        )
        incident = Incident(session_id=session.id, **fields)
        self.state.incidents[session.id] = incident
        self.save()
        log("session_started", session=session.id, kind=kind, title=title)
        self.follow(session.id)
        return incident

    async def send(self, session_id: str, events: list[Any]) -> None:
        await self.anthropic.beta.sessions.events.send(session_id=session_id, events=events)

    async def message(self, session_id: str, text: str) -> None:
        await self.send(
            session_id, [{"type": "user.message", "content": [{"type": "text", "text": text}]}]
        )

    def follow(self, session_id: str) -> None:
        task = self.followers.get(session_id)
        if task is None or task.done():
            self.followers[session_id] = self.loop.create_task(self.follow_session(session_id))

    async def follow_session(self, session_id: str) -> None:
        incident = self.state.incidents[session_id]
        events = self.anthropic.beta.sessions.events
        wait = 5
        while not incident.closed:
            try:
                # The stream opens before the history is read, so nothing falls between them. It
                # may wait days for the owner, so it has no read timeout.
                async with await events.stream(session_id=session_id, timeout=None) as stream:
                    async for event in events.list(session_id=session_id):
                        await self.handle(incident, event)
                    wait = 5
                    async for event in stream:
                        await self.handle(incident, event)
                        if event.type == "session.status_terminated" or incident.closed:
                            return
            except anthropic.NotFoundError:
                log("session_gone", session=session_id)
                self.finish(incident)
                return
            except Exception as error:
                log("stream_dropped", session=session_id, error=failure(error))
            await asyncio.sleep(wait)
            wait = min(wait * 2, 600)

    async def handle(self, incident: Incident, event: Any) -> None:  # noqa: ANN401 - the SDK's union
        if event.id in incident.seen:
            return
        await self.act(incident, event)
        # Only once handled, so a failure is retried when the stream reconnects.
        incident.seen.append(event.id)
        self.save()

    async def act(self, incident: Incident, event: Any) -> None:  # noqa: ANN401
        sid = incident.session_id
        if event.type == "agent.message":
            text = "".join(b.text for b in event.content if b.type == "text")
            incident.answered = True
            log("agent_message", session=sid, text=redact(text, self.secrets))
            await self.say(incident, text)
        elif event.type == "agent.custom_tool_use":
            log("tool_call", session=sid, tool=event.name, input=event.input)
            try:
                result = await self.run_tool(incident, event.name, event.input)
            except Exception as error:
                result = f"Failed: {failure(error)}"
            log("tool_result", session=sid, tool=event.name, result=result)
            await self.send(
                sid,
                [
                    {
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": event.id,
                        "content": [{"type": "text", "text": result}],
                    }
                ],
            )
        elif event.type in ("agent.tool_use", "agent.mcp_tool_use"):
            log(
                "worker_tool",
                session=sid,
                tool=event.name,
                input=event.input,
                permission=event.evaluated_permission,
            )
            if event.evaluated_permission == "ask":
                incident.pending_confirmation = event.id
                self.save()
                await self.say(
                    incident,
                    f"Wants to run `{event.name}`:\n```\n{json.dumps(event.input)[:1500]}\n```"
                    f"Reply **yes** to allow; anything else refuses. <@{self.owner_id}>",
                    mention=True,
                )
        elif event.type == "session.error":
            log("session_error", session=sid, error=str(event))
            await self.say(incident, "The session hit an error; the oncall logs have it.")
        elif event.type == "session.status_idle":
            stop = event.stop_reason.type
            log("idle", session=sid, stop=stop)
            if stop == "budget_reached":
                await self.say(
                    incident,
                    f"Stopped at its spend cap. <@{self.owner_id}>",
                    mention=True,
                )
            # A new session idles before its first message; a sweep that has answered and found
            # nothing ends with nothing posted.
            if incident.thread_id is None and incident.answered and stop != "requires_action":
                self.finish(incident)

    async def poll_alerts(self) -> None:
        url = (
            f"{self.gateway}/alertmanager/api/v2/alerts?active=true&silenced=false&inhibited=false"
        )
        async with self.web.get(url, auth=self.gateway_auth) as response:
            response.raise_for_status()
            alerts = await response.json()
        self.polled_at = time.time()
        new, again, cleared = reconcile(self.state, alerts)
        for alert in new:
            await self.open_alert(alert)
        for incident in again:
            incident.firing = True
            self.save()
            log("alert_again", session=incident.session_id, fingerprint=incident.fingerprint)
            await self.say(incident, "Firing again.")
            await self.retag(incident)
            await self.message(incident.session_id, "The alert is firing again in Alertmanager.")
        for incident in cleared:
            incident.firing = False
            self.save()
            log("alert_cleared", session=incident.session_id, fingerprint=incident.fingerprint)
            if incident.muted:
                self.finish(incident)
                continue
            await self.say(
                incident,
                "Cleared: the number is back within bounds; that does not mean the cause is fixed.",
            )
            await self.retag(incident)
            await self.message(incident.session_id, "The alert has cleared in Alertmanager.")
        now = time.time()
        for incident in list(self.state.incidents.values()):
            if incident.closed or not incident.firing or incident.muted:
                continue
            if incident.thread_id is None:
                # A post that failed to open opens on a later round, on the same session.
                await self.open_post(incident)
            elif incident.reminded_at and now - incident.reminded_at > REMIND_EVERY:
                incident.reminded_at = now
                self.save()
                await self.say(incident, f"Still firing. <@{self.owner_id}>", mention=True)

    async def open_alert(self, alert: dict[str, Any]) -> None:
        incident = await self.new_incident(
            alert_title(alert),
            "alert",
            fingerprint=alert["fingerprint"],
            firing=True,
            alert={k: alert.get(k) for k in ("labels", "annotations", "startsAt")},
            reminded_at=time.time() if alert["labels"].get("level") == "now" else 0.0,
        )
        await self.open_post(incident)

    async def open_post(self, incident: Incident) -> None:
        alert = incident.alert
        post = await self.forum().create_thread(
            name=alert_title(alert),
            content=alert_post(alert, self.owner_id),
            applied_tags=self.tags(FIRING, WORKING),
            allowed_mentions=self.owner,
        )
        incident.thread_id = post.thread.id
        self.save()
        log("alert_opened", session=incident.session_id, thread=post.thread.id, alert=alert)
        await self.message(
            incident.session_id,
            "An alert is firing. Triage it, fix it if you can, and close the incident.\n"
            f"```json\n{json.dumps(alert)}\n```",
        )

    async def retag(self, incident: Incident) -> None:
        thread = await self.thread(incident)
        if thread is None:
            return
        kept = [t for t in thread.applied_tags if t.name.lower() != FIRING]
        await thread.edit(applied_tags=kept + (self.tags(FIRING) if incident.firing else []))

    async def maybe_sweep(self) -> None:
        if time.time() - self.state.last_sweep < SWEEP_EVERY:
            return
        self.state.last_sweep = time.time()
        self.save()
        incident = await self.new_incident("Anomaly sweep", "sweep")
        await self.message(
            incident.session_id,
            "Sweep: look for anything wrong or about to go wrong that no alert covers. Call "
            "open_incident for a real finding; with none, say so in one line and stop.",
        )

    async def on_message(self, message: discord.Message) -> None:
        channel = message.channel
        if message.author.id != self.owner_id or not isinstance(channel, discord.Thread):
            return
        if channel.parent_id != self.forum_id:
            return
        incident = self.state.by_thread(channel.id)
        if incident is None:
            # A forum post's first message has the post's ID: only a new post starts a session.
            if message.id == channel.id:
                await channel.edit(applied_tags=self.tags(WORKING))
                incident = await self.new_incident(channel.name, "owner", thread_id=channel.id)
                await self.message(incident.session_id, f"{channel.name}\n\n{message.content}")
            return
        if incident.closed:
            incident.closed = False
            incident.closed_at = 0.0
            self.save()
            self.follow(incident.session_id)
        intent = owner_intent(message.content, incident.pending_confirmation)
        log("owner_message", session=incident.session_id, intent=intent, text=message.content)
        if intent == "interrupt":
            # An interrupt also drops a paused call, so the next reply is a message again.
            incident.pending_confirmation = None
            self.save()
            await self.send(incident.session_id, [{"type": "user.interrupt"}])
        elif intent in ("allow", "deny"):
            event: dict[str, Any] = {
                "type": "user.tool_confirmation",
                "tool_use_id": incident.pending_confirmation,
                "result": intent,
            }
            if intent == "deny":
                event["deny_message"] = message.content
            incident.pending_confirmation = None
            self.save()
            await self.send(incident.session_id, [event])
        else:
            await self.message(incident.session_id, message.content)
        await message.add_reaction("👀")

    async def run_tool(self, incident: Incident, name: str, args: dict[str, Any]) -> str:
        if name == "close_incident":
            return await self.close_incident(incident, args["outcome"], args["summary"])
        if name == "restart_container":
            return await self.restart_container(args["name"])
        if name == "open_pull_request":
            return await self.open_pull_request(incident, args)
        if name == "open_incident":
            return await self.open_incident(incident, args["title"], args["summary"])
        return f"There is no tool named {name}."

    async def close_incident(self, incident: Incident, outcome: str, summary: str) -> str:
        thread = await self.thread(incident)
        if thread is None:
            return "There is no post for this incident."
        if outcome == "spam":
            incident.muted = True
            incident.thread_id = None
            self.save()
            if not incident.firing:
                self.finish(incident)
            log("incident_deleted", session=incident.session_id, reason=summary)
            await thread.delete()
            return "Deleted the post; it is not posted again while this alert fires."
        needs_owner = outcome == "needs_you"
        embed = discord.Embed(
            title=outcome.replace("_", " ").capitalize(),
            description=redact(summary, self.secrets)[:4000],
            colour=discord.Colour.orange() if needs_owner else discord.Colour.green(),
        )
        await thread.send(
            content=f"<@{self.owner_id}>" if needs_owner else None,
            embed=embed,
            allowed_mentions=self.owner if needs_owner else self.quiet,
        )
        tags = [NEEDS_OWNER] if needs_owner else []
        done = not needs_owner and not incident.firing
        await thread.edit(
            applied_tags=self.tags(*tags, FIRING) if incident.firing else self.tags(*tags),
            archived=done,
        )
        if done:
            self.finish(incident)
        log("incident_closed", session=incident.session_id, outcome=outcome, archived=done)
        return "Closed." if done else "Posted; the post stays open while the alert fires."

    async def restart_container(self, name: str) -> str:
        if not CONTAINER_NAME.fullmatch(name) or CONTAINER_ID.fullmatch(name):
            return "Give the container's name, not its ID."
        if name.startswith("oncall-"):
            # Restarting this program would drop the tool's own result and stall the session.
            return "The on-call containers cannot restart themselves; ask Colin."
        async with self.web.post(f"{self.docker}/containers/{name}/restart?t=30") as response:
            if response.status == 404:
                return f"No container named {name}."
            response.raise_for_status()
        log("container_restarted", name=name)
        return f"Restarted {name}."

    async def open_pull_request(self, incident: Incident, args: dict[str, Any]) -> str:
        texts = [args["title"], args["body"], *(f["content"] for f in args["files"])]
        if any(holds_secret(text, self.secrets) for text in texts):
            log("pull_request_refused", session=incident.session_id, reason="holds a secret")
            return "Refused: it contains a secret. The repository is public; remove it."
        api = f"https://api.github.com/repos/{self.repo}"
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
        }

        async def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
            async with self.web.request(method, api + path, json=body, headers=headers) as r:
                if r.status >= 400:
                    raise RuntimeError(f"GitHub {method} {path}: {r.status} {await r.text()}")
                return await r.json()

        base = (await call("GET", "/git/ref/heads/main"))["object"]["sha"]
        base_tree = (await call("GET", f"/git/commits/{base}"))["tree"]["sha"]
        tree = [
            {
                "path": check_path(f["path"]),
                "mode": "100644",
                "type": "blob",
                "content": f["content"],
            }
            for f in args["files"]
        ]
        new_tree = await call("POST", "/git/trees", {"base_tree": base_tree, "tree": tree})
        commit = await call(
            "POST",
            "/git/commits",
            {"message": args["title"], "tree": new_tree["sha"], "parents": [base]},
        )
        branch = pr_branch(args["branch"], time.time())
        await call("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        thread = await self.thread(incident)
        link = f"\n\nIncident: {thread.jump_url}" if thread else ""
        pr = await call(
            "POST",
            "/pulls",
            {"title": args["title"], "head": branch, "base": "main", "body": args["body"] + link},
        )
        log("pull_request", session=incident.session_id, url=pr["html_url"])
        return f"Opened {pr['html_url']}. Only the owner merges."

    async def open_incident(self, incident: Incident, title: str, summary: str) -> str:
        if incident.thread_id is not None:
            return "This session already has a post; say it there."
        post = await self.forum().create_thread(
            name=redact(title, self.secrets)[:100],
            content=redact(summary, self.secrets)[:DISCORD_LIMIT],
            applied_tags=self.tags(WORKING),
        )
        incident.thread_id = post.thread.id
        self.save()
        log("finding_opened", session=incident.session_id, thread=post.thread.id, title=title)
        return "Opened a post; your messages now go there."


def main() -> None:
    if len(sys.argv) > 1:
        sys.exit(f"usage: {sys.argv[0]}")
    Oncall().run(secret("discord_bot_token"), log_handler=None)


if __name__ == "__main__":
    main()
