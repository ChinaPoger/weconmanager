"""群消息同步契约 —— 不读真实解密库、不联网。

真实数据只在本机存在,CI 里没有;所以本地库读取(messages)与 HTTP 推送
(httpx)都整体打桩,测的是「映射对不对、顺序对不对、断点续传对不对」。
"""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from wecom_svr import directory, mars_sync, messages
from wecom_svr.main import create_app
from wecom_svr.mars_sync import Cursor, GroupState, MarsStateError, MarsSyncError, State
from wecom_svr.settings import Settings, get_agent_home, get_settings


def run(coro):
    """跑一个协程 —— 这套测试里没有并发,不值得为它引入 async 插件。"""
    return asyncio.run(coro)


GROUP = {"rid": 20614687431404, "name": "客户群A", "sort_time": 100}
GROUP2 = {"rid": 3395077687, "name": "kodo-alert-p1", "sort_time": 90}
SELF_VID = "1688856651864877"


def _msg(lid: int, send_time: int, text: str, userid: str = "chenyi", **kw) -> dict:
    """一条 fetch_since 出来的消息。正文与引用是分开的两段,不是拼好的字符串。"""
    return {
        "lid": lid, "rid": GROUP["rid"], "group_name": GROUP["name"],
        "send_time": send_time, "time": "2026-08-07 13:50:38",
        "sender_id": 5629500548464930, "sender_name": kw.get("name", "陈谊"),
        "sender_userid": userid, "sender_corpid": kw.get("corpid", "1970325043038620"),
        "avatar": "https://x/avatar",
        "type": "文本", "text": text, "quote_text": kw.get("quote_text", ""),
        "skipped": False,
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("WECOM_SVR_API_TOKEN", "WECOM_SVR_MARS_BASE_URL",
              "WECOM_SVR_MARS_SYNC_GROUPS", "WECOM_SVR_MARS_SYNC_ENABLED",
              "WECOM_SVR_MARS_GROUP_NAME_PREFIX"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WECOM_SVR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(directory, "self_vid_or_none", lambda: SELF_VID)
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

    def fetch_since(cursors, limit=500, include_skipped=False, offset=0):
        selected, next_offset = messages._quotas(list(cursors), limit, offset)
        rows = []
        for rid, quota in selected:
            cursor = cursors[rid]
            group_rows = [
                m for m in store["messages"]
                if m["rid"] == rid
                and (m["send_time"], m["lid"]) > (cursor.send_time, cursor.lid)
            ]
            group_rows.sort(key=lambda m: (m["send_time"], m["lid"]))
            rows.extend(group_rows[:quota])
        rows.sort(key=lambda m: (m["send_time"], m["lid"]))
        # 真实实现里 LIMIT 在过滤之前生效,skipped 的行照样占额度并推进扫描位置
        kept = [m for m in rows if include_skipped or not m["skipped"]]
        scan_end = {}
        scan_counts = {}
        for row in rows:
            rid = row["rid"]
            scan_end[rid] = Cursor(row["send_time"], row["lid"])
            scan_counts[rid] = scan_counts.get(rid, 0) + 1
        return messages.Batch(
            messages=kept,
            scan_end=scan_end,
            scan_counts=scan_counts,
            selected_rids=[rid for rid, _quota in selected],
            next_offset=next_offset,
        )

    def refresh(force=False):
        store["refreshed"] += 1
        return True

    monkeypatch.setattr(messages, "refresh", refresh)
    monkeypatch.setattr(messages, "fetch_since", fetch_since)
    monkeypatch.setattr(messages, "list_groups", lambda search="", **kw: [GROUP, GROUP2])
    monkeypatch.setattr(messages, "latest_cursor", lambda rid=None: Cursor(9000, 9))
    # 群信息默认开,但消息测试不该去读真实 member_bin。空成员 = 跳过推送。
    monkeypatch.setattr(
        messages, "group_profile",
        lambda rid: {"rid": rid, "name": GROUP["name"] if rid == GROUP["rid"] else "", "sort_time": 0},
    )
    monkeypatch.setattr(messages, "group_members", lambda rid: [])
    monkeypatch.setattr(
        messages,
        "count_since",
        lambda cursors: {
            rid: sum(
                (m["send_time"], m["lid"]) > (cursor.send_time, cursor.lid)
                for m in store["messages"] if m["rid"] == rid
            )
            for rid, cursor in cursors.items()
        },
    )
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
        "uid": 0,
        "groupNumber": "wework-20614687431404",
        "groupName": "客户群A",
        "senderNumber": "5629500548464930",
        "userid": "chenyi",
        "corpid": "1970325043038620",
        "displayname": "陈谊",
        "nickname": "陈谊",
        "message": "并发限制",
        "timestamp": 1786081838,
        "clientType": "Wework",
        "robot": SELF_VID,
        "avatar": "https://x/avatar",
        "messageType": "文本",
    }


def test_robot_defaults_to_current_wecom_vid(settings):
    """QQ 机器人填 QQ 号;企微这边填本机登录账号的 VID。"""
    assert mars_sync.to_hook(_msg(1, 1, "x"), settings)["robot"] == SELF_VID
    assert mars_sync.robot_id(settings) == SELF_VID


def test_explicit_mars_robot_overrides_current_vid(settings):
    s = settings.model_copy(update={"mars_robot": "1688851136652708"})
    assert mars_sync.to_hook(_msg(1, 1, "x"), s)["robot"] == "1688851136652708"


def test_placeholder_mars_robot_still_uses_current_vid(settings):
    s = settings.model_copy(update={"mars_robot": "wecom-svr"})
    assert mars_sync.to_hook(_msg(1, 1, "x"), s)["robot"] == SELF_VID


def test_empty_mars_robot_uses_current_vid(settings):
    s = settings.model_copy(update={"mars_robot": ""})
    assert mars_sync.to_hook(_msg(1, 1, "x"), s)["robot"] == SELF_VID


def test_robot_falls_back_when_current_vid_is_unknown(settings, monkeypatch):
    monkeypatch.setattr(directory, "self_vid_or_none", lambda: None)
    assert mars_sync.to_hook(_msg(1, 1, "x"), settings)["robot"] == "wecom-svr"


def test_group_name_is_unchanged_by_default(settings):
    assert mars_sync.to_hook(_msg(1, 1, "x"), settings)["groupName"] == "客户群A"


def test_group_name_prefix_can_be_configured(settings):
    s = settings.model_copy(update={"mars_group_name_prefix": "work-"})
    assert mars_sync.to_hook(_msg(1, 1, "x"), s)["groupName"] == "work-客户群A"


def test_group_name_prefix_is_not_doubled(settings):
    s = settings.model_copy(update={"mars_group_name_prefix": "work-"})
    msg = {**_msg(1, 1, "x"), "group_name": "work-已经带前缀了"}
    assert mars_sync.to_hook(msg, s)["groupName"] == "work-已经带前缀了"


def test_empty_group_name_uses_rid_placeholder(settings):
    """无名群不能原样推空串:mars 用 GroupName 派生 Slack channel,会撞到一起。"""
    msg = {**_msg(1, 1, "x"), "group_name": ""}
    assert mars_sync.to_hook(msg, settings)["groupName"] == f"未命名群-{GROUP['rid']}"


def test_empty_group_name_placeholder_keeps_prefix(settings):
    s = settings.model_copy(update={"mars_group_name_prefix": "work-"})
    msg = {**_msg(1, 1, "x"), "group_name": "   "}
    assert mars_sync.to_hook(msg, s)["groupName"] == f"work-未命名群-{GROUP['rid']}"


def test_empty_group_name_without_rid_still_has_a_fallback(settings):
    assert mars_sync.group_name("", settings) == "未命名群"


def test_group_number_is_prefixed_to_avoid_colliding_with_qq_groups(settings):
    """mars 的 qq_groups.qq 是全局唯一键,裸 RID 可能撞上真实 QQ 群号。"""
    hook = mars_sync.to_hook(_msg(1, 1, "x"), settings)
    assert hook["groupNumber"].startswith("wework-")
    assert "20614687431404" in hook["groupNumber"]


def test_http_payload_canonicalization_covers_both_group_fields():
    payload = mars_sync.canonicalize_mars_payload({
        "groupNumber": "1.0883245789880156e+16",
        "group_number": "R:1.0883245789880156e+16",
        "senderNumber": "1.0883245789880156e+16",
    })
    assert payload == {
        "groupNumber": "10883245789880156",
        "group_number": "R:10883245789880156",
        "senderNumber": "10883245789880156",
    }


def test_id_text_never_emits_scientific_notation():
    rid = 10945118031183243
    assert mars_sync.id_text(rid) == "10945118031183243"
    assert mars_sync.id_text(float(rid)) == "10945118031183244"  # IEEE-754 最近偶数
    assert mars_sync.id_text("1.0945118031183244e+16") == "10945118031183244"
    assert mars_sync.id_text("10945118031183243") == "10945118031183243"
    assert "e" not in mars_sync.id_text(float(rid)).lower()
    assert mars_sync.group_number_text("R:", float(rid)) == "R:10945118031183244"
    assert mars_sync.normalize_group_number("R:1.0945118031183244e+16") == "R:10945118031183244"
    assert mars_sync.normalize_group_number("wework-1.0945118031183244e+16") == "wework-10945118031183244"


def test_exact_scientific_rid_string_keeps_all_17_digits():
    """新群 RID 从 SQLite/JSON 以科学计数法字符串来时不能再丢精度。"""
    rid = "1.0946415406230956e+16"
    assert mars_sync.id_text(rid) == "10946415406230956"
    assert mars_sync.group_number_text("R:", rid) == "R:10946415406230956"


def test_large_float_rid_is_decimal_in_group_number(settings):
    """sqlite REAL / JSON number 会把 17 位 RID 变成 float,绝不能 f-string 成 e+16。"""
    rid = 10945118031183243.0
    msg = {**_msg(1, 1, "x"), "rid": rid, "group_name": ""}
    s = settings.model_copy(update={"mars_group_prefix": "R:"})
    hook = mars_sync.to_hook(msg, s)
    assert hook["groupNumber"] == "R:10945118031183244"
    assert "e+" not in hook["groupNumber"].lower()
    assert hook["groupName"] == "未命名群-10945118031183244"
    assert hook["senderNumber"] == "5629500548464930"


def test_float_sender_id_is_decimal_not_scientific(settings):
    msg = {**_msg(1, 1, "x"), "sender_id": 5629500548464930.0}
    hook = mars_sync.to_hook(msg, settings)
    assert hook["senderNumber"] == "5629500548464930"
    assert "e" not in hook["senderNumber"].lower()


def test_sendernumber_uses_local_rid_even_when_userid_exists(settings):
    hook = mars_sync.to_hook(_msg(1, 1, "x", userid=""), settings)
    assert hook["senderNumber"] == "5629500548464930"
    assert hook["userid"] == ""
    assert hook["displayname"] == "陈谊"


def test_hook_carries_member_userid_and_corpid(settings):
    hook = mars_sync.to_hook(
        _msg(1, 1, "x", userid="YuSunJie", corpid="1970325276990586"), settings
    )
    assert hook["senderNumber"] == "5629500548464930"
    assert hook["userid"] == "YuSunJie"
    assert hook["corpid"] == "1970325276990586"


def test_hook_normalizes_numeric_member_identity_fields(settings):
    hook = mars_sync.to_hook(
        _msg(1, 1, "x", userid=708781594, corpid=1970325276990586), settings
    )
    assert hook["userid"] == "708781594"
    assert hook["corpid"] == "1970325276990586"


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
    monkeypatch.setattr(mars_sync.media, "resolve", lambda raw, s, *a: urls.get(raw, ""))
    return settings.model_copy(update=KODO)


def _image(n, kind: str = "image") -> "mars_sync.media.MediaItem":
    return mars_sync.media.MediaItem(str(n) * 32, 1, f"{n}.png", kind, None)


@pytest.fixture
def tree(settings, monkeypatch):
    """把媒体树打桩:`collect` 按载荷给项,`resolve_item` 按文件名给 URL。

    返回 `setup(items_by_payload, **overrides) -> Settings`,三个多图测试只在
    「树里有什么」和「断言什么」上不同,建桩这一段没必要各写一遍。
    """
    def setup(items, **overrides):
        by_payload = items if isinstance(items, dict) else {None: items}
        monkeypatch.setattr(
            mars_sync.media, "collect",
            lambda raw: by_payload.get(raw, by_payload.get(None, [])),
        )
        monkeypatch.setattr(
            mars_sync.media, "resolve_item",
            lambda item, *_args: f"https://cdn.x/{item.filename}",
        )
        return settings.model_copy(update={**KODO, **overrides})

    return setup


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


def test_multiple_images_are_rendered_one_url_per_line(tree):
    s = tree([_image(i) for i in (1, 2, 3)])
    msg = {**_msg(1, 1, "[图片]"), "raw": b"tree"}
    assert mars_sync.message_body(msg, s) == (
        "https://cdn.x/1.png\nhttps://cdn.x/2.png\nhttps://cdn.x/3.png"
    )


def test_image_limit_and_file_names_are_rendered_deterministically(tree):
    attachment = mars_sync.media.MediaItem("a" * 32, 20, "notes.md", "file", None)
    s = tree([*(_image(i) for i in (1, 2, 3)), attachment], mars_image_max_per_message=2)
    msg = {**_msg(1, 1, "[图片]"), "raw": b"tree"}
    assert mars_sync.message_body(msg, s) == (
        "https://cdn.x/1.png\nhttps://cdn.x/2.png\n[文件] notes.md\n(另有 1 张未展示)"
    )


def test_media_is_skipped_entirely_when_kodo_is_off(settings, monkeypatch):
    monkeypatch.setattr(
        mars_sync.media, "resolve", lambda raw, s, *a: pytest.fail("没配 kodo 不该去传图")
    )
    msg = {**_msg(1, 1, "[图片]"), "raw": b"own"}
    assert mars_sync.to_hook(msg, settings)["message"] == "[图片]"


def test_image_limit_is_shared_by_body_and_quote(tree):
    own = mars_sync.media.MediaItem("1" * 32, 1, "own.png", "image", None)
    quoted = [mars_sync.media.MediaItem(str(i) * 32, 1, f"q{i}.png", "image", None)
              for i in (2, 3)]
    s = tree({b"own": [own], b"quote": quoted}, mars_image_max_per_message=2)
    msg = {
        **_msg(1, 1, "[图片]", quote_text="[图片]"),
        "raw": b"own", "quote_raw": b"quote",
    }
    assert mars_sync.message_body(msg, s) == (
        "https://cdn.x/own.png\n> 引用: https://cdn.x/q2.png\n(另有 1 张未展示)"
    )


def test_an_attachment_only_message_does_not_claim_to_carry_an_image(tree):
    """`[图片]\\n[文件] x.zip` 是错的:那条消息里根本没有图。"""
    attachment = mars_sync.media.MediaItem("a" * 32, 20, "tstest.zip", "file", None)
    s = tree([attachment])

    # 占位符由 messages 按种类选,渲染再把它换成文件名行 —— 两半必须对上。
    assert messages._media_placeholder([attachment]) == "[文件]"
    msg = {**_msg(1, 1, messages._media_placeholder([attachment])), "raw": b"tree"}
    assert mars_sync.message_body(msg, s) == "[文件] tstest.zip"


def test_an_absent_payload_does_not_eat_an_image_slot(tree):
    """`_ready_count` 和 `message_body` 必须分到同一份配额。

    以前 `quote_raw is None` 也会被当成一个「旧式单图载荷」扣掉一格,于是等图那侧
    只预取 8 张、排版那侧却去解第 9 张 —— 等的不是要排的那张。
    """
    s = tree([_image(i) for i in range(9)])

    body, quote = mars_sync.media_plan({"raw": None, "quote_raw": b"tree"}, s)
    assert (body.images, body.legacy, body.omitted) == ([], False, 0)
    assert len(quote.images) == 9 and quote.omitted == 0


def test_outbox_reuses_the_first_hook_when_media_resolution_changes(settings, monkeypatch):
    msg = {**_msg(7, 100, "[图片]"), "raw": b"tree"}
    calls = iter(["https://cdn.x/first.png", "https://cdn.x/later.png"])
    monkeypatch.setattr(mars_sync, "to_hook", lambda _msg, _settings: {"message": next(calls)})

    first = mars_sync.outbox_hook(msg, settings)
    second = mars_sync.outbox_hook(msg, settings)

    assert first == second == {"message": "https://cdn.x/first.png"}
    assert len(list(settings.mars_outbox_dir.rglob("*.json"))) == 1
    assert mars_sync.peek_outbox(msg, settings) == first


def test_old_outbox_normalizes_numeric_member_identity_fields(settings):
    msg = {**_msg(7, 100, "[图片]"), "raw": b"tree"}
    path = mars_sync._outbox_path(msg, settings)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"message": "frozen", "userid": 708781594, "corpid": 1970325276990586}),
        encoding="utf-8",
    )

    assert mars_sync.peek_outbox(msg, settings) == {
        "message": "frozen",
        "userid": "708781594",
        "corpid": "1970325276990586",
    }


