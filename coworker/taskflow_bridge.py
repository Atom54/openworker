"""TaskFlow bridge — run tasks the user sends from TaskFlow, report back for review.

TaskFlow (the user's desktop kanban) listens on a fixed port; this server does
not, so TaskFlow never pushes: every scheduler tick pulls its queue instead.

A queued task becomes a *disabled* one-shot automation in the folder TaskFlow
linked to its list, or in a fresh scratch dir when the list has none —
disabled so `due()` never fires it, but listed with its transcript like any
other automation. TaskFlow's 409 on the claim is the lock
against two ticks taking the same task.

No in-memory state: completion is reconciled from TaskFlow's in-progress jobs
and this store's last run, so a report survives either app being closed. The
agent never marks a task done — TaskFlow's user does, after reading the report.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

import httpx

from .automation import Schedule, ScheduledTask, TaskStore

logger = logging.getLogger("coworker.taskflow")

TASKFLOW_URL = "http://127.0.0.1:7391"

Launch = Callable[[ScheduledTask], object]
Scratch = Callable[[str], str]  # session id → provisioned scratch dir


def _instructions(item: dict, workspace: str) -> str:
    notes = (item.get("notes") or "").strip()
    # Everything the agent needs is inline. Pointing it at TaskFlow's API made it
    # re-read the task and write the notes itself — a shell call that parks an
    # unattended run on an approval, and a second writer on the notes.
    images = (
        f"Les images des notes (asset://localhost/…) se lisent via {TASKFLOW_URL}/assets/….\n"
        if "asset://" in notes
        else ""
    )
    return (
        f"Tâche : {item['title']}\n\n"
        + (f"{notes}\n\n" if notes else "")
        + f"Tu travailles dans {workspace}.\n"
        + images
        + "Tout le contexte est ci-dessus : ne consulte ni ne modifie TaskFlow. Ton dernier "
        "message est ajouté automatiquement aux notes de la tâche comme rapport : mets-y le "
        "résultat lui-même, puis en français ce qui a été fait, ce qui reste et comment vérifier."
    )


async def tick(
    store: TaskStore, launch: Launch, client: httpx.AsyncClient, scratch: Scratch
) -> None:
    try:
        queue = (await client.get("/agent/queue")).json()
        jobs = (await client.get("/agent/jobs")).json()
    except httpx.HTTPError:
        return  # TaskFlow closed: normal, try again next tick

    for item in queue:
        task = ScheduledTask(
            title=item["title"],
            instructions="",
            schedule=Schedule(kind="once", fire_at=datetime.now().isoformat()),
            workspace=item.get("workspace") or "",
            origin_surface="taskflow",
            enabled=False,
        )
        if not task.workspace:
            task.workspace = scratch(task.task_session_id)
        task.instructions = _instructions(item, task.workspace)
        store.save(task)
        claim = await client.post(
            f"/agent/jobs/{item['task_id']}",
            json={"status": "in_progress", "openworker_id": task.id},
        )
        if claim.status_code != 200:
            store.delete(task.id)  # lost the race, or TaskFlow refused it
            continue
        launch(task)

    for job in jobs:
        if job.get("status") != "in_progress" or not job.get("openworker_id"):
            continue
        runs = store.runs(job["openworker_id"], limit=1)
        if not runs or runs[0].status == "running":
            continue
        run = runs[0]
        report = (
            run.result_text or "(pas de rapport)"
            if run.status == "ok"
            else f"Erreur : {run.error or run.status}"
        )
        await client.post(
            f"/agent/jobs/{job['task_id']}", json={"status": "review", "report": report}
        )
