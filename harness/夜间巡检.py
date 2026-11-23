#!/usr/bin/env python3
"""
夜间巡检：在一个玩具课题上不停地跑完整科研流程，并由独立审查者找出实验、写作、图表、PPT 和 harness 本身的问题。
只找问题、不修改 harness；问题全部追加到 巡检/问题日志_<日期>.md，早上看这一份即可。

每个循环：
  1. harness 跑 N 轮自主实验（额度用完时 harness 自己暂停恢复）
  2. 体检（脚本，不花额度）
  3. 写作 + 绘图 + PPT 样稿（无头 Claude，按 my-research-style）
  4. PPT 渲染成图片
  5. 独立审查者（只读，新会话）对照三份规则挑问题，追加到问题日志

用法：
  nohup python 夜间巡检.py <课题目录> --until 09:00 > 巡检.out 2>&1 &
只在 --until 截止时刻停止；配合 守护巡检.sh，进程意外退出也会自动重启。
"""
import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import harness as H  # noqa: E402  复用 find_claude、额度识别

LOG_DIR = ROOT / "巡检"

# —— 写作 / 绘图 / PPT 样稿的指令 ——
DRAFT_PROMPT = """先加载 my-research-style skill，严格按它和 references/偏好检查表.md 执行。
任务：根据本课题现有的实验结果（findings.md、research-state.yaml、hypotheses/*/result.md 和结果 JSON），在 drafts/ 下生成第 {v} 版样稿：
1. drafts/论文_Results_v{v}.md：论文 Results 小节英文初稿（3–5 段），含一张 LaTeX booktabs 结果表；每个数字注明来源文件。
2. drafts/作图_v{v}.py：用 matplotlib 生成一张结果图，保存为 drafts/图_v{v}.pdf 和 drafts/图_v{v}.png；然后运行它。
3. drafts/汇报_v{v}.json：PPT 内容（≤5 页，给导师看，英文）；运行 `python 汇报PPT.py drafts/汇报_v{v}.json drafts/汇报_v{v}.pptx --严格 --预览`，[检查] 问题改完，再用 Read 看总览 PNG 把版面问题改完（不要自己用 python-pptx 画版面）。
只写 drafts/ 目录，不改实验文件，不跑训练，不改 research-state.yaml。最后 git add drafts/ 并提交。
如果还没有可用结果，就如实写"暂无结果"的最小版本。
这是无人值守运行：脚本必须由你自己运行出结果；某条命令被拒就拆成简单的单条命令重试，不要把命令留给人跑。
硬性步骤（没做不算完成）：写 Results 段落前调用 sentence-mimic，交付物附逐句职责表；PPT、图交付前调用 glance-check 一次，在样稿说明里列出它的发现和处理（与 my-research-style 覆盖表冲突的按覆盖表）。"""

# —— 独立审查者的指令（只读）——
AUDIT_PROMPT = """你是严格的独立审查者，只读不改。目标：找出这个自主科研系统在"实验、写作、图表、PPT、harness 流程"上的问题。
用户已确认的版式（不算问题，不要报）：KU 模板黑底 Take-aways 收尾页（3 条要点）、内容页标题下一行引导问题、页旁术语解释（术语加粗一行 + 白话一行）。
审查对象（课题目录：当前目录）：
- 最新体检报告：{health}
- 实验过程：logs/harness/ 下最近的过程记录（.jsonl，可用 Grep 搜 tool_use / 守卫拦截 / 实验.py）、hypotheses/*/protocol.md 与 result.md、findings.md、research-state.yaml、logs/实验记录.jsonl、git log
- 样稿：drafts/论文_Results_v{v}.md、drafts/作图_v{v}.py 与 drafts/图_v{v}.png（请看图）、drafts/汇报_v{v}_渲染/*.png（PPT 每页的渲染图，请逐页看）
对照的规则：
- 实验：{exp_rules} 和 {loop_skill}
- 写作/图表/表格/PPT：{style_rules}
已有问题（不要重复，除非有新证据说明更严重）：{issue_log}

重点找：
1. 实验：违反预注册或判定规则；基线不公平；种子无随机性；用测试集选参或早停；超出 2 分钟训练约束；换检验救结论；归因缺失；结论强于证据；浪费算力或重复已否定方向。
2. 写作：数字与结果文件不一致或无来源；AI 腔、生僻词、跳跃、重复；防御性写法；结论强于证据。
3. 图表：违反偏好检查表 C/D 类（字体、颜色、图注下结论、坐标轴名、图例遮挡、装饰、粗体）。
4. PPT：违反 E 类（页数、标题不是结论、字太多、导师一眼看不懂、版式简陋）。
5. harness 流程本身：规则不清导致 AI 犹豫或误解、白名单/守卫误拦、状态文件问题、简报缺信息、无谓停机、耗时浪费。

输出格式（只输出这个列表，最多 15 条，按严重度排序；没有新问题就输出"本轮无新问题"）：
- [类别：实验/写作/图表/PPT/harness][严重度：高/中/低] 问题一句话 · 证据：文件路径或轮次 + 原文/数字 · 建议：改哪个文件（如 autoresearch-loop SKILL.md 第 X 节、守卫.py、my-research-style）以及怎么改
每条必须有可核对的证据，不要编造；拿不准的标"待核实"。"""