def test_old_outbox_rewrites_scientific_group_number(settings):
    msg = {**_msg(7, 100, "[图片]"), "raw": b"tree", "rid": 10945118031183243.0}
    path = mars_sync._outbox_path(msg, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "message": "frozen",
            "groupNumber": "R:1.0945118031183244e+16",
            "senderNumber": 5.62950054846493e+15,
        }),
        encoding="utf-8",
    )
    hook = mars_sync.peek_outbox(msg, settings)
    assert hook["groupNumber"] == "R:10945118031183244"
    assert hook["senderNumber"] == "5629500548464930"
    assert "e" not in hook["groupNumber"].lower()


def test_a_message_without_media_is_not_written_to_the_outbox(settings):
    """没有媒体的 Content 已经是消息的纯函数,不值得为它付一次 fsync。"""
    mars_sync.outbox_hook(_msg(1, 100, "就一句话"), settings)
    assert not list(settings.mars_outbox_dir.rglob("*.json"))


def test_the_outbox_is_reclaimed_once_the_watermark_passes(settings, monkeypatch):
    monkeypatch.setattr(mars_sync, "to_hook", lambda _msg, _s: {"message": "x"})
    delivered = {**_msg(1, 100, "[图片]"), "raw": b"tree"}
    in_flight = {**_msg(2, 300, "[图片]"), "raw": b"tree"}
    mars_sync.outbox_hook(delivered, settings)
    mars_sync.outbox_hook(in_flight, settings)

    mars_sync.prune_outbox({str(GROUP["rid"]): Cursor(100, 1)}, settings)

    assert mars_sync.peek_outbox(delivered, settings) is None
    assert mars_sync.peek_outbox(in_flight, settings) is not None


