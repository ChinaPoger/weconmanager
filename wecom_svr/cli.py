"""本地发送 CLI —— 不启服务也能用,等价于原 wecom_local_send.py 的命令行。

    wecom-svr-local --search "notify"           # 搜索会话
    wecom-svr-local --rid 3395103030 "测试"     # 按 RID 发消息
    wecom-svr-local --rid 3395103030 --image /tmp/a.png
    wecom-svr-local --rid 3395103030 --open-only
    wecom-svr-local --group "陈涛,陈挺" --group-name "临时讨论组"   # 拉群
    wecom-svr-local --rid 3395103030 --rename "客户群-新名字"       # 改群名

拉群/改名和 HTTP 那两个端点走的是同一个 local_group,行为完全一致(包括选人前
逐个核对、改完回库核验)。CLI 这条路存在的意义是: 不用先起服务、也不用配 token。
"""
import argparse
import json
import sys

from .local_group import create_group, rename_group
from .local_send import (
    LocalSendError,
    list_conversations,
    open_conversation_by_rid,
    send_image,
    send_message,
)


def _print_convs(convs: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(convs, ensure_ascii=False, indent=2))
        return
    for c in convs:
        print(f"  RID={c['rid']:>16}  type={c['type']}  {c['name']}")


def _create_group(a: argparse.Namespace) -> None:
    """拉群 —— 成功时把「谁被选中了」逐个打出来。

    选人是按名字在客户端通讯录里搜的,同名同姓很常见,所以「建成了」这句话本身
    信息不够: 得让人看见到底拉进来的是哪个部门的哪个人。
    """
    members = [m.strip() for m in a.group.split(",") if m.strip()]
    result = create_group(members, name=a.group_name or "")

    if result.get("success") and not a.json:
        for m in result.get("members", []):
            dept = f"({m['dept']})" if m.get("dept") else ""
            print(f"  ✓ {m['name']}{dept}  ← 搜的是「{m['query']}」")
        # 群建出来了但名字没改上 —— 不能被一句「已建群」盖过去,那是要人手工补的
        if result.get("name_error"):
            print(f"⚠️  {result['name_error']}")

    _finish(result, a.json, "✅ 已建群(含自己共 {member_count} 人)")


def _finish(result: dict, as_json: bool, ok_msg: str) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("success"):
        print(ok_msg.format(**result))
    else:
        print(f"❌ {result.get('error')}")
    if not result.get("success"):
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="企业微信本地操作(以本人身份):发消息 / 发图 / 查会话 / 拉群 / 改群名")
    ap.add_argument("--name", "-n", help="按名称查找会话")
    ap.add_argument("--rid", "-r", type=int, help="按 RID 查找会话")
    ap.add_argument("--search", "-s", help="搜索会话列表")
    ap.add_argument("--list", "-l", action="store_true", help="列出最近会话")
    ap.add_argument("--open-only", action="store_true", help="只按 RID 打开会话,不发送")
    ap.add_argument("--image", help="发送本地图片文件路径(必须配合 --rid)")
    ap.add_argument("--group", "-g", metavar="成员1,成员2",
                    help="以本人身份拉群:成员逗号分隔,除自己外至少 2 个")
    ap.add_argument("--group-name", help="拉群时顺便把群名改掉(可选)")
    ap.add_argument("--rename", metavar="新群名",
                    help="给一个已有的群改名(必须配合 --rid)")
    ap.add_argument("--json", "-j", action="store_true", help="JSON 输出")
    ap.add_argument("content", nargs="?", help="消息内容 (配合 --name 或 --rid)")
    a = ap.parse_args()

    try:
        if a.list or a.search:
            _print_convs(list_conversations(a.search or ""), a.json)
            return

        if a.group:
            _create_group(a)
            return

        if a.rename:
            if not a.rid:
                sys.exit("❌ --rename 需要配合 --rid 指定改哪个群")
            _finish(rename_group(a.rid, a.rename), a.json,
                    "✅ 已改名「{name}」(RID={rid},原名「{old_name}」)")
            return

        if a.open_only:
            if not a.rid:
                sys.exit("❌ --open-only 需要配合 --rid")
            _finish(open_conversation_by_rid(a.rid), a.json,
                    "✅ 已打开「{conv_name}」(RID={conv_rid})")
            return

        if a.image:
            if not a.rid:
                sys.exit("❌ --image 需要配合 --rid")
            _finish(send_image(image_path=a.image, conv_rid=a.rid), a.json,
                    "✅ 已发送图片到「{conv_name}」(RID={conv_rid})")
            return

        if not a.content:
            ap.print_help()
            sys.exit(1)

        _finish(send_message(to=a.name or "", content=a.content, conv_rid=a.rid), a.json,
                "✅ 已发送到「{conv_name}」(RID={conv_rid}, 跳过 {skipped_users} 个同名用户)")

    except LocalSendError as e:
        sys.exit(f"❌ {e}")


if __name__ == "__main__":
    main()
