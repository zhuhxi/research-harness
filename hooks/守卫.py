#!/usr/bin/env python3
"""
PreToolUse 守卫 hook：在 Claude 调用工具前检查，违规则退出码 2 拦截（stderr 会反馈给 Claude）。

拦截内容：
  1. 改锁定文件：goal.yaml、eval/、approvals/、logs/实验记录.jsonl，以及 goal.yaml 里 locked_paths 列出的路径（如基线结果）
  2. 改已提交的预注册 protocol.md（判定标准提交后不得改）
  3. 危险命令：rm -rf、git push、改写 git 历史、sudo 等
  4. 用 shell 命令（重定向、sed -i、mv、cp、rm…）间接改锁定文件
"""
import fnmatch
import json
import os
import re
import subprocess
import sys

# 固定锁定的路径（相对课题根目录，glob 语法）
FIXED_LOCKED = ["goal.yaml", "eval/*", "eval/**", "approvals/*", "approvals/**",
                "logs/实验记录.jsonl", "logs/作业.jsonl", "logs/测试集使用.jsonl", "实验.py", "集群.py", ".claude/*", ".claude/**"]

# 危险命令正则：命中即拦截
DANGEROUS = [
    (r"\brm\s+(-\w*r\w*f|-\w*f\w*r)\b", "禁止 rm -rf"),
    (r"\bgit\s+push\b", "禁止 git push"),
    (r"\bgit\s+reset\b", "禁止 git reset（会改写历史）"),
    (r"\bgit\s+rebase\b", "禁止 git rebase（会改写历史）"),
    (r"\bgit\s+commit\b.*--amend", "禁止 git commit --amend（预注册提交不可改）"),
    (r"\bgit\s+(checkout|restore)\b.*(--\s|--source)", "禁止用 git checkout/restore 覆盖文件"),
    (r"\bgit\s+(filter-branch|filter-repo)\b", "禁止改写 git 历史"),
    (r"\bsudo\b", "禁止 sudo"),
    (r"\bsbatch\b", "禁止直接 sbatch，请通过 实验.py --where madar 提交"),
    (r"\bssh\b", "禁止直接 ssh，集群作业请通过 实验.py --where madar 提交"),
    (r"\bscancel\b", "禁止直接 scancel"),
    (r"--account[= ](?!<slurm-account>\b)", "集群账号只允许 <slurm-account>"),
    (r"\bchmod\b.*\b(eval|goal\.yaml|approvals)\b", "禁止改锁定文件权限"),
]

# 命令里出现这些"写操作"并且涉及锁定路径时拦截
WRITE_OPS = r"(>|>>|\bsed\s+-i|\bmv\b|\bcp\b|\brm\b|\btee\b|\btruncate\b|\bln\b|open\(.+['\"][wa])"


def block(msg: str) -> None:
    """拦截：退出码 2，stderr 反馈给 Claude。"""
    print(f"[守卫拦截] {msg}", file=sys.stderr)
    sys.exit(2)


def project_root(cwd: str) -> str:
    """课题根目录：优先 CLAUDE_PROJECT_DIR，否则向上找 goal.yaml。"""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return os.path.realpath(env)
    d = os.path.realpath(cwd)
    while d != "/":
        if os.path.exists(os.path.join(d, "goal.yaml")):
            return d
        d = os.path.dirname(d)
    return os.path.realpath(cwd)


def locked_patterns(root: str) -> list:
    """固定锁定 + goal.yaml 里的 locked_paths（简单行解析，避免依赖 yaml 库）。"""
    pats = list(FIXED_LOCKED)
    try:
        lines = open(os.path.join(root, "goal.yaml"), encoding="utf-8").read().splitlines()
        inside = False
        for ln in lines:
            if re.match(r"^locked_paths\s*:", ln):
                inside = True
                continue
            if inside:
                m = re.match(r"^\s+-\s+[\"']?([^\"'#]+?)[\"']?\s*(#.*)?$", ln)
                if m:
                    pats.append(m.group(1).strip())
                elif ln.strip() and not ln.startswith(" "):
                    inside = False
    except FileNotFoundError:
        pass
    return pats


def rel(root: str, path: str) -> str:
    """把绝对/相对路径转为相对课题根目录的路径。"""
    p = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    return os.path.relpath(p, root)


def is_locked(relpath: str, pats: list) -> bool:
    return any(fnmatch.fnmatch(relpath, p) or relpath == p.rstrip("/*") for p in pats)


