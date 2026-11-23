#!/usr/bin/env bash
# 用模板新建一个课题目录：bash 新建课题.sh <目标路径>
# 生成后：填 goal.yaml，把评估代码放 eval/，再运行 harness。
set -euo pipefail
HARNESS="$(cd "$(dirname "$0")/.." && pwd)"
DST="${1:?用法：bash 新建课题.sh <目标路径>}"

[ -e "$DST" ] && { echo "目标已存在：$DST"; exit 1; }
cp -r "$HARNESS/templates/项目" "$DST"
mkdir -p "$DST/eval" "$DST/src" "$DST/.claude/skills"

# skill 用软链接，harness 更新后所有课题自动用新版
for sk in "$HARNESS"/skills/*/; do ln -s "${sk%/}" "$DST/.claude/skills/$(basename "$sk")"; done
# 守卫 hook 与实验入口同样用软链接
mkdir -p "$DST/.claude/hooks" "$DST/approvals" "$DST/baselines"
ln -s "$HARNESS/hooks/守卫.py" "$DST/.claude/hooks/守卫.py"
ln -s "$HARNESS/tools/实验.py" "$DST/实验.py"
ln -s "$HARNESS/tools/汇报PPT.py" "$DST/汇报PPT.py"   # 固定版式的汇报 PPT 工具（KU 模板）
touch "$DST/approvals/.gitkeep" "$DST/baselines/.gitkeep"

cd "$DST"
git init -q
git add -A
git commit -qm "新建课题骨架"
echo "已创建：$DST"
echo "下一步：编辑 goal.yaml；填写 eval/评估.py 的 _load_raw()（测试集默认封存）；然后："
echo "  python $HARNESS/harness/harness.py $DST --iters 1"