def test_watermark_cannot_cross_the_media_cutover(client, local):
    settings = get_settings()
    rid = GROUP["rid"]
    state = State(
        version=3,
        groups={str(rid): GroupState(cursor=Cursor(900, 1), media_cutover=Cursor(850, 1))},
    )
    state.refresh_shadow_cursor()
    mars_sync.save_state(state, settings)

    response = client.post(
        "/api/v1/mars/sync/watermark", json={"rid": rid, "send_time": 800, "lid": 0},
    )

    assert response.status_code == 400
    assert "禁止跨点回放" in response.json()["errmsg"]

    forced = client.post(
        "/api/v1/mars/sync/watermark",
        json={"rid": rid, "send_time": 800, "lid": 0, "force": True},
    )
    assert forced.status_code == 200
    assert mars_sync.load_state(settings).groups[str(rid)].cursor == Cursor(800, 0)


def test_the_media_cutover_also_covers_groups_outside_the_current_scope(settings, local, pushed):
    """升级那一刻不在白名单里的群,回来之后同样不许跨点回放。"""
    other = str(GROUP2["rid"])
    state = State(version=2, groups={
        str(GROUP["rid"]): GroupState(cursor=Cursor(800, 7)),
        other: GroupState(cursor=Cursor(700, 3), active=False),
    })
    mars_sync.save_state(state, settings)

    run(mars_sync.sync_once(settings))

    groups = mars_sync.load_state(settings).groups
    assert groups[other].media_cutover == Cursor(700, 3)