def is_committed_protocol(root: str, relpath: str) -> bool:
    """protocol.md 已被 git 跟踪 = 预注册已提交，不得再改。"""
    if os.path.basename(relpath) != "protocol.md" or "_模板" in relpath:
        return False
    r = subprocess.run(["git", "ls-files", "--error-unmatch", relpath],
                       cwd=root, capture_output=True)
    return r.returncode == 0


def main():
    data = json.load(sys.stdin)
    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {}) or {}
    root = project_root(data.get("cwd", os.getcwd()))
    pats = locked_patterns(root)

    # —— 文件写入类工具 ——
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        r = rel(root, path)
        if r.startswith(".."):
            block(f"不得写课题目录以外的文件：{path}")
        if is_locked(r, pats):
            block(f"{r} 是锁定文件（目标/评估/批准/实验记录/基线），只读")
        if is_committed_protocol(root, r):
            block(f"{r} 的预注册已提交，判定标准不得修改；如需修改请新建假设")
        # 测试集封存：实验代码不得绕过 eval 直接加载测试集或读测试标签
        text = str(inp.get("content", "")) + str(inp.get("new_string", ""))
        if re.match(r"(hypotheses|src)/", r) and re.search(
                r"train\s*=\s*False|test_batch|_raw\(\)|\.targets\b.*te\b|_USE_LOG", text):
            block("测试集已封存：实验代码不得直接加载测试集或测试标签，测试指标只能通过 eval.评估 的 score()/correct_mask() 获取")
        return

    # —— Bash ——
    if tool == "Bash":
        cmd = inp.get("command", "")
        for pat, msg in DANGEROUS:
            if re.search(pat, cmd):
                block(msg)
        # 实验必须走 实验.py：直接 python 运行 hypotheses/ 或 src/ 下脚本一律拦截
        if re.search(r"\bpython3?\s+(-\S+\s+)*\S*(hypotheses|src)/\S*(训练|运行|实验|train|run|fit)\S*\.py", cmd, re.I) \
                and "实验.py" not in cmd:
            block("实验须通过 python 实验.py --stage toy|pilot|full --h H编号 -- <命令> 运行")
        # 间接写锁定文件：按 && || ; | 换行 拆成子命令逐段检查，避免一段里的 ">"（如 commit 信息里的邮箱）误伤另一段
        keys = {re.sub(r"/\*+$", "", p) for p in pats}
        keys = {k for k in keys if k and "*" not in k}
        for seg in re.split(r"&&|\|\||;|\||\n", cmd):
            seg = seg.strip()
            if not seg or re.match(r"git\b", seg):
                continue  # git add/commit/log 不改文件内容；危险的 git 操作已在上面拦截
            noq = re.sub(r"'[^']*'|\"[^\"]*\"", "''", seg)  # 去掉引号内文字再判断
            noq = re.sub(r"\d?>&\d|[12]?>\s*/dev/null", " ", noq)  # 2>&1、>/dev/null 不是写文件
            hit = lambda text, k: re.search(r"(^|[\s/'\"=\\(])" + re.escape(k) + r"($|[\s/'\"\\),])", text)
            # 重定向：只检查 > 或 >> 后面的目标
            for tgt in re.findall(r">>?\s*([^\s;&|]+)", noq):
                for k in keys:
                    if hit(" " + tgt + " ", k):
                        block(f"命令里对锁定路径 {k} 有写操作（重定向）")
            # 其他写操作（sed -i、mv、cp、rm、tee、truncate、ln、python 写文件）：涉及锁定路径就拦
            other = re.search(r"\bsed\s+-i|\bmv\b|\bcp\b|\brm\b|\btee\b|\btruncate\b|\bln\b|\btouch\b|\binstall\b|\bdd\b", noq) \
                or re.search(r"open\(.+['\"][wa]", seg)
            if other:
                body = re.sub(r"^\s*python3?\s+实验\.py\b", "", seg)  # 运行 实验.py 不算写它
                for k in keys:
                    if hit(body, k):
                        block(f"命令里对锁定路径 {k} 有写操作")
            if re.search(r"protocol\.md", seg) and re.search(r"(>|\bsed\s+-i|\btee\b)", noq):
                for m in re.finditer(r"([\w./\u4e00-\u9fff-]*protocol\.md)", seg):
                    if is_committed_protocol(root, rel(root, m.group(1))):
                        block(f"{m.group(1)} 的预注册已提交，不得覆盖")

if __name__ == "__main__":
    main()
