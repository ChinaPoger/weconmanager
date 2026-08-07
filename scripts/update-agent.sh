#!/usr/bin/env bash
# 把 vendor/wecom-agent 更新到上游 main 的最新 commit,并验证兼容性。
#
#   scripts/update-agent.sh            # 更新 + 跑测试(不提交)
#   scripts/update-agent.sh --check    # 只看落后多少,不改动
#   scripts/update-agent.sh --commit   # 更新 + 跑测试,通过则提交 pin
#
# 上游改过目录结构一次(wecom.py → legacy/self-built-app/wecom.py),
# 所以更新后必须跑测试:test_vendor_layout 会捕捉这类破坏性变更。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUB="vendor/wecom-agent"
cd "$REPO"

MODE="${1:-update}"

if [[ ! -e "$SUB/.git" ]]; then
    echo "→ submodule 未初始化,先拉取"
    git submodule update --init "$SUB"
fi

git -C "$SUB" fetch -q origin
OLD="$(git -C "$SUB" rev-parse HEAD)"
NEW="$(git -C "$SUB" rev-parse origin/main)"

if [[ "$OLD" == "$NEW" ]]; then
    echo "✅ 已是最新: ${OLD:0:7}"
    exit 0
fi

BEHIND="$(git -C "$SUB" rev-list --count "$OLD..$NEW")"
echo "上游有 $BEHIND 个新 commit: ${OLD:0:7} → ${NEW:0:7}"
git -C "$SUB" log --oneline "$OLD..$NEW" | sed 's/^/    /'

# 关注会影响本服务的路径变动(wecom.py 位置、decrypt/ 布局)
echo
echo "── 相关文件变动 ──"
git -C "$SUB" diff --name-status "$OLD..$NEW" -- '*wecom.py' 'decrypt/*' \
    | sed 's/^/    /' || true

if [[ "$MODE" == "--check" ]]; then
    echo
    echo "(--check 模式,未改动)"
    exit 0
fi

echo
echo "→ 更新 submodule 到 origin/main"
git submodule update --remote "$SUB"

echo "→ 跑测试验证兼容性"
if ! uv run pytest -q; then
    echo
    echo "❌ 测试失败 —— 上游可能又改了结构。回滚:"
    echo "   git -C $SUB checkout $OLD"
    exit 1
fi

if [[ "$MODE" == "--commit" ]]; then
    git add "$SUB"
    git commit -q -m "chore: 更新 wecom-agent pkg 到 ${NEW:0:7}"
    echo "✅ 已提交 pin 更新"
else
    echo
    echo "✅ 测试通过。提交 pin:"
    echo "   git add $SUB && git commit -m 'chore: 更新 wecom-agent pkg 到 ${NEW:0:7}'"
fi