# ─── 等图:图还没下载下来时按住水位 ──────────────────────────────────────

@pytest.fixture
def waiting(settings, monkeypatch):
    """把 media.pending 打桩:raw 是 b"late" 的那张图还没落到本地。"""
    monkeypatch.setattr(mars_sync.media, "pending", lambda raw, s: raw == b"late")
    # 等图规划现在会主动尝试拉取;这里仍要整体隔离真实企微 profile,CI 没有本地缓存。
    monkeypatch.setattr(mars_sync.media, "ensure_local", lambda raw, s, rid, lid: "")
    monkeypatch.setattr(mars_sync.media, "resolve", lambda raw, s, *a: "")
    monkeypatch.setattr(mars_sync.time, "time", lambda: 1000.0)
    return settings.model_copy(update={**KODO, "mars_image_wait": 60})


def _late(lid: int, send_time: int, **kw) -> dict:
    return {**_msg(lid, send_time, "[图片]"), "type": "图片", "raw": b"late", **kw}


def test_a_freshly_arrived_image_is_held_for_the_next_round(waiting, local, pushed):
    """企微要等你点开会话才下载图片,这时推出去的只会是个占位符。"""
    local["messages"] = [_msg(1, 900, "先说句话"), _late(2, 980)]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), waiting)

    result = run(mars_sync.sync_once(waiting))
    assert result.pushed == 1 and result.held == 1
    assert [h["message"] for h in pushed["sent"]] == ["先说句话"]
    # 水位停在图片那条**之前**,下一轮才能重新读到它
    assert mars_sync.load_state(waiting).cursor == Cursor(900, 1)