def load_prompts():
    """每个循环重新读取指令文件（巡检/提示_样稿.md、巡检/提示_审查.md），改指令无需重启；文件不存在就用代码里的默认。"""
    d, a = DRAFT_PROMPT, AUDIT_PROMPT
    f1, f2 = LOG_DIR / "提示_样稿.md", LOG_DIR / "提示_审查.md"
    if f1.exists():
        d = f1.read_text(encoding="utf-8")
    if f2.exists():
        a = f2.read_text(encoding="utf-8")
    return d, a


def now():
    return dt.datetime.now()


def say(msg):
    line = f"[{now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "巡检.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_claude(proj: Path, claude: str, prompt: str, allowed: list, tag: str, timeout_min=40) -> str:
    """调用无头 Claude；额度用完就等，最多重试 3 次。返回最终回复文本（失败返回空串）。"""
    for _ in range(3):
        cmd = [claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--permission-mode", "dontAsk", "--settings", str(proj / ".claude" / "settings.json"),
               "--model", "claude-opus-5-5", "--allowedTools", ",".join(allowed)]
        for d in H.extra_dirs(proj):
            cmd += ["--add-dir", d]
        try:
            r = H.run_child(cmd, cwd=proj, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            timeout=timeout_min * 60)
        except subprocess.TimeoutExpired:
            say(f"{tag} 超时")
            return ""
        out_dir = proj / "logs" / "巡检"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{tag}_{now():%m%d_%H%M%S}.jsonl").write_text(r.stdout or "", encoding="utf-8")
        info = {}
        for ln in (r.stdout or "").splitlines()[::-1]:
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            if ev.get("type") == "result":
                info = ev
                break
        text = str(info.get("result", "")) + (r.stderr or "")
        if (r.returncode != 0 or info.get("is_error")) and H.QUOTA_PATTERNS.search(text):
            wait = H.parse_reset_wait(text)
            say(f"{tag} 遇到额度/限流，暂停 {wait // 60} 分钟")
            time.sleep(wait)
            continue
        say(f"{tag} 完成（{info.get('duration_ms', 0) / 60000:.1f} 分钟，≈${info.get('total_cost_usd')}）")
        return str(info.get("result", ""))
    return ""


def render_ppt(proj: Path, v: int) -> None:
    """PPT → PDF → 每页 PNG，供审查者看图。"""
    pptx = proj / "drafts" / f"汇报_v{v}.pptx"
    if not pptx.exists():
        say(f"未找到 {pptx.name}，跳过渲染")
        return
    out = proj / "drafts" / f"汇报_v{v}_渲染"
    out.mkdir(exist_ok=True)
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(out), str(pptx)],
                   capture_output=True, timeout=300)
    pdf = out / f"汇报_v{v}.pdf"
    if pdf.exists():
        subprocess.run(["pdftoppm", "-png", "-r", "60", str(pdf), str(out / "第")], capture_output=True, timeout=300)



# —— 巡检模式：研究停下时自动处理，保证一直测到截止时刻 ——
import re

# 研究判定达标后依次换的新目标（同一份 CIFAR-10 数据，难度和侧重点不同）
VARIANTS = [
    ("玩具课题_CIFAR一分钟", "在单张 RTX 4090 上、每次训练（含推理）墙钟时间 ≤ 1 分钟、参数量 ≤ 1M 的约束下，把 CIFAR-10 测试准确率做到显著优于同等约束、同等调参的基线，并尽量逼近 94%。", None),
    ("玩具课题_CIFAR少样本", "只用 5000 张训练图像（从训练集固定抽取）、每次训练 ≤ 2 分钟，把 CIFAR-10 测试准确率做到显著优于同等调参的 ResNet-18 基线。", "少样本"),
    ("玩具课题_CIFAR噪声鲁棒", "每次训练 ≤ 2 分钟，在加了高斯噪声（σ=0.1，固定种子）的 CIFAR-10 测试集上，把准确率做到显著优于同等调参的 ResNet-18 基线。", "噪声"),
]


