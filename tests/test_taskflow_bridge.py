"""TaskFlow bridge — ticks against a fake TaskFlow API and a fake host. No network, no LLM."""

from __future__ import annotations

import json

import httpx

from coworker.taskflow_bridge import INTERRUPTED, tick


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
            transport=httpx.MockTransport(self.handler), base_url="http://taskflow.test"
        )


class FakeHost:
    def __init__(self, running=False, waiting=False, reply=None):
        self.running, self.parked, self.reply = running, waiting, reply
        self.opened: list[tuple] = []
        self.dropped: list[str] = []
        self.launched: list[tuple[str, str]] = []

    def open_session(self, title, workspace, task_id):
        self.opened.append((title, workspace, task_id))
        return f"sess-{task_id}"

    def drop_session(self, session_id):
        self.dropped.append(session_id)

    def launch(self, session_id, message):
        self.launched.append((session_id, message))

    def is_running(self, session_id):
        return self.running

    def waiting(self, session_id):
        return self.parked

    def last_reply(self, session_id):
        return self.reply


QUEUED = {"task_id": 7, "title": "Fix login", "notes": "See #12", "workspace": "/tmp/eloqa"}


async def _tick(host, fake):
    async with fake.client() as client:
        await tick(host, client)
    return fake.posts


async def test_claims_a_queued_task_as_a_conversation():
    host, fake = FakeHost(), FakeTaskFlow(queue=[QUEUED])
    posts = await _tick(host, fake)

    assert host.opened == [("Fix login", "/tmp/eloqa", 7)]
    assert posts == [(7, {"status": "in_progress", "openworker_id": "sess-7"})]
    [(session_id, message)] = host.launched
    assert session_id == "sess-7"
    assert message.startswith("Fix login\n\nSee #12")
    assert "Scheduled" not in message
    assert "7391" not in message, "no API pointer without images"


async def test_images_in_notes_get_the_assets_url():
    host = FakeHost()
    item = {**QUEUED, "notes": "![](asset://localhost/2026/09/a.png)"}
    await _tick(host, FakeTaskFlow(queue=[item]))
    assert "127.0.0.1:7391/assets/" in host.launched[0][1]


async def test_list_without_folder_passes_no_workspace():
    host = FakeHost()
    await _tick(host, FakeTaskFlow(queue=[{**QUEUED, "workspace": None}]))
    assert host.opened == [("Fix login", None, 7)]


async def test_lost_claim_drops_the_session():
    host = FakeHost()
    await _tick(host, FakeTaskFlow(queue=[QUEUED], claim_status=409))
    assert host.dropped == ["sess-7"] and host.launched == []


def _job(status="in_progress"):
    return {"task_id": 7, "status": status, "openworker_id": "sess-7"}


async def test_finished_run_is_reported_for_review():
    posts = await _tick(FakeHost(reply="Done, see PR #3."), FakeTaskFlow(jobs=[_job()]))
    assert posts == [(7, {"status": "review", "report": "Done, see PR #3."})]


async def test_run_stopped_without_reply_is_still_reported():
    posts = await _tick(FakeHost(reply=None), FakeTaskFlow(jobs=[_job("blocked")]))
    assert posts == [(7, {"status": "review", "report": INTERRUPTED})]


async def test_run_waiting_on_an_approval_is_flagged_then_released():
    parked = FakeHost(running=True, waiting=True)
    assert await _tick(parked, FakeTaskFlow(jobs=[_job()])) == [(7, {"status": "blocked"})]
    assert await _tick(parked, FakeTaskFlow(jobs=[_job("blocked")])) == []

    busy = FakeHost(running=True)
    assert await _tick(busy, FakeTaskFlow(jobs=[_job("blocked")])) == [
        (7, {"status": "in_progress"})
    ]
    assert await _tick(busy, FakeTaskFlow(jobs=[_job()])) == []


async def test_other_jobs_are_left_alone():
    jobs = [_job("queued"), _job("review"), {"task_id": 8, "status": "in_progress"}]
    assert await _tick(FakeHost(reply="x"), FakeTaskFlow(jobs=jobs)) == []


async def test_taskflow_closed_is_silent():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://x") as c:
        await tick(FakeHost(), c)


def test_manager_host_opens_a_visible_unattended_conversation(tmp_path, monkeypatch):
    from coworker.server.manager import SessionManager
    from coworker.taskflow_bridge import ManagerHost

    monkeypatch.setenv("COWORKER_SCRATCH_BASE", str(tmp_path / "scratch"))
    repo = tmp_path / "repo"
    repo.mkdir()
    m = SessionManager(data_dir=tmp_path / "data", workspace=str(tmp_path))
    host = ManagerHost(m)

    sid = host.open_session("Écrire le post", None, 42)
    rec = m.session_store.load(sid)
    assert rec.title == "Écrire le post"
    assert rec.origin == "taskflow"
    assert rec.workspace.startswith(str(tmp_path / "scratch")), "no folder → scratch"
    assert m.unattended.is_unattended(sid)
    assert not host.is_running(sid) and not host.waiting(sid)
    assert host.last_reply(sid) is None

    in_repo = host.open_session("Fix", str(repo), 43)
    assert m.session_store.load(in_repo).workspace == str(repo.resolve())

    host.drop_session(sid)
    assert m.session_store.load(sid) is None