def test_the_held_image_goes_out_once_the_file_lands(waiting, local, pushed, monkeypatch):
    local["messages"] = [_late(1, 980)]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), waiting)
    assert run(mars_sync.sync_once(waiting)).held == 1

    monkeypatch.setattr(mars_sync.media, "pending", lambda raw, s: False)
    monkeypatch.setattr(mars_sync.media, "resolve", lambda raw, s, *a: "https://cdn.x/late.png")
    result = run(mars_sync.sync_once(waiting))

    assert result.pushed == 1 and result.held == 0
    assert [h["message"] for h in pushed["sent"]] == ["https://cdn.x/late.png"]


def test_ready_count_actively_fetches_before_holding_the_group(waiting, monkeypatch):
    state = {"pending": True}
    calls: list[tuple[bytes, int, int]] = []
    monkeypatch.setattr(mars_sync.media, "pending", lambda raw, s: state["pending"])

    def fetched(raw, settings, rid, lid):
        calls.append((raw, rid, lid))
        state["pending"] = False
        return "/tmp/original.png"

    monkeypatch.setattr(mars_sync.media, "ensure_local", fetched)
    msg = _late(7, 980)

    assert mars_sync._ready_count([msg], waiting) == 1
    assert calls == [(b"late", GROUP["rid"], 7)]


def test_ready_count_does_not_treat_a_type101_source_url_as_ready(waiting, monkeypatch):
    item = mars_sync.media.MediaItem(
        "a" * 32,
        100,
        "image-1.jpg",
        "image",
        None,
        "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=expired",
    )
    msg = _late(7, 980, raw=b"type101")
    calls = []

    monkeypatch.setattr(mars_sync.media, "collect", lambda raw: [item])
    monkeypatch.setattr(
        mars_sync.media,
        "locate_item",
        lambda found, settings: mars_sync.media.Located(
            found.md5, found.filename, found.source_url, [], True,
        ),
    )

    def unavailable(found, settings, rid, lid, deadline, loc):
        calls.append((found, rid, lid, loc.url))
        return ""

    monkeypatch.setattr(mars_sync.media, "ensure_item_local", unavailable)

    assert mars_sync._ready_count([msg], waiting) == 0
    assert calls == [(item, GROUP["rid"], 7, item.source_url)]