def _set_state(pj: Path, status: str):
    """只改 status 与 approval_request 两行，保留注释。"""
    sp = pj / "research-state.yaml"
    t = sp.read_text(encoding="utf-8")
    t = re.sub(r"(?m)^status:.*$", f"status: {status}", t)
    t = re.sub(r'(?m)^approval_request:.*$', 'approval_request: ""', t)
    sp.write_text(t, encoding="utf-8")


def _log_issue(issue_log: Path, text: str):
    with open(issue_log, "a", encoding="utf-8") as f:
        f.write(f"\n## 巡检自动处理（{now():%m-%d %H:%M}）\n{text}\n")
    say(text.splitlines()[0][:120])


def new_variant_project(base: Path, name: str, goal_text: str, kind):
    """用模板建新课题，评估代码复制当前课题并按变体改动。"""
    dst = base.parent / name
    if dst.exists():
        return dst
    subprocess.run(["bash", str(ROOT / "scripts" / "新建课题.sh"), str(dst)], capture_output=True)
    ev = (base / "eval" / "评估.py").read_text(encoding="utf-8")
    if kind == "少样本":
        ev = ev.replace("    va, trn = idx[:N_VAL], idx[N_VAL:]",
                        "    va, trn = idx[:N_VAL], idx[N_VAL:N_VAL + 5000]  # 少样本：只用 5000 张训练图像")
    if kind == "噪声":
        ev = ev.replace("    return (x[trn], y[trn]), (x[va], y[va]), (te.data, np.asarray(te.targets, dtype=np.int64))",
                        "    xte = te.data.astype(np.float32) / 255.0\n"
                        "    xte = np.clip(xte + np.random.RandomState(7).normal(0, 0.1, xte.shape), 0, 1)\n"
                        "    xte = (xte * 255).round().astype(np.uint8)  # 测试集加固定高斯噪声 σ=0.1\n"
                        "    return (x[trn], y[trn]), (x[va], y[va]), (xte, np.asarray(te.targets, dtype=np.int64))")
    (dst / "eval" / "评估.py").write_text(ev, encoding="utf-8")
    (dst / "eval" / "__init__.py").touch()
    g = (base / "goal.yaml").read_text(encoding="utf-8")
    g = re.sub(r"(?ms)^goal: >-\n.*?\n\n", "goal: >-\n  " + goal_text + "\n\n", g, count=1)
    # 变体的时间上限与边界按目标调整，避免沿用上一个课题的约束（巡检 07:20 发现的矛盾）
    if "1 分钟" in goal_text:
        g = re.sub(r"max_run_seconds: \d+.*", "max_run_seconds: 75    # 训练 ≤60 s + 余量", g)
        g = g.replace("每次训练墙钟时间 ≤ 2 分钟", "每次训练墙钟时间 ≤ 1 分钟")
    if "1M" in goal_text and "参数量 ≤ 1M" not in g.split("boundaries:")[-1]:
        g = g.replace("  - 每次训练墙钟时间", "  - 模型参数量 ≤ 1M（在 result.md 写明参数量）\n  - 每次训练墙钟时间", 1)
    (dst / "goal.yaml").write_text(g, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=dst)
    subprocess.run(["git", "commit", "-qm", f"巡检自动建立变体课题：{name}"], cwd=dst)
    return dst


