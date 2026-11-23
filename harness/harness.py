#!/usr/bin/env python3
"""
Research Harness 主循环（第 1 周版本）

职责：
  1. 反复调用无头 Claude Code，每轮执行一次 autoresearch-loop 迭代；
  2. 每轮都是新会话——研究状态全靠课题目录里的文件，不依赖聊天记录；
  3. 额度用完 / 限流时保存进度、睡到重置后自动继续；
  4. 遇到 needs_approval / done / STOP 文件 / 预算上限就停下。

用法：
  python harness.py <课题目录>               # 正常运行
  python harness.py <课题目录> --iters 1     # 只跑一轮（试跑）
  python harness.py <课题目录> --dry-run     # 只打印将执行的命令
停止：在课题目录里 touch STOP
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import 集群  # noqa: E402  madar SLURM 接口

# —— 子进程管理：被停止时连带停掉子进程组，避免孤儿进程继续占 GPU、写文件 ——
import signal as _signal
_CHILDREN = []


def _kill_children(signum=None, frame=None):
    for p in list(_CHILDREN):
        try:
            os.killpg(p.pid, _signal.SIGTERM)
        except Exception:
            pass
    if signum is not None:
        sys.exit(128 + signum)


def run_child(cmd, timeout=None, **kw):
    """像 subprocess.run 一样运行，但子进程放进独立进程组，本进程被停止时一并终止。"""
    p = subprocess.Popen(cmd, start_new_session=True, **kw)
    _CHILDREN.append(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, _signal.SIGTERM)
        p.communicate()
        raise
    finally:
        _CHILDREN.remove(p)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


for _s in (_signal.SIGTERM, _signal.SIGHUP):
    _signal.signal(_s, _kill_children)


JOB_POLL_MIN = 10  # 等集群作业时每 10 分钟查一次（只用 ssh，不调用 Claude，不耗额度）

# 每轮发给 Claude 的指令：只做一次迭代，按 skill 执行
ITER_PROMPT = (
    "按 autoresearch-loop skill 执行一次科研迭代（只做一次，做完就结束）。"
    "先读 goal.yaml、research-state.yaml、findings.md。"
    "结束前必须更新 research-state.yaml、追加 findings.md 并 git commit。"
    "如果 logs/最新审查.md 存在，先读其中 [实验] 类的高/中问题（如基线不公平、结论强于证据），本轮优先处理。"
    "开头逐条核对 pending_approvals：已经做完或不再需要的立即删掉，别让人去做已经做完的事。"
)

# 状态文件损坏时的修复指令
FIX_PROMPT = ("research-state.yaml 不是合法的 YAML，解析报错：{err}\n"
              "只做一件事：用 Edit 工具把它修成合法 YAML（长文本改用块标量 >- 或加引号），"
              "不改任何内容含义，不跑实验；修完用 python3 -c 'import yaml;yaml.safe_load(open(\"research-state.yaml\"))' 验证，再 git commit。")

# 简报 / 交接：只写报告，不做实验
BRIEF_PROMPT = "按 daily-brief skill 生成简报并 git commit。只写简报，不跑实验，不改 research-state.yaml。"
HANDOFF_PROMPT = "按 research-handoff skill 生成本周期研究交接并 git commit。只写交接，不跑实验，不改 research-state.yaml。"

# 独立审查（11-22 巡检 2 发现：普通运行没有审查，"同等调参"实际 9 组对 6 组也判了支持）
# 有假设新判为"支持"时，起一个只读新会话核对代码与预注册是否一致，结果写进 logs/最新审查.md，下一轮优先处理
REVIEW_PROMPT = (
    "你是独立审查者，只读不改。刚下判定（支持或否定）的假设：{hids}。逐条核对：\n"
    "1. 运行代码与 protocol.md 是否一致：调参网格大小、搜索预算、数据划分、子采样、种子、指标，逐项对照代码行；\n"
    "2. 基线是否与新方法同等对待（同预算、同数据、同种子）；\n"
    "3. 有没有碰测试集，或用同一份验证集既选参又报告最终结论；\n"
    "4. result.md、findings.md 的数字是否与结果 JSON 一致，措辞是否强于证据；\n"
    "5. 统计是否按预注册做（重采样单位、种子数、方向一致）；\n"
    "6. 判“否定”的：新方法是否得到了公平的机会（调参是否充分、选中值是否在网格边缘、实现有没有错），避免误杀方向。\n"
    "输出 Markdown：每个问题一行“[实验][高/中/低] 问题 · 证据（文件:行）· 建议的复验方式（必须换数据划分或新种子，不得在原验证集上重算救回）”。"
    "没有高/中问题就写“未发现高/中问题”，并列出你核对过的项目。只输出审查结果本身。"
)

# 识别"额度用完 / 限流"的关键词（命令行报错文本里出现即视为额度问题）
QUOTA_PATTERNS = re.compile(
    r"usage limit|rate limit|limit reached|quota|overloaded|429|resets? at", re.I
)


def find_claude() -> str:
    """找 claude 可执行文件：环境变量 > PATH > 桌面版自带的最新 CLI。"""
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    p = shutil.which("claude")
    if p and os.path.exists(os.path.realpath(p)):
        return p
    # 桌面版内置 CLI，按版本号取最新
    cands = sorted(glob.glob(os.path.expanduser("~/.claude/remote/ccd-cli/2.*")))
    if cands:
        return cands[-1]
    sys.exit("找不到 claude 命令，请设置环境变量 CLAUDE_BIN")


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def log(proj: Path, msg: str) -> None:
    """同时打印并写入 logs/harness.log，方便第二天查看。"""
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(proj / "logs" / "harness.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse_reset_wait(text: str) -> int:
    """
    尝试从报错文本里解析重置时间，返回需要等待的秒数。
    支持 'limit reached|<unix 时间戳>' 这种格式；解析不出则默认等 30 分钟。
    """
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        wait = int(m.group(1)) - int(time.time()) + 60  # 多等 1 分钟保险
        if 0 < wait < 6 * 3600:
            return wait
    return 30 * 60


def extra_dirs(proj: Path) -> list:
    """goal.yaml 的 env.extra_dirs，加上 harness 仓库本身（规则文件、skill 参考）。"""
    goal = load_yaml(proj / "goal.yaml")
    dirs = [os.path.expanduser(d) for d in ((goal.get("env") or {}).get("extra_dirs") or [])]
    return dirs + [str(Path(__file__).resolve().parent.parent)]


def run_claude(proj: Path, claude: str, args, tag: str, prompt: str) -> str:
    """
    调用一次无头 Claude。tag 用于日志与文件名（如"第 3 轮"、"简报"）。
    返回："ok" / "quota:<等待秒数>" / "error"。原始输出完整存到 logs/harness/ 作为证据。
    """
    cmd = [
        claude, "-p", prompt,
        "--output-format", "stream-json", "--verbose",  # 保存完整过程，供体检和审查使用
        "--permission-mode", "dontAsk",   # 不在白名单里的操作一律拒绝，不会卡住等人
        "--settings", str(proj / ".claude" / "settings.json"),  # 显式加载课题白名单
        "--model", args.model,
    ]
    # 未经"信任"的目录会忽略 settings 里的 allow 白名单（deny 与 hook 仍生效），
    # 所以把白名单直接用 --allowedTools 传入，避免改全局 ~/.claude.json
    allow = json.loads((proj / ".claude" / "settings.json").read_text(encoding="utf-8")) \
        .get("permissions", {}).get("allow", [])
    if allow:
        cmd += ["--allowedTools", ",".join(allow)]
    # 课题目录以外需要读的目录（数据、harness 规则文件）；写操作仍由守卫限制在课题内
    for d in extra_dirs(proj):
        cmd += ["--add-dir", d]
    if args.max_usd:
        cmd += ["--max-budget-usd", str(args.max_usd)]
    if args.dry_run:
        log(proj, "DRY-RUN: " + " ".join(cmd))
        return "ok"

    t0 = time.time()
    try:
        r = run_child(cmd, cwd=proj, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                      timeout=args.iter_timeout_min * 60)
    except subprocess.TimeoutExpired:
        log(proj, f"{tag}超时（>{args.iter_timeout_min} 分钟），视为错误")
        return "error"
    dur = time.time() - t0

    # 保存完整输出
    out_dir = proj / "logs" / "harness"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 完整过程（每行一个事件）存为 .jsonl；末尾追加一行运行元信息
    with open(out_dir / f"{tag}_{dt.datetime.now():%m%d_%H%M%S}.jsonl", "w", encoding="utf-8") as f:
        f.write(r.stdout or "")
        f.write(json.dumps({"type": "harness_meta", "returncode": r.returncode,
                            "seconds": round(dur), "stderr": (r.stderr or "")[-2000:]},
                           ensure_ascii=False) + "\n")

    # 解析最终结果：stream-json 的最后一个 type=result 事件
    info = {}
    for ln in (r.stdout or "").splitlines()[::-1]:
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if ev.get("type") == "result":
            info = ev
            break
    # 额度判断只看最终结果和 stderr，避免实验输出里的字样误判
    text = str(info.get("result", "")) + (r.stderr or "")
    is_err = r.returncode != 0 or info.get("is_error")

    if not info and r.returncode != 0:
        text = (r.stdout or "")[-3000:] + (r.stderr or "")
    if is_err and QUOTA_PATTERNS.search(text):
        return "quota:" + str(parse_reset_wait(text))
    if is_err:
        log(proj, f"{tag}出错（rc={r.returncode}）：{text[-300:]!r}")
        return "error"

    # 记录被拒绝的操作，方便调整白名单
    denials = info.get("permission_denials") or []
    if denials:
        names = [d.get("tool_name", "?") for d in denials]
        log(proj, f"{tag}有 {len(denials)} 次操作被拒：{names}")

    cost = info.get("total_cost_usd")
    log(proj, f"{tag}完成，用时 {dur/60:.1f} 分钟，花费≈{cost}，"
              f"回复末尾：{str(info.get('result',''))[-200:]!r}")
    return "ok"


def resume_on_approval(proj: Path) -> None:
    """课题停在 done / needs_approval 时，若 approvals/ 里有比状态文件更新的批准文件，就恢复为 running。
    11-22 巡检：done 的同时挂着测试集申请，人批准后没有机制让课题继续。"""
    sp = proj / "research-state.yaml"
    try:
        st = load_yaml(sp)
    except Exception:
        return
    if st.get("status") not in ("done", "needs_approval"):
        return
    t_state = os.path.getmtime(sp)
    new = [f.name for f in (proj / "approvals").glob("*.ok") if os.path.getmtime(f) > t_state] \
        if (proj / "approvals").exists() else []
    if not new:
        return
    txt = re.sub(r"(?m)^status:\s*\S+", "status: running", sp.read_text(encoding="utf-8"), count=1)
    sp.write_text(txt, encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", f"harness：检测到新批准 {'、'.join(new)}，恢复运行"], cwd=proj, capture_output=True)
    log(proj, f"检测到新批准文件 {new}，状态恢复为 running")


IDLE_SLEEP_MIN = 15   # 连续 2 轮没跑实验时暂停的分钟数


def _count_runs(proj: Path) -> int:
    """实验记录行数（本机运行 + 集群作业），用来判断这一轮有没有真跑实验。"""
    n = 0
    for f in ("logs/实验记录.jsonl", "logs/作业.jsonl"):
        p = proj / f
        if p.exists():
            n += sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
    return n


def supported_ids(proj: Path) -> set:
    """当前判为"支持"或"否定"的假设编号集合（否定会进"已否定"栏、以后不再尝试，误杀同样要审查）。"""
    try:
        hs = load_yaml(proj / "research-state.yaml").get("hypotheses") or []
    except Exception:
        return set()
    # 模板规定英文值（supported/refuted），AI 有时写中文或混合句（11-22 巡检：只认中文导致审查一次没触发）
    pat = re.compile(r"支持|否定|supported|refuted", re.I)
    return {str(h.get("id")) for h in hs if isinstance(h, dict) and pat.search(str(h.get("verdict", "")))}


def run_review(proj: Path, claude: str, args, hids) -> None:
    """独立只读审查，结果写进 logs/最新审查.md（由 harness 写文件，审查者只读）。失败不影响主循环。"""
    cmd = [claude, "-p", REVIEW_PROMPT.format(hids="、".join(sorted(hids))),
           "--output-format", "json", "--permission-mode", "dontAsk",
           "--allowedTools", "Read,Grep,Glob,Bash(git log:*),Bash(git show:*)", "--model", args.model]
    for d in extra_dirs(proj):
        cmd += ["--add-dir", d]
    t0 = time.time()
    try:
        r = run_child(cmd, cwd=proj, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30 * 60)
        report = str(json.loads(r.stdout or "{}").get("result", "")).strip()
    except Exception as e:
        log(proj, f"独立审查失败：{e}"); return
    if not report:
        log(proj, "独立审查无输出"); return
    out = proj / "logs" / "最新审查.md"
    out.write_text(f"# 最新审查（{dt.datetime.now():%m-%d %H:%M}，对象 {'、'.join(sorted(hids))}）\n\n{report}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", str(out)], cwd=proj, capture_output=True)
    subprocess.run(["git", "commit", "-qm", f"harness：独立审查 {'、'.join(sorted(hids))}"], cwd=proj, capture_output=True)
    log(proj, f"独立审查完成，用时 {(time.time() - t0) / 60:.1f} 分钟：{report[:120]!r}")


def run_report(proj: Path, claude: str, args, tag: str, prompt: str) -> None:
    """生成简报/交接；遇到额度问题等待后重试，最多 3 次。失败不影响主循环。"""
    for _ in range(3):
        res = run_claude(proj, claude, args, tag, prompt)
        if res.startswith("quota"):
            wait = int(res.split(":")[1])
            log(proj, f"{tag}遇到额度/限流，暂停 {wait/60:.0f} 分钟后重试")
            time.sleep(wait)
            continue
        return
    log(proj, f"{tag}多次失败，跳过")


def main():
    ap = argparse.ArgumentParser(description="Research Harness 主循环")
    ap.add_argument("project", help="课题目录（由 新建课题.sh 生成）")
    ap.add_argument("--iters", type=int, default=None, help="最多迭代轮数（默认取 goal.yaml）")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--max-usd", type=float, default=None, help="单轮花费上限（API 计费时有效）")
    ap.add_argument("--iter-timeout-min", type=int, default=90, help="单轮最长分钟数")
    ap.add_argument("--until", type=float, default=None,
                    help="截止时刻（Unix 秒）：剩余时间不够再跑一轮就不开新轮，单轮超时也压到截止前（11-22 巡检：截止 22:30 却跑到 22:55）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--交接", dest="handoff", action="store_true", help="只生成一份研究交接后退出")
    ap.add_argument("--简报", dest="brief", action="store_true", help="只生成一份简报后退出")
    ap.add_argument("--审查", dest="review", nargs="*", default=None, help="只对给定假设（默认：所有判为支持或否定的）做一次独立审查后退出")
    args = ap.parse_args()

    proj = Path(args.project).resolve()
    (proj / "logs").mkdir(exist_ok=True)
    goal = load_yaml(proj / "goal.yaml")
    budget = goal.get("budget", {})
    # max_iterations 是课题累计上限（11-22 巡检发现：原先只限单次运行，重启 harness 就能无限超出）
    goal_max = int(budget.get("max_iterations", 30))
    try:
        cur_it = int(load_yaml(proj / "research-state.yaml").get("iteration", 0) or 0)
    except Exception:
        cur_it = 0
    remaining = max(0, goal_max - cur_it)
    max_iters = min(args.iters, remaining) if args.iters else remaining
    if max_iters == 0 and not (args.review is not None or args.handoff or args.brief):
        log(proj, f"已到 goal.yaml 的 max_iterations={goal_max}（当前第 {cur_it} 轮），不再迭代；要继续请由你调高 max_iterations")
    max_hours = budget.get("max_wall_hours", 72)
    os.environ["CUDA_VISIBLE_DEVICES"] = str(budget.get("gpus", "0"))
    # 课题指定的 Python 环境：把它的 bin 放到 PATH 最前，让实验里的 python 用这个环境
    env_bin = (goal.get("env") or {}).get("python_bin")
    if env_bin:
        os.environ["PATH"] = os.path.expanduser(env_bin) + os.pathsep + os.environ["PATH"]

    claude = find_claude()
    if args.review is not None:
        run_review(proj, claude, args, set(args.review) or supported_ids(proj))
        return
    if args.handoff or args.brief:
        run_report(proj, claude, args, "交接" if args.handoff else "简报",
                   HANDOFF_PROMPT if args.handoff else BRIEF_PROMPT)
        return
    log(proj, f"启动：claude={claude}，最多 {max_iters} 轮 / {max_hours} 小时")
    t_start = time.time()
    resume_on_approval(proj)
    done_iters, consecutive_err, idle_streak = 0, 0, 0
    brief_day, since_brief = dt.date.today(), 0   # 每天一份简报：跨天且有新迭代时生成
    stop_reason = ""

    while done_iters < max_iters:
        # —— 停止条件检查 ——
        if (proj / "STOP").exists():
            log(proj, "发现 STOP 文件，停止"); stop_reason = "stop"; break
        if (time.time() - t_start) / 3600 > max_hours:
            log(proj, "达到连续运行时长上限，停止"); stop_reason = "hours"; break
        if args.until:
            # 按已完成轮的平均用时估计下一轮，没有历史时按 15 分钟估计
            avg = (time.time() - t_start) / done_iters if done_iters else 15 * 60
            left = args.until - time.time()
            if left < avg:
                log(proj, f"距截止只剩 {left/60:.0f} 分钟，不够再跑一轮（平均 {avg/60:.0f} 分钟），停止"); stop_reason = "deadline"; break
            # 本轮最长不超过截止时刻
            args.iter_timeout_min = max(5, min(args.iter_timeout_min, int(left / 60)))
        # 跨天：先给前一天写简报
        if dt.date.today() != brief_day and since_brief > 0 and not args.dry_run:
            run_report(proj, claude, args, "简报", BRIEF_PROMPT)
            brief_day, since_brief = dt.date.today(), 0
        try:
            state = load_yaml(proj / "research-state.yaml")
        except yaml.YAMLError as e:
            # 状态文件被写坏：调用一次 Claude 专门修复，计入连续错误，避免死循环
            log(proj, f"research-state.yaml 解析失败，调用修复：{str(e)[:150]}")
            run_claude(proj, claude, args, "修复状态", FIX_PROMPT.format(err=str(e)[:500]))
            consecutive_err += 1
            if consecutive_err >= 3:
                log(proj, "状态文件连续 3 次修复失败，停止"); break
            continue
        status = state.get("status", "running")
        if status != "running":
            log(proj, f"状态为 {status}，停止。请求：{state.get('approval_request','')}")
            stop_reason = status
            break

        # —— 集群作业：先刷新（结束的自动拉回结果）；AI 在等作业时 harness 只轮询不唤醒 ——
        try:
            n_open = 集群.refresh(proj)
        except Exception as e:  # 网络等问题不致命，保守按"仍在运行"处理
            log(proj, f"刷新集群作业失败：{e}")
            n_open = 1 if state.get("waiting_for_jobs") else 0
        if state.get("waiting_for_jobs"):
            if n_open > 0:
                log(proj, f"等待 {n_open} 个集群作业，{JOB_POLL_MIN} 分钟后再查（不调用 Claude）")
                time.sleep(JOB_POLL_MIN * 60)
                continue
            # 作业都结束了：清除等待标记，唤醒 AI 分析
            sp = proj / "research-state.yaml"
            sp.write_text(re.sub(r"(?m)^waiting_for_jobs:\s*true", "waiting_for_jobs: false",
                                 sp.read_text(encoding="utf-8")), encoding="utf-8")
            log(proj, "集群作业已全部结束，结果已同步，唤醒 AI")

        it = int(state.get("iteration", 0)) + 1
        before = supported_ids(proj)
        n_runs0 = _count_runs(proj)
        res = run_claude(proj, claude, args, f"第{it}轮", ITER_PROMPT)

        if res.startswith("quota"):
            # 额度用完：不计入轮数，睡到重置后从同一状态继续
            wait = int(res.split(":")[1])
            log(proj, f"额度/限流，暂停 {wait/60:.0f} 分钟后继续")
            time.sleep(wait)
            continue
        if res == "error":
            consecutive_err += 1
            if consecutive_err >= 3:
                log(proj, "连续 3 轮出错，停止，请人工检查 logs/harness/"); break
            time.sleep(60)
            continue

        # 状态没推进（iteration 未 +1）说明本轮没完成，按出错处理
        try:
            new_it = int(load_yaml(proj / "research-state.yaml").get("iteration", 0))
        except yaml.YAMLError:
            new_it = it  # 本轮把状态写坏了：下一轮开头会触发修复
        if not args.dry_run and new_it < it:
            log(proj, f"第 {it} 轮结束但 research-state.yaml 未推进，按出错处理")
            consecutive_err += 1
            if consecutive_err >= 3:
                log(proj, "连续 3 轮未推进，停止"); break
            continue

        consecutive_err = 0
        done_iters += 1
        # 实验记录与作业记录由 harness 统一提交（AI 不能改这两个文件，也常忘记提交）
        # 只 add 存在的文件：路径不存在时 git add 整条失败（11-22 巡检：纯本机课题没有作业.jsonl，实验记录从未入库）
        recs = [f for f in ("logs/实验记录.jsonl", "logs/作业.jsonl") if (proj / f).exists()]
        if recs:
            subprocess.run(["git", "add", "-f", *recs], cwd=proj, capture_output=True)
        subprocess.run(["git", "commit", "-qm", f"harness：第 {it} 轮实验记录"], cwd=proj, capture_output=True)
        since_brief += 1
        if args.dry_run:
            break
        # 空转退避（11-22 巡检：外部阻塞时 3 分钟内连开 3 轮，每轮只确认"仍被占"）
        if _count_runs(proj) == n_runs0:
            idle_streak += 1
            try:
                still_running = load_yaml(proj / "research-state.yaml").get("status", "running") == "running"
            except Exception:
                still_running = True
            if idle_streak >= 2 and still_running and done_iters < max_iters:   # 已停机或已是最后一轮就不用等
                log(proj, f"连续 {idle_streak} 轮没有跑任何实验，疑似外部阻塞，暂停 {IDLE_SLEEP_MIN} 分钟再继续")
                time.sleep(IDLE_SLEEP_MIN * 60)
        else:
            idle_streak = 0
        new_sup = supported_ids(proj) - before
        if new_sup:                       # 新判"支持"或"否定"：先独立审查，下一轮按审查意见处理
            run_review(proj, claude, args, new_sup)

    # 停下时：有新迭代就补一份简报，让你知道为什么停；完成或用满时长再出一份研究交接
    # 停下时补简报；但距上一份简报不足 3 小时就跳过（巡检每个循环都会停一次，避免重复写）
    briefs = sorted((proj / "logs" / "简报").glob("*.md"), key=os.path.getmtime) if (proj / "logs" / "简报").exists() else []
    recent = briefs and time.time() - os.path.getmtime(briefs[-1]) < 3 * 3600
    # 最好结果变了就不算"最近"：立即补简报，避免导师读到过时结论
    try:
        best_now = str((load_yaml(proj / "research-state.yaml").get("best") or {}).get("metric"))
    except Exception:
        best_now = ""
    mark = proj / "logs" / "简报" / ".上次best"
    if not mark.exists() or mark.read_text().strip() != best_now:
        recent = False
    # 停机（done / needs_approval / 到上限）时一律补简报：简报要说清为什么停、等谁决定（11-22 巡检：done 后简报仍停在第 4 轮）
    if stop_reason in ("done", "needs_approval"):
        recent = False
    if since_brief > 0 and not args.dry_run and not recent:
        run_report(proj, claude, args, "简报", BRIEF_PROMPT)
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(best_now)
    if stop_reason in ("done", "hours") and done_iters > 0 and not args.dry_run:
        run_report(proj, claude, args, "交接", HANDOFF_PROMPT)
    log(proj, f"结束：本次共完成 {done_iters} 轮")


if __name__ == "__main__":
    main()
