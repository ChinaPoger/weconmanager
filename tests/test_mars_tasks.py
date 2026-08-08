"""从 mars 拉任务的契约 —— 不联网、不碰真实客户端。

mars 整体用 httpx.MockTransport 假掉,执行侧(官方 API / 本地客户端)打桩。
测的是那几条不能退化的取舍:白名单、过期、幂等(宁可漏发)、失败语义。
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from wecom_svr import mars_tasks
from wecom_svr.main import create_app
from wecom_svr.mars_tasks import MarsTasksNotConfigured, Outcome, State, Task
from wecom_svr.settings import Settings, get_agent_home, get_settings
from wecom_svr.uilock import ui_session

TOKEN = "t0ken"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("WECOM_SVR_API_TOKEN", "WECOM_SVR_MARS_BASE_URL", "WECOM_SVR_MARS_TASKS_TOKEN",
              "WECOM_SVR_MARS_TASKS_ENABLED", "WECOM_SVR_MARS_TASKS_ACTIONS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WECOM_SVR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_agent_home.cache_clear()
    yield
    get_settings.cache_clear()
    get_agent_home.cache_clear()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        mars_base_url="https://ts.qiniu.io/",
        mars_tasks_token=TOKEN,
        mars_tasks_worker="mac-test",
    )


class FakeMars:
    """一个只认 /pull 和 /:id/ack 的假 mars。"""

    def __init__(self) -> None:
        self.queue: list[dict] = []
        self.pulls: list[dict] = []
        self.starts: list[tuple[int, dict]] = []
        self.acks: list[tuple[int, dict]] = []
        self.ack_fails = False
        self.stale: set[int] = set()      # 这些任务的令牌已失效(mars 回 409)

    def give(self, id: int, action: str, payload: dict, **kw) -> None:
        kw.setdefault("lease_token", f"mac-test:tok{id}")
        self.queue.append({"id": id, "action": action, "payload": payload, **kw})

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.url.path.startswith("/api/internal/wecom/tasks")
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("/pull"):
            self.pulls.append(body)
            batch, self.queue = self.queue[: body.get("max", 5)], self.queue[body.get("max", 5):]
            return httpx.Response(200, json={"tasks": batch})
        if request.url.path.endswith("/start"):
            task_id = int(request.url.path.split("/")[-2])
            if task_id in self.stale:
                return httpx.Response(409, text="stale lease")
            self.starts.append((task_id, body))
            return httpx.Response(200, json={"id": task_id, "status": 5})
        if request.url.path.endswith("/ack"):
            task_id = int(request.url.path.split("/")[-2])
            if task_id in self.stale:
                return httpx.Response(409, text="stale lease")
            if self.ack_fails:
                return httpx.Response(502, text="mars 挂了")
            self.acks.append((task_id, body))
            return httpx.Response(200, json={"errcode": 0})
        return httpx.Response(404, text="no route")

    @property
    def last_ack(self) -> dict:
        return self.acks[-1][1]


@pytest.fixture
def mars(monkeypatch):
    fake = FakeMars()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    # run_once 会优先复用循环持有的常驻客户端 —— 借这个口子把假 mars 挂进去
    monkeypatch.setattr(mars_tasks.loop, "client", client)
    yield fake
    monkeypatch.setattr(mars_tasks.loop, "client", None)


@pytest.fixture
def app_send(monkeypatch):
    """官方 API 打桩。"""
    calls = []

    async def fake(category, method, args):
        calls.append((category, method, args))
        return {"errcode": 0, "errmsg": "ok", "msgid": "M1"}

    monkeypatch.setattr(mars_tasks, "call_wecom", fake)
    return calls


@pytest.fixture
def local(monkeypatch):
    """本地客户端打桩。"""
    calls = []

    def fake(to="", content="", conv_rid=None):
        calls.append({"to": to, "content": content, "conv_rid": conv_rid})
        return {"success": True, "conv_name": to or str(conv_rid)}

    monkeypatch.setattr(mars_tasks.local_send, "send_message", fake)
    return calls


# ─── 配置 ───────────────────────────────────────────────────────────────

def test_missing_base_url_is_a_config_error(tmp_path):
    with pytest.raises(MarsTasksNotConfigured, match="mars 地址"):
        mars_tasks.tasks_url(Settings(data_dir=tmp_path, mars_tasks_token=TOKEN))


def test_missing_token_is_a_config_error(tmp_path):
    with pytest.raises(MarsTasksNotConfigured, match="token"):
        mars_tasks.tasks_url(Settings(data_dir=tmp_path, mars_base_url="https://ts.qiniu.io"))


def test_url_is_the_internal_route(settings):
    assert mars_tasks.tasks_url(settings, "/pull") == \
        "https://ts.qiniu.io/api/internal/wecom/tasks/pull"


def test_worker_defaults_to_hostname(tmp_path):
    s = Settings(data_dir=tmp_path, mars_base_url="x", mars_tasks_token=TOKEN)
    # 两台 Mac 共用一个 worker 名会各拿走一半任务,所以默认必须是机器相关的
    assert mars_tasks.worker_name(s)
    assert mars_tasks.worker_name(s) != "mac"


def test_wait_is_capped_below_the_nginx_read_timeout(settings):
    assert mars_tasks.pull_wait(settings, 999) == mars_tasks.MAX_WAIT
    assert mars_tasks.pull_wait(settings, -1) == 0.0


def test_pull_body_carries_worker_and_lease(settings, mars):
    run(mars_tasks.run_once(settings, wait=0))
    assert mars.pulls == [{"worker": "mac-test", "max": 1, "wait": 0.0, "lease": 120}]


# ─── 白名单 ─────────────────────────────────────────────────────────────

def test_app_message_is_allowed_by_default(settings, mars, app_send):
    mars.give(1, "message.send", {"touser": "zhangsan", "content": "hi"})
    result = run(mars_tasks.run_once(settings, wait=0))

    assert (result.pulled, result.done, result.failed) == (1, 1, 0)
    # 动手之前先把任务推进 executing,带着认领时拿到的令牌
    assert mars.starts == [(1, {"lease_token": "mac-test:tok1"})]
    assert app_send == [("message", "text", {"touser": "zhangsan", "content": "hi"})]
    assert mars.acks[0][0] == 1
    assert mars.last_ack["ok"] is True


def test_local_send_is_refused_unless_whitelisted(settings, mars, local):
    """local.* 是以本人身份说话,默认不给远端队列驱动。"""
    mars.give(1, "local.message.send", {"to": "张三", "content": "hi"})
    result = run(mars_tasks.run_once(settings, wait=0))

    assert local == []                      # 关键:根本没执行
    assert result.failed == 1
    assert mars.last_ack["ok"] is False
    assert mars.last_ack["retry"] is False
    assert "白名单" in mars.last_ack["error"]


def test_whitelisted_local_send_runs(settings, mars, local):
    settings = settings.model_copy(update={"mars_tasks_actions": "local.message.send"})
    mars.give(1, "local.message.send", {"to": "张三", "content": "hi"})
    run(mars_tasks.run_once(settings, wait=0))

    assert local == [{"to": "张三", "content": "hi", "conv_rid": None}]
    assert mars.last_ack["ok"] is True


def test_star_allows_every_known_action(settings):
    s = settings.model_copy(update={"mars_tasks_actions": "*"})
    assert mars_tasks.allowed_actions(s) == set(mars_tasks.ACTIONS)


def test_unknown_action_never_reaches_the_whitelist_check(settings, mars):
    mars.give(1, "rm.-rf", {})
    run(mars_tasks.run_once(settings, wait=0))
    assert "不认识的 action" in mars.last_ack["error"]


# ─── 执行语义 ───────────────────────────────────────────────────────────

def test_expired_task_is_not_executed(settings, mars, app_send, monkeypatch):
    """Mac 睡了三小时再醒,不该把三小时前的告警喷出去。"""
    monkeypatch.setattr(mars_tasks.time, "time", lambda: 2000.0)
    mars.give(1, "message.send", {"touser": "z", "content": "hi"}, expire_at=1000)
    run(mars_tasks.run_once(settings, wait=0))

    assert app_send == []
    assert mars.last_ack["ok"] is False
    assert "过期" in mars.last_ack["error"]


def test_bad_payload_fails_without_calling_the_handler(settings, mars, app_send):
    """队列这条路和 HTTP 那条路共用同一套 schema,不会更松。"""
    mars.give(1, "message.send", {"touser": "z"})    # 缺 content
    run(mars_tasks.run_once(settings, wait=0))

    assert app_send == []
    assert mars.last_ack["ok"] is False
    assert "payload 校验失败" in mars.last_ack["error"]


def test_app_api_errcode_is_a_failure(settings, mars, monkeypatch):
    """官方 API 失败时 HTTP 也是 200,只看状态码会把失败当成功回报。"""
    async def fail(category, method, args):
        return {"errcode": 40003, "errmsg": "invalid userid"}

    monkeypatch.setattr(mars_tasks, "call_wecom", fail)
    mars.give(1, "message.send", {"touser": "nobody", "content": "hi"})
    result = run(mars_tasks.run_once(settings, wait=0))

    assert result.failed == 1
    assert mars.last_ack["ok"] is False
    assert mars.last_ack["error"] == "invalid userid"


def test_local_failure_body_is_a_failure(settings, mars, monkeypatch):
    settings = settings.model_copy(update={"mars_tasks_actions": "local.message.send"})
    monkeypatch.setattr(
        mars_tasks.local_send, "send_message",
        lambda **kw: {"success": False, "error": "没找到会话"},
    )
    mars.give(1, "local.message.send", {"to": "查无此人", "content": "hi"})
    run(mars_tasks.run_once(settings, wait=0))

    assert mars.last_ack["ok"] is False
    assert mars.last_ack["error"] == "没找到会话"


def test_client_busy_is_reported_before_start(settings, mars):
    """界面被占着 → 必须在 /start **之前**就退回队列。

    这是协议里唯一安全的 retry:任务还是 leased,一条消息都没发过。
    等进了 executing 再喊忙,mars 就分不清「没发」和「发了一半」了。
    """
    settings = settings.model_copy(update={"mars_tasks_actions": "local.message.send"})
    mars.give(1, "local.message.send", {"to": "张三", "content": "hi"})

    with ui_session("别的操作占着界面"):
        result = run(mars_tasks.run_once(settings, wait=0))

    assert mars.starts == []                 # 关键:从没进过 executing
    assert mars.last_ack["ok"] is False
    assert mars.last_ack["retry"] is True
    # 这一轮"因为忙而退回"要能被数出来 —— 后台循环靠它决定歇一下,
    # 否则任务立刻可再领 + 本轮不阻塞在长轮询上 = 以网络速度空转
    assert result.busy == 1


def test_one_failure_does_not_stop_the_batch(settings, mars, app_send):
    """默认一次只领一条(见 mars_tasks_max),但批量领的时候一条失败不该带停后面的。"""
    settings = settings.model_copy(update={"mars_tasks_max": 2})
    mars.give(1, "message.send", {"touser": "z"})            # 校验失败
    mars.give(2, "message.send", {"touser": "z", "content": "hi"})
    result = run(mars_tasks.run_once(settings, wait=0))

    assert (result.done, result.failed) == (1, 1)
    assert [a[0] for a in mars.acks] == [1, 2]


# ─── 令牌(fencing)────────────────────────────────────────────────────

def test_stale_lease_at_start_means_never_execute(settings, mars, app_send):
    """本机卡了太久,任务已经被回收并重新认领 —— 这时候动手就是发第二遍。"""
    mars.give(1, "message.send", {"touser": "z", "content": "hi"})
    mars.stale.add(1)
    result = run(mars_tasks.run_once(settings, wait=0))

    assert app_send == []
    assert mars.acks == []                  # 也不该回报,那条已经不归我们管了
    assert result.dropped == 1
    assert mars_tasks.load_state(settings).inflight is None


def test_stale_lease_at_ack_drops_the_result(settings, mars, app_send):
    """start 过了但 ack 时租约失效:结果作废,别反复重试。"""
    mars.give(1, "message.send", {"touser": "z", "content": "hi"})
    original = mars.handle

    def go_stale(request):
        if request.url.path.endswith("/ack"):
            mars.stale.add(1)
        return original(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(go_stale))
    mars_tasks.loop.client = client
    result = run(mars_tasks.run_once(settings, wait=0))

    assert len(app_send) == 1               # 已经发出去了
    assert result.dropped == 1
    assert mars_tasks.load_state(settings).inflight is None


def test_start_is_skipped_for_tasks_judged_dead(settings, mars, app_send):
    """过期/不在白名单的任务一条消息都不会发,没必要进 executing。"""
    mars.give(1, "rm.-rf", {})
    run(mars_tasks.run_once(settings, wait=0))
    assert mars.starts == []
    assert mars.last_ack["ok"] is False


# ─── 幂等:宁可漏发,不重发 ─────────────────────────────────────────────

def test_ack_failure_keeps_inflight_for_the_next_round(settings, mars, app_send):
    mars.give(1, "message.send", {"touser": "z", "content": "hi"})
    mars.ack_fails = True
    result = run(mars_tasks.run_once(settings, wait=0))

    assert result.error                                   # 本轮停在这
    flight = mars_tasks.load_state(settings).inflight
    assert flight["id"] == 1
    assert flight["outcome"]["ok"] is True                 # 结果已落盘

    # 下一轮补 ack,而不是重新发一遍
    mars.ack_fails = False
    run(mars_tasks.run_once(settings, wait=0))
    assert len(app_send) == 1
    assert mars.acks == [(1, {"ok": True, "retry": False, "error": "",
                             "result": {"errcode": 0, "errmsg": "ok", "msgid": "M1"},
                             "lease_token": "mac-test:tok1"})]
    assert mars_tasks.load_state(settings).inflight is None


def test_crash_mid_send_is_settled_as_sent_not_retried(settings, mars, app_send):
    """没有 outcome 的 inflight = 崩在执行途中,不知道发出去没有。

    重发是对方屏幕上多一条消息,漏发只是少一条通知 —— 所以按已发送结案。
    """
    mars_tasks.save_state(State(inflight={
        "id": 7, "action": "message.send", "phase": "started", "token": "mac-test:tok7",
    }), settings)
    run(mars_tasks.run_once(settings, wait=0))

    assert app_send == []                                  # 绝不重投
    assert mars.acks[0][0] == 7
    assert mars.last_ack["ok"] is False
    assert mars.last_ack["retry"] is False
    assert "不重投" in mars.last_ack["error"]
    assert mars_tasks.load_state(settings).inflight is None


def test_nothing_is_persisted_before_the_outcome_exists(settings, mars, monkeypatch):
    """动手当中本机不记任何东西 —— 崩了由 mars 的 reaper 判「结果未知」。

    本机再记一遍「我做到哪了」是冗余的:mars 那边 executing + 租约超时已经是
    同一个结论。少记一次,就少一个能写坏、能被并发读岔的状态。
    """
    settings = settings.model_copy(update={"mars_tasks_actions": "local.message.send"})
    seen = {}

    def send(**kw):
        seen["inflight"] = mars_tasks.load_state(settings).inflight
        return {"success": True}

    monkeypatch.setattr(mars_tasks.local_send, "send_message", send)
    mars.give(1, "local.message.send", {"to": "张三", "content": "hi"})
    run(mars_tasks.run_once(settings, wait=0))

    assert seen["inflight"] is None          # 执行途中本机什么都没落盘
    assert mars.starts == [(1, {"lease_token": "mac-test:tok1"})]   # 但 mars 知道它在执行
    assert mars.last_ack["ok"] is True


def test_state_file_is_written_atomically(settings):
    """崩在写状态文件中间会留下半截 JSON,而读不动就等于丢了 inflight 记录。"""
    state = State(done=3, inflight={"id": 1, "outcome": {"ok": True}})
    mars_tasks.save_state(state, settings)

    # 落盘期间不留残骸,读回来也不掉字段
    leftovers = list(settings.mars_tasks_state_file.parent.glob(".mars-tasks-*"))
    assert leftovers == []
    assert mars_tasks.load_state(settings) == state


def test_counters_accumulate_across_rounds(settings, mars, app_send):
    mars.give(1, "message.send", {"touser": "z", "content": "hi"})
    run(mars_tasks.run_once(settings, wait=0))
    mars.give(2, "message.send", {"touser": "z"})           # 失败
    run(mars_tasks.run_once(settings, wait=0))

    state = mars_tasks.load_state(settings)
    assert (state.done, state.failed) == (1, 1)
    assert state.last_run > 0


# ─── 解析 ───────────────────────────────────────────────────────────────

def test_non_object_payload_becomes_empty_not_a_crash(settings):
    # mars 那边 payload 是 json.RawMessage,正常一定是个对象;不是对象也别炸,
    # 交给 precheck 的 schema 校验去报「缺哪个字段」
    assert Task.from_dict({"id": 1, "action": "x", "payload": "{oops"}).payload == {}
    assert Task.from_dict({"id": 1, "action": "x", "payload": None}).payload == {}
    assert Task.from_dict({"id": 1, "action": "x", "payload": [1, 2]}).payload == {}


def test_state_survives_a_corrupt_file(settings):
    settings.mars_tasks_state_file.parent.mkdir(parents=True, exist_ok=True)
    settings.mars_tasks_state_file.write_text("{ not json", encoding="utf-8")
    assert mars_tasks.load_state(settings) == State()


def test_verdict_tolerates_a_non_dict_result():
    assert mars_tasks._verdict("x", "boom").ok is False


def test_outcome_ack_body_shape():
    assert Outcome(True, result={"a": 1}).to_ack("tok") == \
        {"ok": True, "retry": False, "error": "", "result": {"a": 1}, "lease_token": "tok"}


# ─── 接口 ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WECOM_SVR_MARS_BASE_URL", "https://ts.qiniu.io")
    monkeypatch.setenv("WECOM_SVR_MARS_TASKS_TOKEN", TOKEN)
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c


def test_status_reports_config_without_leaking_the_token(client):
    body = client.get("/api/v1/mars/tasks/status").json()
    assert body["configured"] is True
    assert body["config"]["token_configured"] is True
    assert TOKEN not in json.dumps(body)
    assert body["config"]["allowed_actions"] == ["message.send"]
    assert body["running"] is False


def test_run_pulls_a_round(client, mars):
    mars.give(1, "message.send", {"touser": "z", "content": "hi"})
    body = client.post("/api/v1/mars/tasks/run", json={"wait": 0}).json()
    assert body["pulled"] == 1


def test_run_without_a_body_defaults_to_no_waiting(client, mars):
    assert client.post("/api/v1/mars/tasks/run").json()["pulled"] == 0
    assert mars.pulls[0]["wait"] == 0.0


def test_start_and_stop_the_loop(client):
    assert client.post("/api/v1/mars/tasks/start").json()["running"] is True
    assert client.post("/api/v1/mars/tasks/stop").json()["running"] is False


def test_unreachable_mars_is_a_502(client, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        mars_tasks.loop, "client",
        httpx.AsyncClient(transport=httpx.MockTransport(boom)),
    )
    resp = client.post("/api/v1/mars/tasks/run", json={"wait": 0})
    assert resp.status_code == 502
    assert "调用 mars 失败" in resp.json()["errmsg"]


def test_unconfigured_run_is_a_400(monkeypatch):
    monkeypatch.delenv("WECOM_SVR_MARS_TASKS_TOKEN", raising=False)
    monkeypatch.setenv("WECOM_SVR_MARS_BASE_URL", "https://ts.qiniu.io")
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        resp = c.post("/api/v1/mars/tasks/run", json={"wait": 0})
    assert resp.status_code == 400
    assert "WECOM_SVR_MARS_TASKS_TOKEN" in resp.json()["errmsg"]