def keep_running(pj: Path, issue_log: Path) -> Path:
    """研究不在 running 时自动处理：达标就换新目标，请求批准就自动批准（测试用），超预算就追加。"""
    try:
        st = H.load_yaml(pj / "research-state.yaml")
    except Exception as e:  # 状态文件坏了：交给 harness 修复，这里不处理
        _log_issue(issue_log, f"- [harness] {pj.name} 的 research-state.yaml 解析失败，交给 harness 修复：{str(e)[:160]}")
        return pj
    status, req = st.get("status", "running"), st.get("approval_request", "")
    # harness 的 max_iterations 改为课题累计上限后（11-22），巡检测试模式到上限就追加 10 轮
    try:
        g = (pj / "goal.yaml").read_text(encoding="utf-8")
        m = re.search(r"max_iterations: (\d+)", g)
        if m and int(st.get("iteration", 0) or 0) >= int(m.group(1)):
            (pj / "goal.yaml").write_text(g.replace(m.group(0), f"max_iterations: {int(m.group(1)) + 10}", 1), encoding="utf-8")
            _log_issue(issue_log, f"- [巡检] {pj.name} 到达 max_iterations={m.group(1)}，测试模式追加 10 轮")
    except Exception as e:
        _log_issue(issue_log, f"- [巡检] 追加迭代上限失败：{e}")
    if status == "running":
        return pj
    if status == "done":
        for name, goal_text, kind in VARIANTS:
            if not (pj.parent / name).exists():
                nxt = new_variant_project(pj, name, goal_text, kind)
                _log_issue(issue_log, f"- [巡检] {pj.name} 判定 done，换到新目标课题 {nxt.name}（检查：done 判定是否站得住，见该课题 findings.md）")
                return nxt
        # 变体都用完了：把当前课题改回 running，要求继续逼近更高目标
        _set_state(pj, "running")
        _log_issue(issue_log, f"- [巡检] 所有变体都已用过，{pj.name} 从 done 改回 running 继续")
        return pj
    # needs_approval：测试模式自动批准，并记录请求内容供早上审查
    if re.search(r"预算|budget|GPU 分钟|gpu_hours", str(req), re.I):
        g = (pj / "goal.yaml").read_text(encoding="utf-8")
        g = re.sub(r"max_gpu_hours: (\d+)", lambda m: f"max_gpu_hours: {int(m.group(1)) + 6}", g)
        (pj / "goal.yaml").write_text(g, encoding="utf-8")
        extra = "，已把 max_gpu_hours 追加 6"
    else:
        extra = ""
    _set_state(pj, "running")
    with open(pj / "findings.md", "a", encoding="utf-8") as f:
        f.write(f"\n- {now():%m-%d %H:%M} · 巡检模式自动批准停机请求（测试用）：{req}\n")
    subprocess.run(["git", "add", "-A"], cwd=pj)
    subprocess.run(["git", "commit", "-qm", "巡检模式自动批准停机请求"], cwd=pj)
    _log_issue(issue_log, f"- [巡检] {pj.name} 停机请求被自动批准{extra}。请求原文：{req}（检查：这个停机是否必要、理由是否充分）")
    return pj


