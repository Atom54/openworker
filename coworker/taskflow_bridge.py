"""TaskFlow bridge — run tasks the user sends from TaskFlow, report back for review.

TaskFlow (the user's desktop kanban) listens on a fixed port; this server does
not, so TaskFlow never pushes: every scheduler tick pulls its queue instead.

A queued task becomes an ordinary conversation (listed in Recents, continuable
like any chat), opened unattended so its approvals park in the Inbox, in the
folder TaskFlow linked to the task's list — or a scratch dir when there is none.
TaskFlow's 409 on the claim is the lock against two ticks taking the same task.

No in-memory state: each tick reconciles TaskFlow's in-progress jobs against
the sessions themselves. Running and parked on an approval → `blocked`; running
again → `in_progress`; idle → `review`, with the last assistant message as the
report. A session left idle by a restart mid-run is reported too, so a task is
never stuck. The agent never marks a task done — TaskFlow's user does.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

import httpx

logger = logging.getLogger("coworker.taskflow")

TASKFLOW_URL = "http://127.0.0.1:7391"

INTERRUPTED = (
    "Le run s'est arrêté sans réponse (OpenWorker redémarré ?). "
    "Voir la conversation dans OpenWorker."
)


class Host(Protocol):
    """What the bridge needs from the server — see `ManagerHost`."""

    def open_session(self, title: str, workspace: Optional[str], task_id: int) -> str: ...
    def drop_session(self, session_id: str) -> None: ...
    def launch(self, session_id: str, message: str) -> None: ...
    def is_running(self, session_id: str) -> bool: ...
    def waiting(self, session_id: str) -> bool: ...
    def last_reply(self, session_id: str) -> Optional[str]: ...


def _instructions(item: dict) -> str:
    notes = (item.get("notes") or "").strip()
    # Everything the agent needs is inline. Pointing it at TaskFlow's API made it
    # re-read the task and write the notes itself — a shell call that parks an
    # unattended run on an approval, and a second writer on the notes.
    images = (
        f"Les images des notes (asset://localhost/…) se lisent via {TASKFLOW_URL}/assets/….\n\n"
        if "asset://" in notes
        else ""
    )
    return (
        f"{item['title']}\n\n"
        + (f"{notes}\n\n" if notes else "")
        + images
        + "(Tâche envoyée depuis TaskFlow. Tout le contexte est ci-dessus : ne consulte ni ne "
        "modifie TaskFlow. Ton dernier message est ajouté automatiquement aux notes de la tâche "
        "comme rapport : mets-y le résultat lui-même, puis en français ce qui a été fait, ce qui "
        "reste et comment vérifier.)"
    )


async def tick(host: Host, client: httpx.AsyncClient) -> None:
    try:
        queue = (await client.get("/agent/queue")).json()
        jobs = (await client.get("/agent/jobs")).json()
    except httpx.HTTPError:
        return  # TaskFlow closed: normal, try again next tick

    for item in queue:
        session_id = host.open_session(item["title"], item.get("workspace"), item["task_id"])
        claim = await client.post(
            f"/agent/jobs/{item['task_id']}",
            json={"status": "in_progress", "openworker_id": session_id},
        )
        if claim.status_code != 200:
            host.drop_session(session_id)  # lost the race, or TaskFlow refused it
            continue
        host.launch(session_id, _instructions(item))

    for job in jobs:
        status, session_id = job.get("status"), job.get("openworker_id")
        if status not in ("in_progress", "blocked") or not session_id:
            continue
        if host.is_running(session_id):
            # Unattended runs never self-approve: a parked ask shows in TaskFlow
            # as "to approve" until the user answers it in OpenWorker.
            parked = host.waiting(session_id)
            if parked != (status == "blocked"):
                await client.post(
                    f"/agent/jobs/{job['task_id']}",
                    json={"status": "blocked" if parked else "in_progress"},
                )
            continue
        report = host.last_reply(session_id) or INTERRUPTED
        await client.post(
            f"/agent/jobs/{job['task_id']}", json={"status": "review", "report": report}
        )


class ManagerHost:
    """`Host` over the server's SessionManager — the same calls a Slack mention
    uses to spawn a visible background session (`_spawn_mention_session`)."""

    def __init__(self, manager) -> None:
        self.m = manager

    def open_session(self, title: str, workspace: Optional[str], task_id: int) -> str:
        import uuid

        session_id = uuid.uuid4().hex
        # No folder → this conversation's scratch dir. Explicitly: get_engine
        # would fall back to the app's default workspace, not a scratch dir.
        engine = self.m.get_engine(
            session_id,
            workspace=workspace or self.m._provision_scratch(session_id),
            agent=self.m.personas.default_id(),
        )
        if engine is None:
            raise RuntimeError(f"could not open a session for TaskFlow task {task_id}")
        self.m.save(session_id, engine)  # the row must exist before rename/set_origin
        self.m.session_store.rename(session_id, title[:80])
        self.m.session_store.set_origin(session_id, "taskflow", f"TaskFlow #{task_id}")
        self.m.set_unattended(session_id, True)  # approvals → the Inbox
        return session_id

    def drop_session(self, session_id: str) -> None:
        self.m.delete_session(session_id)

    def launch(self, session_id: str, message: str) -> None:
        import asyncio

        # Spawned, not awaited: a run lasts minutes and must not stall the tick.
        run = asyncio.create_task(
            self.m.deliver_to_session(session_id, message), name=session_id
        )
        self.m._taskflow_runs.add(run)
        run.add_done_callback(self.m._taskflow_runs.discard)

    def is_running(self, session_id: str) -> bool:
        # A spawned run counts from the spawn on, before its first step marks
        # the session running — hence the check on our own live tasks too.
        return self.m.is_running(session_id) or any(
            t.get_name() == session_id and not t.done() for t in self.m._taskflow_runs
        )

    def waiting(self, session_id: str) -> bool:
        return bool(self.m.inbox.pending(session_id))

    def last_reply(self, session_id: str) -> Optional[str]:
        from .server.manager import _last_assistant_text

        record = self.m.session_store.load(session_id)
        return _last_assistant_text(record.messages) if record else None