def test_waiting_gives_up_after_the_deadline(waiting, local, pushed):
    """等不到就带着占位符推走 —— 一张永远不会下载的图不能卡死整条同步链。"""
    local["messages"] = [_late(1, 900)]              # now=1000,已经等满 60s
    mars_sync.save_state(State(cursor=Cursor(800, 0)), waiting)

    result = run(mars_sync.sync_once(waiting))
    assert result.pushed == 1 and result.held == 0
    assert [h["message"] for h in pushed["sent"]] == ["[图片]"]


def test_messages_after_a_held_image_wait_their_turn(waiting, local, pushed):
    """保序是硬要求:mars 的 SetTimeline 按入库顺序切问题,不能跳过去先推后面的。"""
    local["messages"] = [_late(1, 980), _msg(2, 990, "后面这句")]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), waiting)

    result = run(mars_sync.sync_once(waiting))
    assert result.pushed == 0 and result.held == 2
    assert pushed["sent"] == []
    assert mars_sync.load_state(waiting).cursor == Cursor(800, 0)


def test_a_held_batch_does_not_let_the_watermark_jump_to_the_scan_end(waiting, local, pushed):
    """批尾有被过滤掉的系统消息时,水位本来会跳到扫描位置 —— 那会吃掉被扣下的那条。"""
    local["messages"] = [_late(1, 980), {**_msg(2, 990, "撤回了一条消息"), "skipped": True}]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), waiting)

    run(mars_sync.sync_once(waiting))
    assert mars_sync.load_state(waiting).cursor == Cursor(800, 0)


def test_an_image_with_nothing_to_wait_for_is_not_held(waiting, local, pushed):
    """手机端发的图正文里连文件名都没有,等一个窗口纯属白等。"""
    local["messages"] = [{**_msg(1, 980, "[图片]"), "type": "图片", "raw": b"no-name"}]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), waiting)

    result = run(mars_sync.sync_once(waiting))
    assert result.pushed == 1 and result.held == 0


def test_a_quoted_image_can_hold_the_batch_too(waiting, local, pushed):
    msg = {**_msg(1, 980, "这个", quote_text="[图片]"), "quote_raw": b"late"}
    local["messages"] = [msg]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), waiting)

    assert run(mars_sync.sync_once(waiting)).held == 1


def test_waiting_can_be_switched_off(waiting, local, pushed):
    s = waiting.model_copy(update={"mars_image_wait": 0})
    local["messages"] = [_late(1, 980)]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), s)

    result = run(mars_sync.sync_once(s))
    assert result.pushed == 1 and result.held == 0


def test_waiting_is_off_when_there_is_nowhere_to_upload(settings, local, pushed, monkeypatch):
    """没配 kodo 就永远换不出 URL,等下去毫无意义。"""
    monkeypatch.setattr(mars_sync.media, "pending", lambda raw, s, *a: pytest.fail("不该去看缓存"))
    local["messages"] = [_late(1, 980)]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), settings)

    assert run(mars_sync.sync_once(settings)).pushed == 1


def test_a_held_group_does_not_delay_another_group(waiting, local, pushed):
    """本次设计的核心:A 群等图时,B 群仍在同一轮推出并提交自己的水位。"""
    s = waiting.model_copy(update={"mars_sync_groups": "客户群A,kodo-alert-p1"})
    second = {**_msg(3, 985, "B 正常"), "rid": GROUP2["rid"], "group_name": GROUP2["name"]}
    local["messages"] = [_late(1, 980), second, _msg(2, 990, "A 后续")]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), s)

    result = run(mars_sync.sync_once(s))

    state = mars_sync.load_state(s)
    assert [hook["message"] for hook in pushed["sent"]] == ["B 正常"]
    assert result.held == 2
    assert state.groups[str(GROUP["rid"])].cursor == Cursor(800, 0)
    assert state.groups[str(GROUP2["rid"])].cursor == Cursor(985, 3)


def test_a_failed_group_does_not_stop_another_group(settings, local, monkeypatch):
    s = settings.model_copy(update={"mars_sync_groups": "客户群A,kodo-alert-p1"})
    second = {**_msg(2, 950, "B 正常"), "rid": GROUP2["rid"], "group_name": GROUP2["name"]}
    local["messages"] = [_msg(1, 900, "A 失败"), second]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), s)
    sent = []

    async def fail_a(_client, hook, _settings):
        if hook["groupNumber"].endswith(str(GROUP["rid"])):
            raise MarsSyncError("boom-a")
        sent.append(hook)

    monkeypatch.setattr(mars_sync, "push_one", fail_a)
    result = run(mars_sync.sync_once(s))

    assert [hook["message"] for hook in sent] == ["B 正常"]
    assert result.partial is True and result.error == "boom-a"
    assert result.group_results[str(GROUP["rid"])]["error"] == "boom-a"
    state = mars_sync.load_state(s)
    assert state.groups[str(GROUP["rid"])].cursor == Cursor(800, 0)
    assert state.groups[str(GROUP2["rid"])].cursor == Cursor(950, 2)