def auto_approve_full(pj: Path, issue_log: Path):
    """pending_approvals 里请求 Full 的假设，自动建批准文件，让 Full 路径也被测到。"""
    try:
        st = H.load_yaml(pj / "research-state.yaml")
    except Exception:
        return
    for item in st.get("pending_approvals") or []:
        txt = json.dumps(item, ensure_ascii=False)
        m = re.search(r"\b(H\d+)\b", txt)
        if m and re.search(r"full|Full|FULL|晋级", txt):
            ok = pj / "approvals" / f"{m.group(1)}.ok"
            if not ok.exists():
                ok.parent.mkdir(exist_ok=True)
                ok.write_text(f"巡检模式自动批准 {now():%m-%d %H:%M}\n", encoding="utf-8")
                _log_issue(issue_log, f"- [巡检] 自动批准 {pj.name} 的 {m.group(1)} Full 实验。请求：{txt[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--until", default="09:00", help="截止时刻 HH:MM（到点后不再开始新循环）")
    ap.add_argument("--iters", type=int, default=4, help="每个循环 harness 跑几轮")
    a = ap.parse_args()

    proj = Path(a.project).resolve()
    LOG_DIR.mkdir(exist_ok=True)
    hh, mm = map(int, a.until.split(":"))
    end = now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    if end <= now():
        end += dt.timedelta(days=1)
    claude = H.find_claude()
    # 与 harness 一致：课题指定的 Python 环境放到 PATH 最前（作图、PPT 脚本要用）
    goal = H.load_yaml(proj / "goal.yaml")
    env_bin = (goal.get("env") or {}).get("python_bin")
    if env_bin:
        os.environ["PATH"] = os.path.expanduser(env_bin) + os.pathsep + os.environ["PATH"]
    issue_log = LOG_DIR / f"问题日志_{now():%Y%m%d}.md"
    if not issue_log.exists():
        issue_log.write_text(f"# 夜间巡检问题日志 {now():%Y-%m-%d}\n\n课题：{proj}\n\n", encoding="utf-8")
    allow_all = json.loads((proj / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]["allow"]
    allow_ro = ["Read", "Glob", "Grep", "Bash(git log*)", "Bash(ls *)", "Bash(cat *)", "Bash(head *)", "Bash(tail *)", "Bash(wc *)"]
    rules = {
        "exp_rules": str(ROOT / "偏好整理" / "实验要求清单_定稿.md"),
        "loop_skill": str(ROOT / "skills" / "autoresearch-loop" / "SKILL.md"),
        "style_rules": str(ROOT / "skills" / "my-research-style" / "references" / "偏好检查表.md"),
    }
    say(f"开始巡检：{proj.name}，截止 {end:%m-%d %H:%M}，问题日志 {issue_log}")
    # 循环编号接着已有样稿往下编，守护进程重启后不覆盖旧样稿
    def next_v(pj):
        vs = [int(x) for x in re.findall(r"论文_Results_v(\d+)\.md", " ".join(os.listdir(pj / "drafts"))) ] \
            if (pj / "drafts").exists() else []
        return max(vs, default=0)

    # 记住切换后的课题，守护进程重启后接着用；文件里存"起始课题<TAB>当前课题"，
    # 只有这次传入的课题与上次起始课题相同（即守护重启）才沿用（11-22 巡检：换课题启动时被旧记录覆盖）
    state_file = LOG_DIR / "当前课题.txt"
    start_proj = proj
    if state_file.exists():
        parts = state_file.read_text().strip().split("\t")
        if len(parts) == 2 and Path(parts[0]) == start_proj and Path(parts[1]).exists():
            proj = Path(parts[1])
    v, last_check = next_v(proj), ""
    cycle = 0

    def one_cycle():
        nonlocal last_check, proj, v
        v = next_v(proj) + 1
        # 0. 研究停下时，巡检模式自动处理，保证流程继续（每次处理都记进问题日志）
        proj = keep_running(proj, issue_log)
        state_file.write_text(f"{start_proj}\t{proj}")
        auto_approve_full(proj, issue_log)
        # 1. 自主实验
        # 研究本身给样稿和审查留出约 10 分钟，避免整个循环越过截止时刻
        H.run_child([sys.executable, str(HERE / "harness.py"), str(proj), "--iters", str(a.iters),
                     "--until", str(end.timestamp() - 600)],
                    cwd=proj, timeout=6 * 3600)
        # 2. 体检
        h = subprocess.run([sys.executable, str(ROOT / "tools" / "体检.py"), str(proj)]
                           + (["--since", last_check] if last_check else []),
                           capture_output=True, text=True)
        last_check = now().isoformat(timespec="seconds")
        health = sorted(glob.glob(str(proj / "logs" / "体检" / "*.md")))[-1]
        st = subprocess.run(["grep", "-m1", "^status:", str(proj / "research-state.yaml")],
                            capture_output=True, text=True).stdout.strip()
        # 4. 样稿 + 渲染 PPT
        draft_p, audit_p = load_prompts()
        run_claude(proj, claude, draft_p.format(v=v), allow_all, f"样稿v{v}")
        render_ppt(proj, v)
        # 4b. 样稿之后（11-22：交接要反映最新样稿），每 3 个循环生成一次简报和研究交接，一并交给审查者
        # 第一个循环、每 3 个循环、或下个循环赶不上截止时，都生成（11-22：只跑 1 个循环的课题一直没有交接）
        if cycle == 1 or cycle % 3 == 0 or (end - now()).total_seconds() < 1800:
            H.run_child([sys.executable, str(HERE / "harness.py"), str(proj), "--简报"], cwd=proj, timeout=3600)
            H.run_child([sys.executable, str(HERE / "harness.py"), str(proj), "--交接"], cwd=proj, timeout=3600)
        # 5. 独立审查
        report = run_claude(proj, claude, audit_p.format(v=v, health=health, issue_log=str(issue_log), **rules),
                            allow_ro, f"审查v{v}", timeout_min=30)
        # 审查结果放进课题目录，下一轮实验和样稿会先读它（审查与研究闭环）
        if report:
            (proj / "logs" / "最新审查.md").write_text(f"# 最新审查（第 {v} 轮，{now():%m-%d %H:%M}）\n\n{report}\n", encoding="utf-8")
        with open(issue_log, "a", encoding="utf-8") as f:
            f.write(f"\n## 第 {v} 轮审查（{now():%m-%d %H:%M}，课题 {proj.name}，{st}）\n\n")
            f.write("### 体检\n" + (h.stdout or "（体检无输出）") + "\n### 审查者发现\n" + (report or "（审查失败或无输出）") + "\n")

    # 只在截止时刻停止；任何异常都记下来，稍等后继续
    while now() < end:
        cycle += 1
        say(f"—— 第 {cycle} 个循环（课题 {proj.name}）——")
        try:
            one_cycle()
        except Exception as e:
            import traceback
            say(f"第 {cycle} 个循环异常：{e!r}")
            with open(issue_log, "a", encoding="utf-8") as f:
                f.write(f"\n## 巡检脚本异常（{now():%m-%d %H:%M}，第 {cycle} 个循环）\n```\n{traceback.format_exc()[-1500:]}\n```\n")
            time.sleep(60)
    say("到达截止时刻，巡检结束")


if __name__ == "__main__":
    main()
