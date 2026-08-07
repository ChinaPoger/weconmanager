"""群消息同步契约 —— 不读真实解密库、不联网。

真实数据只在本机存在,CI 里没有;所以本地库读取(messages)与 HTTP 推送
(httpx)都整体打桩,测的是「映射对不对、顺序对不对、断点续传对不对」。
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from wecom_svr import mars_sync, messages
from wecom_svr.main import create_app
from wecom_svr.mars_sync import Cursor, MarsSyncError, State
from wecom_svr.settings import Settings, get_agent_home, get_settings


def run(coro):
    """跑一个协程 —— 这套测试里没有并发,不值得为它引入 async 插件。"""
    return asyncio.run(coro)


GROUP = {"rid": 20614687431404, "name": "客户群A", "sort_time": 100}
GROUP2 = {"rid": 3395077687, "name": "kodo-alert-p1", "sort_time": 90}


def _msg(lid: int, send_time: int, text: str, userid: str = "chenyi", **kw) -> dict:
    """一条 fetch_since 出来的消息。正文与引用是分开的两段,不是拼好的字符串。"""
    return {
        "lid": lid, "rid": GROUP["rid"], "group_name": GROUP["name"],
        "send_time": send_time, "time": "2026-08-07 13:50:38",
        "sender_id": 5629500548464930, "sender_name": kw.get("name", "陈谊"),
        "sender_userid": userid, "avatar": "https://x/avatar",
        "type": "文本", "text": text, "quote_text": kw.get("quote_text", ""),
        "skipped": False,
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("WECOM_SVR_API_TOKEN", "WECOM_SVR_MARS_BASE_URL",
              "WECOM_SVR_MARS_SYNC_GROUPS", "WECOM_SVR_MARS_SYNC_ENABLED"):
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
        mars_sync_groups="客户群A",
    )


@pytest.fixture
def local(monkeypatch):
    """本地库打桩:一个群、一批固定消息。"""
    store = {"messages": [], "refreshed": 0}

    def fetch_since(cursor, rids=None, limit=500, include_skipped=False):
        rid_set = set(rids) if rids is not None else None
        rows = [
            m for m in store["messages"]
            if (m["send_time"], m["lid"]) > (cursor.send_time, cursor.lid)
            and (rid_set is None or m["rid"] in rid_set)
        ]
        rows.sort(key=lambda m: (m["send_time"], m["lid"]))
        rows = rows[:limit]
        # 真实实现里 LIMIT 在过滤之前生效,skipped 的行照样占额度并推进扫描位置
        kept = [m for m in rows if include_skipped or not m["skipped"]]
        end = Cursor(rows[-1]["send_time"], rows[-1]["lid"]) if rows else cursor
        return messages.Batch(messages=kept, cursor=end, scanned=len(rows))

    def refresh(force=False):
        store["refreshed"] += 1
        return True

    monkeypatch.setattr(messages, "refresh", refresh)
    monkeypatch.setattr(messages, "fetch_since", fetch_since)
    monkeypatch.setattr(messages, "list_groups", lambda search="": [GROUP, GROUP2])
    monkeypatch.setattr(messages, "latest_cursor", lambda: Cursor(9000, 9))
    return store


@pytest.fixture
def pushed(monkeypatch):
    """记录发往 mars 的 hook;把 fail_at 设成序号可以模拟中途失败。"""
    sent = []
    ctl = {"fail_at": None, "sent": sent}

    async def fake_push(client, hook, settings):
        if ctl["fail_at"] is not None and len(sent) == ctl["fail_at"]:
            raise MarsSyncError("mars 返回 500: boom")
        sent.append(hook)

    monkeypatch.setattr(mars_sync, "push_one", fake_push)
    return ctl


# ─── 映射 ──────────────────────────────────────────────────────────────

def test_hook_shape_matches_mars_message_hook(settings):
    hook = mars_sync.to_hook(_msg(1, 1786081838, "并发限制"), settings)
    assert hook == {
        "groupnumber": "wework-20614687431404",
        "groupName": "work-客户群A",
        "sendernumber": "chenyi",
        "displayname": "陈谊",
        "nickname": "陈谊",
        "message": "并发限制",
        "timestamp": 1786081838,
        "clientType": "Wework",
        "robot": "wecom-svr",
        "avatar": "https://x/avatar",
        "messageType": "文本",
    }


def test_group_name_gets_the_work_prefix_mars_expects(settings):
    """mars 靠群名认企微群,也靠它把这类群排除在客户未响应告警外。"""
    assert mars_sync.to_hook(_msg(1, 1, "x"), settings)["groupName"] == "work-客户群A"


def test_group_name_prefix_is_not_doubled(settings):
    msg = {**_msg(1, 1, "x"), "group_name": "work-已经带前缀了"}
    assert mars_sync.to_hook(msg, settings)["groupName"] == "work-已经带前缀了"


def test_group_name_prefix_can_be_disabled(settings):
    s = settings.model_copy(update={"mars_group_name_prefix": ""})
    assert mars_sync.to_hook(_msg(1, 1, "x"), s)["groupName"] == "客户群A"


def test_group_number_is_prefixed_to_avoid_colliding_with_qq_groups(settings):
    """mars 的 qq_groups.qq 是全局唯一键,裸 RID 可能撞上真实 QQ 群号。"""
    hook = mars_sync.to_hook(_msg(1, 1, "x"), settings)
    assert hook["groupnumber"].startswith("wework-")
    assert "20614687431404" in hook["groupnumber"]


def test_sender_without_userid_falls_back_to_vid(settings):
    """外部联系人和机器人没有 acctid,不能让 sendernumber 变空。"""
    hook = mars_sync.to_hook(_msg(1, 1, "x", userid=""), settings)
    assert hook["sendernumber"] == "wework-vid-5629500548464930"
    assert hook["displayname"] == "陈谊"


def test_card_placeholder_is_removed_but_type_is_kept(settings):
    msg = {**_msg(1, 1, "[卡片] 708781594 https://work.weixin.qq.com/card"), "type": "卡片"}
    hook = mars_sync.to_hook(msg, settings)
    assert hook["message"] == "708781594 https://work.weixin.qq.com/card"
    assert hook["messageType"] == "卡片"


def test_document_placeholder_is_removed_but_type_is_kept(settings):
    msg = {**_msg(1, 1, "[文档] https://doc.weixin.qq.com/smartpage/a1"), "type": "文档链接"}
    hook = mars_sync.to_hook(msg, settings)
    assert hook["message"] == "https://doc.weixin.qq.com/smartpage/a1"
    assert hook["messageType"] == "文档链接"


def test_placeholder_like_text_is_not_removed_from_plain_text(settings):
    hook = mars_sync.to_hook(_msg(1, 1, "[卡片] 是用户手写的文字"), settings)
    assert hook["message"] == "[卡片] 是用户手写的文字"


# ─── 图片正文 ──────────────────────────────────────────────────────────

KODO = {"kodo_ak": "AK", "kodo_sk": "SK", "kodo_bucket": "b", "kodo_domain": "https://cdn.x"}


@pytest.fixture
def kodo(settings, monkeypatch):
    """把 media.resolve 打桩:自己的图和引用里的图给不同 URL。"""
    urls = {b"own": "https://cdn.x/own.png", b"quote": "https://cdn.x/quote.png"}
    monkeypatch.setattr(mars_sync.media, "resolve", lambda raw, s: urls.get(raw, ""))
    return settings.model_copy(update=KODO)


def test_image_placeholder_is_replaced_by_the_url(kodo):
    """图渲染出来之后,再留一个「[图片]」在图上方纯属噪音。"""
    msg = {**_msg(1, 1, "[图片]"), "raw": b"own"}
    assert mars_sync.to_hook(msg, kodo)["message"] == "https://cdn.x/own.png"


def test_placeholder_stays_when_the_image_cannot_be_resolved(kodo):
    """缓存被清了就换不出 URL —— 这时占位符必须留着,否则消息就空了。"""
    msg = {**_msg(1, 1, "[图片]"), "raw": b"gone"}
    assert mars_sync.to_hook(msg, kodo)["message"] == "[图片]"


def test_own_and_quoted_images_get_their_own_urls(kodo):
    """一条消息可能有两张图,各换各的 —— 两段本来就没合并过,不用拆行去猜。"""
    msg = {**_msg(1, 1, "[图片]", quote_text="[图片]"), "raw": b"own", "quote_raw": b"quote"}
    assert mars_sync.to_hook(msg, kodo)["message"] == (
        "https://cdn.x/own.png\n> 引用: https://cdn.x/quote.png"
    )


def test_quote_url_alone_does_not_touch_the_body(kodo):
    msg = {**_msg(1, 1, "103会议室", quote_text="[图片]"), "quote_raw": b"quote"}
    assert mars_sync.to_hook(msg, kodo)["message"] == (
        "103会议室\n> 引用: https://cdn.x/quote.png"
    )


def test_a_url_is_applied_only_once_in_a_multiline_body(kodo):
    """没占位符时把 URL 缀在正文末尾,而不是每行都缀一遍。"""
    msg = {**_msg(1, 1, "第一行\n第二行"), "raw": b"own"}
    assert mars_sync.to_hook(msg, kodo)["message"] == "第一行\n第二行 https://cdn.x/own.png"


def test_quoting_the_same_image_is_deduped_by_url(kodo):
    """图片回复同一张图时,两段解析成同一个 URL,只留一份。

    旧实现在占位符阶段比对,两边都是「[图片]」就被当成重复,引用整段丢掉。
    """
    monkey = {**_msg(1, 1, "[图片]", quote_text="[图片]"), "raw": b"own", "quote_raw": b"own"}
    assert mars_sync.to_hook(monkey, kodo)["message"] == "https://cdn.x/own.png"


def test_media_is_skipped_entirely_when_kodo_is_off(settings, monkeypatch):
    monkeypatch.setattr(
        mars_sync.media, "resolve", lambda raw, s: pytest.fail("没配 kodo 不该去传图")
    )
    msg = {**_msg(1, 1, "[图片]"), "raw": b"own"}
    assert mars_sync.to_hook(msg, settings)["message"] == "[图片]"


def test_hook_url(settings):
    assert mars_sync.hook_url(settings) == "https://ts.qiniu.io/api/qq/hook/v2/message"


def test_missing_base_url_is_a_config_error():
    with pytest.raises(mars_sync.MarsNotConfigured):
        mars_sync.hook_url(Settings(mars_base_url=""))


# ─── 一轮同步 ───────────────────────────────────────────────────────────

def test_pushes_in_send_order(settings, local, pushed):
    local["messages"] = [
        _msg(3, 1000, "第三"), _msg(1, 900, "第一"), _msg(2, 1000, "第二"),
    ]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), settings)

    result = run(mars_sync.sync_once(settings))

    assert result.pushed == 3
    # 同秒的两条按 LID 排,不能乱 —— mars 的 SetTimeline 依赖入库顺序
    assert [h["message"] for h in pushed["sent"]] == ["第一", "第二", "第三"]
    assert mars_sync.load_state(settings).cursor == Cursor(1000, 3)


def test_only_whitelisted_groups_are_synced(settings, local, pushed):
    """告警群动辄二十万条,白名单之外的一条都不能漏出去。"""
    other = {**_msg(5, 1000, "告警"), "rid": GROUP2["rid"], "group_name": GROUP2["name"]}
    local["messages"] = [_msg(1, 900, "正常"), other]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), settings)

    run(mars_sync.sync_once(settings))
    assert [h["message"] for h in pushed["sent"]] == ["正常"]


def test_resumes_from_last_success_after_failure(settings, local, pushed):
    local["messages"] = [_msg(1, 900, "a"), _msg(2, 1000, "b"), _msg(3, 1100, "c")]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), settings)

    pushed["fail_at"] = 1                      # 第一条成功,第二条炸
    result = run(mars_sync.sync_once(settings))
    assert result.pushed == 1
    assert "boom" in result.error
    assert mars_sync.load_state(settings).cursor == Cursor(900, 1)

    pushed["fail_at"] = None                   # 下一轮从断点接上,不重推 a
    result = run(mars_sync.sync_once(settings))
    assert [h["message"] for h in pushed["sent"]] == ["a", "b", "c"]
    assert result.error == ""


def test_watermark_passes_over_a_batch_that_is_all_system_messages(settings, local, pushed):
    """LIMIT 在过滤之前生效,所以整批被过滤时水位也必须往前走,否则永远卡住。"""
    local["messages"] = [
        {**_msg(1, 900, "撤回了一条消息"), "skipped": True},
        {**_msg(2, 1000, "加入了群聊"), "skipped": True},
    ]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), settings)

    result = run(mars_sync.sync_once(settings))
    assert result.pushed == 0
    assert pushed["sent"] == []
    assert mars_sync.load_state(settings).cursor == Cursor(1000, 2)


def test_watermark_stays_put_when_a_push_fails_mid_batch(settings, local, pushed):
    """失败后不能顺手把水位推到批尾,否则中断点后面的消息会被吃掉。"""
    local["messages"] = [
        _msg(1, 900, "a"), _msg(2, 1000, "b"),
        {**_msg(3, 1100, "系统"), "skipped": True},
    ]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), settings)

    pushed["fail_at"] = 1
    run(mars_sync.sync_once(settings))
    assert mars_sync.load_state(settings).cursor == Cursor(900, 1)


def test_dry_run_pushes_nothing_and_keeps_watermark(settings, local, pushed):
    local["messages"] = [_msg(1, 900, "a")]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), settings)

    result = run(mars_sync.sync_once(settings, dry_run=True))
    assert result.scanned == 1 and result.pushed == 0
    assert pushed["sent"] == []
    assert mars_sync.load_state(settings).cursor == Cursor(800, 0)


def test_first_run_starts_at_latest_not_from_the_beginning(settings, local, pushed):
    """默认不倒灌历史 —— 往下游灌几万条历史是不可撤销的。"""
    local["messages"] = [_msg(1, 100, "很久以前"), _msg(2, 9500, "刚刚")]

    run(mars_sync.sync_once(settings))
    assert [h["message"] for h in pushed["sent"]] == ["刚刚"]


def test_backfill_seconds_opts_into_history(settings, local, pushed, monkeypatch):
    monkeypatch.setattr(mars_sync.time, "time", lambda: 10000.0)
    s = settings.model_copy(update={"mars_sync_backfill": 2000})
    local["messages"] = [_msg(1, 100, "太老"), _msg(2, 9500, "在窗口内")]

    run(mars_sync.sync_once(s))
    assert [h["message"] for h in pushed["sent"]] == ["在窗口内"]


def test_no_groups_configured_refuses_to_sync(local, pushed, tmp_path):
    s = Settings(data_dir=tmp_path, mars_base_url="https://ts.qiniu.io", mars_sync_groups="")
    with pytest.raises(mars_sync.MarsNotConfigured):
        run(mars_sync.sync_once(s))
    assert pushed["sent"] == []


def test_sync_all_groups_ignores_white_list(local, pushed, tmp_path, monkeypatch):
    s = Settings(
        data_dir=tmp_path,
        mars_base_url="https://ts.qiniu.io",
        mars_sync_groups="",
        mars_sync_all_groups=True,
    )
    monkeypatch.setattr(messages, "list_groups", lambda search="": [GROUP, GROUP2])
    second = {**_msg(2, 1000, "b"), "rid": GROUP2["rid"], "group_name": GROUP2["name"]}
    local["messages"] = [_msg(1, 900, "a"), second]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), s)
    run(mars_sync.sync_once(s))
    assert [h["message"] for h in pushed["sent"]] == ["a", "b"]


def test_unknown_group_name_is_reported_not_silently_ignored(local, pushed, tmp_path):
    s = Settings(data_dir=tmp_path, mars_base_url="https://ts.qiniu.io",
                 mars_sync_groups="查无此群")
    with pytest.raises(mars_sync.MarsNotConfigured) as e:
        run(mars_sync.sync_once(s))
    assert "查无此群" in str(e.value)


# ─── 水位持久化 ─────────────────────────────────────────────────────────

def test_state_round_trips(settings):
    mars_sync.save_state(State(cursor=Cursor(5, 6), pushed=7, last_error="x"), settings)
    got = mars_sync.load_state(settings)
    assert (got.cursor, got.pushed, got.last_error) == (Cursor(5, 6), 7, "x")


def test_corrupt_state_falls_back_to_zero_instead_of_crashing(settings):
    settings.mars_state_file.parent.mkdir(parents=True, exist_ok=True)
    settings.mars_state_file.write_text("{ not json", encoding="utf-8")
    assert mars_sync.load_state(settings) == State()


# ─── HTTP 接口 ──────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WECOM_SVR_MARS_BASE_URL", "https://ts.qiniu.io")
    monkeypatch.setenv("WECOM_SVR_MARS_SYNC_GROUPS", "客户群A")
    get_settings.cache_clear()
    return TestClient(create_app())


def test_status_reports_config_and_watermark(client, local):
    r = client.get("/api/v1/mars/sync/status")
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["hook_url"] == "https://ts.qiniu.io/api/qq/hook/v2/message"
    assert body["config"]["groups"] == ["客户群A"]
    assert body["running"] is False


def test_groups_marks_the_selected_ones(client, local):
    r = client.get("/api/v1/mars/sync/groups")
    assert r.status_code == 200
    got = {g["name"]: g["selected"] for g in r.json()["groups"]}
    assert got == {"客户群A": True, "kodo-alert-p1": False}


def test_run_dry_run_over_http(client, local, pushed):
    local["messages"] = [_msg(1, 9500, "hi")]
    r = client.post("/api/v1/mars/sync/run", json={"dry_run": True})
    assert r.status_code == 200
    assert r.json()["scanned"] == 1 and r.json()["pushed"] == 0
    assert pushed["sent"] == []


def test_run_without_config_is_400_not_500(monkeypatch, local, pushed):
    monkeypatch.delenv("WECOM_SVR_MARS_BASE_URL", raising=False)
    monkeypatch.setenv("WECOM_SVR_MARS_SYNC_GROUPS", "")
    get_settings.cache_clear()
    c = TestClient(create_app())
    r = c.post("/api/v1/mars/sync/run", json={})
    assert r.status_code == 400
    assert "群" in r.json()["errmsg"]


def test_downstream_failure_is_502_not_500(client, local, monkeypatch):
    local["messages"] = [_msg(1, 9500, "hi")]
    mars_sync.save_state(State(cursor=Cursor(1, 0)), get_settings())

    async def boom(*a, **kw):
        raise MarsSyncError("调用 mars 失败: connect timeout")

    monkeypatch.setattr(mars_sync, "sync_once", boom)
    r = client.post("/api/v1/mars/sync/run", json={})
    assert r.status_code == 502
    assert "connect timeout" in r.json()["errmsg"]


def test_watermark_reset_to_latest(client, local):
    r = client.post("/api/v1/mars/sync/watermark", json={})
    assert r.status_code == 200
    assert r.json()["cursor"] == {"send_time": 9000, "lid": 9}


def test_watermark_backfill(client, local, monkeypatch):
    monkeypatch.setattr(mars_sync.time, "time", lambda: 10000.0)
    r = client.post("/api/v1/mars/sync/watermark", json={"backfill_seconds": 600})
    assert r.json()["cursor"] == {"send_time": 9400, "lid": 0}


def test_preview_shows_the_exact_hook_body(client, local):
    local["messages"] = [_msg(1, 9500, "待推")]
    mars_sync.save_state(State(cursor=Cursor(9000, 0)), get_settings())
    r = client.post("/api/v1/mars/sync/preview")
    assert r.status_code == 200
    pending = r.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["hook"]["message"] == "待推"
    assert pending[0]["hook"]["groupnumber"] == "wework-20614687431404"


def test_start_stop_toggle_the_loop(client, local):
    assert client.post("/api/v1/mars/sync/start").json()["running"] is True
    assert client.post("/api/v1/mars/sync/stop").json()["running"] is False
