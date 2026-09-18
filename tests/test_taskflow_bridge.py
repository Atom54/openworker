"""TaskFlow bridge — one tick against a fake TaskFlow API. No network, no LLM."""

from __future__ import annotations

import json

import httpx

from coworker.automation import TaskRun, TaskStore
from coworker.taskflow_bridge import tick


class FakeTaskFlow:
    def __init__(self, queue=(), jobs=(), claim_status=200):
        self.queue = list(queue)
        self.jobs = list(jobs)
        self.claim_status = claim_status
        self.posts: list[tuple[int, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/agent/queue":
            return httpx.Response(200, json=self.queue)
        if path == "/agent/jobs":
            return httpx.Response(200, json=self.jobs)
        if path.startswith("/agent/jobs/") and request.method == "POST":
            body = json.loads(request.content)
            self.posts.append((int(path.rsplit("/", 1)[1]), body))
            status = self.claim_status if body["status"] == "in_progress" else 200
            return httpx.Response(status, json={})
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler),
            base_url="http://taskflow.test",
        )


def _idle(session_id: str) -> bool:
    return False


def _scratch(session_id: str) -> str:
    return f"/scratch/{session_id}"


QUEUED = {"task_id": 7, "title": "Fix login", "notes": "See #12", "workspace": "/tmp/eloqa"}


async def test_claims_a_queued_task_and_launches_it(tmp_path):
    store, launched = TaskStore(tmp_path / "a.db"), []
    fake = FakeTaskFlow(queue=[QUEUED])
    async with fake.client() as client:
        await tick(store, launched.append, client, _scratch, _idle)

    [task] = store.list()
    assert task.workspace == "/tmp/eloqa"
    assert not task.enabled, "the scheduler must never fire it on its own"
    assert "Fix login" in task.instructions and "See #12" in task.instructions
    assert "7391" not in task.instructions, "no API pointer without images"
    assert fake.posts == [(7, {"status": "in_progress", "openworker_id": task.id})]
    assert [t.id for t in launched] == [task.id]


async def test_list_without_folder_gets_a_scratch_dir(tmp_path):
    store = TaskStore(tmp_path / "a.db")
    fake = FakeTaskFlow(queue=[{**QUEUED, "workspace": None}])
    async with fake.client() as client:
        await tick(store, lambda t: None, client, _scratch, _idle)
    [task] = store.list()
    assert task.workspace == f"/scratch/{task.task_session_id}"
    assert f"Tu travailles dans /scratch/{task.task_session_id}" in task.instructions


async def test_lost_claim_leaves_no_trace(tmp_path):
    store, launched = TaskStore(tmp_path / "a.db"), []
    fake = FakeTaskFlow(queue=[QUEUED], claim_status=409)
    async with fake.client() as client:
        await tick(store, launched.append, client, _scratch, _idle)
    assert store.list() == [] and launched == []


async def _reconcile(tmp_path, run_status, job_status="in_progress", waiting=_idle, **run_fields):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = TaskStore(tmp_path / "a.db")
    fake = FakeTaskFlow(queue=[QUEUED])
    async with fake.client() as client:
        await tick(store, lambda t: None, client, _scratch, _idle)
    [task] = store.list()
    if run_status:
        store.add_run(TaskRun(task_id=task.id, status=run_status, **run_fields))
    fake.queue, fake.posts = [], []
    fake.jobs = [{"task_id": 7, "status": job_status, "openworker_id": task.id}]
    async with fake.client() as client:
        await tick(store, lambda t: None, client, _scratch, waiting)
    return fake.posts


async def test_finished_run_is_reported_for_review(tmp_path):
    posts = await _reconcile(tmp_path, "ok", result_text="Done, see PR #3.")
    assert posts == [(7, {"status": "review", "report": "Done, see PR #3."})]


async def test_failed_run_is_reported_too(tmp_path):
    posts = await _reconcile(tmp_path, "error", error="model unavailable")
    assert posts == [(7, {"status": "review", "report": "Erreur : model unavailable"})]


async def test_run_waiting_on_an_approval_is_flagged_then_released(tmp_path):
    parked = lambda session_id: session_id.startswith("__run__")
    assert await _reconcile(tmp_path / "a", "running", waiting=parked) == [
        (7, {"status": "blocked"})
    ]
    assert await _reconcile(tmp_path / "b", "running", job_status="blocked") == [
        (7, {"status": "in_progress"})
    ]
    assert await _reconcile(tmp_path / "c", "running", job_status="blocked", waiting=parked) == []


async def test_blocked_run_that_finishes_goes_to_review(tmp_path):
    posts = await _reconcile(tmp_path, "ok", job_status="blocked", result_text="ok")
    assert posts == [(7, {"status": "review", "report": "ok"})]


async def test_running_or_missing_run_waits(tmp_path):
    assert await _reconcile(tmp_path, "running") == []
    assert await _reconcile(tmp_path / "b", None) == []


async def test_taskflow_closed_is_silent(tmp_path):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://x")
    async with client:
        await tick(TaskStore(tmp_path / "a.db"), lambda t: None, client, _scratch, _idle)
