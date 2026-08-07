"""本地发送 CLI —— 不启服务也能用,等价于原 wecom_local_send.py 的命令行。

    wecom-svr-local --search "notify"           # 搜索会话
    wecom-svr-local --rid 3395103030 "测试"     # 按 RID 发消息
    wecom-svr-local --rid 3395103030 --image /tmp/a.png
    wecom-svr-local --rid 3395103030 --open-only
"""
import argparse
import json
import sys

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
    ap = argparse.ArgumentParser(description="企业微信本地消息发送(以本人身份)")
    ap.add_argument("--name", "-n", help="按名称查找会话")
    ap.add_argument("--rid", "-r", type=int, help="按 RID 查找会话")
    ap.add_argument("--search", "-s", help="搜索会话列表")
    ap.add_argument("--list", "-l", action="store_true", help="列出最近会话")
    ap.add_argument("--open-only", action="store_true", help="只按 RID 打开会话,不发送")
    ap.add_argument("--image", help="发送本地图片文件路径(必须配合 --rid)")
    ap.add_argument("--json", "-j", action="store_true", help="JSON 输出")
    ap.add_argument("content", nargs="?", help="消息内容 (配合 --name 或 --rid)")
    a = ap.parse_args()

    try:
        if a.list or a.search:
            _print_convs(list_conversations(a.search or ""), a.json)
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