def test_fetch_since_applies_a_real_per_group_quota(monkeypatch):
    """固化实施修正:深积压群不能吃完全局 LIMIT,并顺带执行真实 UNION SQL。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE MESSAGE (LID INTEGER, conv_id INTEGER, sender_id INTEGER, "
        "content_type INTEGER, send_time INTEGER, content BLOB, extracontent BLOB)"
    )
    conn.execute("CREATE TABLE CONVERSATION (RID INTEGER, name TEXT, conversationtype INTEGER)")
    conn.executemany(
        "INSERT INTO CONVERSATION VALUES (?, ?, ?)",
        [(1, "A", 1), (1, "同 RID 的单聊", 0), (2, "B", 1)],
    )
    conn.executemany(
        "INSERT INTO MESSAGE VALUES (?, ?, 9, 1, ?, x'01', x'')",
        [(lid, 1, 100 + lid) for lid in range(1, 11)] + [(20, 2, 105)],
    )
    monkeypatch.setattr(messages, "_rows", lambda sql, params=(): conn.execute(sql, params).fetchall())
    monkeypatch.setattr(messages, "_load_senders", lambda: {})
    monkeypatch.setattr(
        messages,
        "_vendor",
        lambda: SimpleNamespace(export=SimpleNamespace(fmt_time=str, resolve_sender=lambda *_: "")),
    )
    monkeypatch.setattr(messages, "_type_name", lambda _ctype: "文本")
    monkeypatch.setattr(messages, "_render", lambda *_args: "正文")
    monkeypatch.setattr(messages, "_quote_payload", lambda _raw: None)
    monkeypatch.setattr(messages, "_quote_summary", lambda _raw: "")

    batch = messages.fetch_since({1: Cursor(), 2: Cursor()}, limit=4)

    assert batch.scan_counts == {1: 2, 2: 1}
    assert [(msg["rid"], msg["lid"]) for msg in batch.messages] == [(1, 1), (1, 2), (2, 20)]
    conn.close()


def test_sync_once_calls_are_serialized_by_one_lock(monkeypatch):
    active = 0
    peak = 0

    async def fake_once(*_args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return mars_sync.SyncResult()

    monkeypatch.setattr(mars_sync, "_sync_once", fake_once)

    async def both():
        await asyncio.gather(mars_sync.sync_once(), mars_sync.sync_once())

    run(both())
    assert peak == 1


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
    monkeypatch.setattr(messages, "list_groups", lambda search="", **kw: [GROUP, GROUP2])
    second = {**_msg(2, 1000, "b"), "rid": GROUP2["rid"], "group_name": GROUP2["name"]}
    local["messages"] = [_msg(1, 900, "a"), second]
    mars_sync.save_state(State(cursor=Cursor(800, 0)), s)
    run(mars_sync.sync_once(s))
    assert [h["message"] for h in pushed["sent"]] == ["a", "b"]


def test_sync_all_groups_asks_for_unnamed_groups(settings, monkeypatch):
    """无名群丢掉后,改名会被当成全新群从 latest 起跳。"""
    seen = {}

    def list_groups(search="", include_unnamed=False, limit=200):
        seen["include_unnamed"] = include_unnamed
        return [GROUP]

    monkeypatch.setattr(messages, "list_groups", list_groups)
    s = settings.model_copy(update={"mars_sync_all_groups": True, "mars_sync_groups": ""})
    rids, names, missing = mars_sync.target_rids(s)
    assert seen["include_unnamed"] is True
    assert rids == [GROUP["rid"]]
    assert names == [GROUP["name"]]
    assert missing == []


def test_sync_all_groups_gives_unnamed_groups_a_placeholder_name(settings, monkeypatch):
    unnamed = {"rid": 10796022576028616, "name": "", "sort_time": 80}
    monkeypatch.setattr(messages, "list_groups", lambda search="", **kw: [unnamed])
    s = settings.model_copy(update={"mars_sync_all_groups": True, "mars_sync_groups": ""})
    rids, names, missing = mars_sync.target_rids(s)
    assert rids == [unnamed["rid"]]
    assert names == ["未命名群-10796022576028616"]
    assert missing == []


def test_whitelist_can_select_an_unnamed_group_by_rid(settings, monkeypatch):
    unnamed = {"rid": 10796022576028616, "name": "", "sort_time": 80}

    def list_groups(search="", include_unnamed=False, limit=200):
        return [unnamed] if include_unnamed else []

    monkeypatch.setattr(messages, "list_groups", list_groups)
    s = settings.model_copy(update={"mars_sync_groups": "10796022576028616"})
    rids, names, missing = mars_sync.target_rids(s)
    assert rids == [unnamed["rid"]]
    assert names == ["未命名群-10796022576028616"]
    assert missing == []


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


def test_v1_cursor_is_copied_to_each_group_before_sync(settings, local, pushed):
    s = settings.model_copy(update={"mars_sync_groups": "客户群A,kodo-alert-p1"})
    s.mars_state_file.write_text(
        '{"cursor":{"send_time":800,"lid":7},"pushed":3}', encoding="utf-8"
    )

    run(mars_sync.sync_once(s))

    state = mars_sync.load_state(s)
    assert state.version == 3
    assert {group.cursor for group in state.groups.values()} == {Cursor(800, 7)}
    assert all(group.media_cutover == Cursor(800, 7) for group in state.groups.values())
    assert s.mars_state_file.with_name(f"{s.mars_state_file.name}.v1.bak").exists()


def test_dry_run_does_not_persist_v1_migration(settings, local, pushed):
    settings.mars_state_file.write_text(
        '{"cursor":{"send_time":800,"lid":7}}', encoding="utf-8"
    )

    run(mars_sync.sync_once(settings, dry_run=True))

    assert '"version"' not in settings.mars_state_file.read_text(encoding="utf-8")


def test_reactivated_group_keeps_an_explicit_gap_pointer(settings, local):
    rid = GROUP["rid"]
    state = State(
        version=2,
        groups={str(rid): GroupState(cursor=Cursor(800, 0), active=False)},
    )
    local["messages"] = [_msg(1, 900, "离开期间")]

    got = mars_sync.reconcile_state(state, [rid], settings, persist=False)

    group = got.groups[str(rid)]
    assert group.active is True
    assert group.cursor == Cursor(9000, 9)
    assert group.gap_from == Cursor(800, 0)
    assert group.gap_messages == 1


def test_state_write_failure_is_not_silently_ignored(settings, monkeypatch):
    monkeypatch.setattr(mars_sync.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(MarsStateError, match="disk full"):
        mars_sync.save_state(State(version=2), settings)


def test_corrupt_state_fails_closed_instead_of_skipping_backlog(settings):
    settings.mars_state_file.parent.mkdir(parents=True, exist_ok=True)
    settings.mars_state_file.write_text("{ not json", encoding="utf-8")
    with pytest.raises(MarsStateError):
        mars_sync.load_state(settings)


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
    assert body["config"]["robot"] == SELF_VID
    assert body["running"] is False


def test_status_exposes_corrupt_state_without_skipping_it(client, local):
    settings = get_settings()
    settings.mars_state_file.write_text("{ broken", encoding="utf-8")
    r = client.get("/api/v1/mars/sync/status")
    assert r.status_code == 200
    assert "损坏" in r.json()["state_error"]
    assert r.json()["state"] is None


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


def test_watermark_can_resume_the_saved_tombstone_gap(client, local):
    settings = get_settings()
    rid = GROUP["rid"]
    state = State(
        version=3,
        groups={
            str(rid): GroupState(
                cursor=Cursor(9000, 9), media_cutover=Cursor(800, 0), active=True,
                gap_from=Cursor(800, 0), gap_messages=3,
            )
        },
    )
    state.refresh_shadow_cursor()
    mars_sync.save_state(state, settings)

    r = client.post(
        "/api/v1/mars/sync/watermark",
        json={"rid": rid, "resume_from_tombstone": True},
    )

    assert r.status_code == 200
    assert r.json()["changes"][str(rid)]["new"] == {"send_time": 800, "lid": 0}
    group = mars_sync.load_state(settings).groups[str(rid)]
    assert group.cursor == Cursor(800, 0) and group.gap_from is None


def test_preview_shows_the_exact_hook_body(client, local):
    local["messages"] = [_msg(1, 9500, "待推")]
    mars_sync.save_state(State(cursor=Cursor(9000, 0)), get_settings())
    r = client.post("/api/v1/mars/sync/preview")
    assert r.status_code == 200
    pending = r.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["hook"]["message"] == "待推"
    assert pending[0]["hook"]["groupNumber"] == "wework-20614687431404"


def test_preview_shows_the_frozen_hook_not_a_fresh_one(client, local, monkeypatch):
    """preview 的意义就是「下一轮会推什么」。落过 outbox 的推的是冻结的那份。"""
    settings = get_settings()
    msg = {**_msg(1, 9500, "[图片]"), "raw": b"tree"}
    local["messages"] = [msg]
    mars_sync.save_state(State(cursor=Cursor(9000, 0)), settings)
    monkeypatch.setattr(mars_sync, "to_hook", lambda _m, _s: {"message": "冻结前"})
    mars_sync.outbox_hook(msg, settings)
    monkeypatch.setattr(mars_sync, "to_hook", lambda _m, _s: {"message": "重算后"})

    pending = client.post("/api/v1/mars/sync/preview").json()["pending"]
    assert [p["hook"]["message"] for p in pending] == ["冻结前"]


def test_start_stop_toggle_the_loop(client, local):
    assert client.post("/api/v1/mars/sync/start").json()["running"] is True
    assert client.post("/api/v1/mars/sync/stop").json()["running"] is False
