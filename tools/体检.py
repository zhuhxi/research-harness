#!/usr/bin/env python3
"""
harness 体检：只读扫描课题目录的日志与 git 历史，自动找"流程问题"，不调用 Claude、不花额度。

检查项：
  1. 每轮耗时、花费、权限拒绝、守卫拦截、实验.py 拒绝、出错/超时/未推进
  2. 预注册顺序：每个假设的 protocol.md 是否早于结果文件提交
  3. 实验记录：超时、失败的实验；绕过 实验.py 直接跑实验的迹象
  4. 状态：pending_approvals 积压、status 非 running
用法：python 体检.py <课题目录> [--since 上次体检时间ISO]
输出：打印 Markdown，同时写到 logs/体检/体检_<时间>.md
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
from collections import Counter
from pathlib import Path

import yaml


def git(proj, *a):
    return subprocess.run(["git", *a], cwd=proj, capture_output=True, text=True).stdout


def scan_rounds(proj: Path, since: str):
    """逐个读取 logs/harness/*.jsonl，统计每轮的过程指标。"""
    rows = []
    for f in sorted(glob.glob(str(proj / "logs" / "harness" / "*.jsonl"))):
        if since and dt.datetime.fromtimestamp(os.path.getmtime(f)).isoformat() < since:
            continue
        r = {"文件": Path(f).name, "工具调用": 0, "守卫拦截": [], "实验拒绝": [],
             "权限拒绝": 0, "直接python": [], "秒": None, "花费": None, "出错": False}
        for ln in open(f, encoding="utf-8", errors="ignore"):
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            t = ev.get("type")
            if t == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_use":
                        r["工具调用"] += 1
                        cmd = (b.get("input") or {}).get("command", "")
                        # 直接用 python -c / 脚本跑训练类代码而不走 实验.py
                        # 只在实际执行训练脚本，或 python -c 里有训练动作时才报警（读源码、分析不算）
                        if "实验.py" not in cmd and (
                                re.search(r"\bpython3?\s+\S*(训练|train)\S*\.py", cmd) or
                                re.search(r"python3?\s+-c\s+.*(\.backward\(|optimizer\.step|\.fit\()", cmd)):
                            r["直接python"].append(cmd[:100])
            elif t == "user":
                for b in ev.get("message", {}).get("content", []):
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        c = json.dumps(c, ensure_ascii=False) if not isinstance(c, str) else c
                        for m in re.findall(r"\[守卫拦截\][^\\\"\n]{0,80}", c):
                            r["守卫拦截"].append(m)
                        for m in re.findall(r"\[实验\.py 拒绝\][^\\\"\n]{0,80}", c):
                            if "{" not in m:  # 读 实验.py 源码时出现的 f-string 字面量不算
                                r["实验拒绝"].append(m)
            elif t == "result":
                r["被拒命令"] = [(d.get("tool_input") or {}).get("command", d.get("tool_name", ""))[:90]
                               for d in (ev.get("permission_denials") or [])]
                r["秒"] = round(ev.get("duration_ms", 0) / 1000)
                r["花费"] = ev.get("total_cost_usd")
                r["权限拒绝"] = len(ev.get("permission_denials") or [])
                r["出错"] = bool(ev.get("is_error"))
        rows.append(r)
    return rows


def check_prereg(proj: Path):
    """protocol.md 首次提交时间应早于同目录其它文件的首次提交时间。"""
    bad = []
    for d in sorted(glob.glob(str(proj / "hypotheses" / "H*"))):
        rel = os.path.relpath(d, proj)
        def first(path):
            out = git(proj, "log", "--diff-filter=A", "--format=%ct", "--", path).split()
            return int(out[-1]) if out else None
        tp = first(f"{rel}/protocol.md")
        others = [p for p in git(proj, "ls-files", rel).split() if not p.endswith("protocol.md")]
        if tp is None and others:
            bad.append(f"{rel}：有结果文件但 protocol.md 未提交")
            continue
        for p in others:
            to = first(p)
            if to is not None and tp is not None and to < tp:
                bad.append(f"{rel}：{os.path.basename(p)} 早于 protocol.md 提交")
                break
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--since", default="")
    a = ap.parse_args()
    proj = Path(a.project).resolve()
    now = dt.datetime.now()
    if not a.since:  # 默认从上一份体检开始统计，避免旧问题一直留在统计里
        prev = sorted((proj / "logs" / "体检").glob("体检_*.md"), key=os.path.getmtime) if (proj / "logs" / "体检").exists() else []
        if prev:
            a.since = dt.datetime.fromtimestamp(os.path.getmtime(prev[-1])).isoformat(timespec="seconds")

    rows = scan_rounds(proj, a.since)
    try:
        state = yaml.safe_load(open(proj / "research-state.yaml", encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        state = {"status": f"⚠ 状态文件解析失败：{str(e)[:80]}"}
    rec = [json.loads(l) for l in open(proj / "logs" / "实验记录.jsonl", encoding="utf-8")] \
        if (proj / "logs" / "实验记录.jsonl").exists() else []
    if a.since:
        rec = [x for x in rec if x.get("time", "") >= a.since]
    hlog = (proj / "logs" / "harness.log").read_text(encoding="utf-8") if (proj / "logs" / "harness.log").exists() else ""
    hlines = [l for l in hlog.splitlines() if not a.since or l[1:20].replace(" ", "T") >= a.since[:19]]

    out = [f"## 体检 {now:%m-%d %H:%M}（自 {a.since or '开始'}）", ""]
    # 1. 轮次概况
    secs = [r["秒"] for r in rows if r["秒"]]
    cost = round(sum(r["花费"] or 0 for r in rows), 2)
    out.append(f"- 调用 {len(rows)} 次，平均 {sum(secs)/max(len(secs),1)/60:.1f} 分钟，估算花费 ${cost:.2f}")
    errs = [l for l in hlines if re.search(r"轮出错|轮超时|未推进|，停止|连续 3 轮", l)]
    if errs:
        out.append(f"- harness 出错/超时/未推进/停止 {len(errs)} 次：" + "；".join(e[22:90] for e in errs[-5:]))
    g = Counter(m for r in rows for m in r["守卫拦截"])
    e = Counter(m for r in rows for m in r["实验拒绝"])
    pd = sum(r["权限拒绝"] for r in rows)
    if g:
        out.append("- 守卫拦截：" + "；".join(f"{k}（{v} 次）" for k, v in g.most_common(6)))
    if e:
        out.append("- 实验.py 拒绝：" + "；".join(f"{k}（{v} 次）" for k, v in e.most_common(6)))
    if pd:
        # 按命令开头两个词聚合，列出被拒最多的 5 类及示例
        agg = Counter()
        ex = {}
        for r in rows:
            for c in r.get("被拒命令", []):
                k = " ".join(re.sub(r"^cd \S+;?\s*", "", c).split()[:2])
                agg[k] += 1
                ex.setdefault(k, c)
        out.append(f"- 白名单/守卫拒绝 {pd} 次，最多的：" + "；".join(f"`{k}`×{v}（例：{ex[k][:60]}）" for k, v in agg.most_common(5)))
    direct = [c for r in rows for c in r["直接python"]]
    if direct:
        out.append(f"- ⚠ 疑似绕过 实验.py 直接训练 {len(direct)} 次，例：`{direct[0]}`")
    # 2. 预注册顺序
    bad = check_prereg(proj)
    out.append("- 预注册顺序：" + ("全部正常" if not bad else "⚠ " + "；".join(bad)))
    # 3. 实验记录
    if rec:
        st = Counter(x["stage"] for x in rec)
        to = [x for x in rec if x.get("timed_out")]
        fail = [x for x in rec if x.get("returncode") not in (0, None) and not x.get("timed_out")]
        out.append(f"- 实验 {len(rec)} 次（{dict(st)}），超时 {len(to)}，失败 {len(fail)}")
        # 同一次运行只算一类（11-22：被杀的运行既算超时又算超上限，读者以为出了 7 次问题）
        ov = [x for x in rec if x.get("over_limit") and not x.get("timed_out")]
        busy = [x for x in rec if x.get("contended") or x.get("rerun_needed")]
        if busy:
            out.append(f"- 同卡争用 {len(busy)} 次（计时不可信，需空闲时重跑 {sum(1 for x in busy if x.get('rerun_needed'))} 次）；"
                       f"其中超时被杀 {sum(1 for x in busy if x.get('timed_out'))} 次")
        if ov:
            out.append(f"- ⚠ 超过 goal 单次上限 {len(ov)} 次（不含已算进超时的；结果应作废）：" + "；".join(f"{x['h']} {x['minutes']*60:.0f}s" for x in ov[:5]))
    # 4. 状态
    pa = state.get("pending_approvals") or []
    out.append(f"- 状态：{state.get('status')}，第 {state.get('iteration')} 轮，待批准 {len(pa)} 项，"
               f"best={((state.get('best') or {}).get('metric'))}")
    if state.get("status") != "running":
        out.append(f"- ⚠ 状态不是 running：{state.get('approval_request', '')}")
    # 测试集使用次数（封存后由 eval 记录）：同一假设反复用测试集要警惕
    use = proj / "logs" / "测试集使用.jsonl"
    if use.exists():
        recs = [json.loads(l) for l in open(use, encoding="utf-8") if l.strip()]
        cnt = Counter(r.get("h") for r in recs)
        heavy = [f"{h}×{n}" for h, n in cnt.most_common() if n > 6]
        # 区分本次统计区间内的新使用（11-22 巡检：只报累计数，看不出近期有没有新调用）
        new = [r for r in recs if a.since and str(r.get("time", "")) >= a.since]
        out.append(f"- 测试集使用：累计 {sum(cnt.values())} 次、{len(cnt)} 个假设；本次区间内新增 {len(new)} 次"
                   + (f"（{'，'.join(sorted({str(r.get('h')) for r in new}))}）" if new else "")
                   + (f"；⚠ 偏多：{'，'.join(heavy[:5])}" if heavy else ""))
    # state 里登记的阶段 vs 实验记录里实际跑到的最高阶段
    order = {"toy": 0, "pilot": 1, "full": 2}
    allrec = [json.loads(l) for l in open(proj / "logs" / "实验记录.jsonl", encoding="utf-8")] \
        if (proj / "logs" / "实验记录.jsonl").exists() else []
    top = {}
    for x in allrec:
        if order.get(x.get("stage"), -1) > order.get(top.get(x["h"]), -1):
            top[x["h"]] = x["stage"]
    mism = [f"{h.get('id')} 登记 {h.get('stage')}、实际跑到 {top[h.get('id')]}"
            for h in (state.get("hypotheses") or []) if isinstance(h, dict)
            and h.get("id") in top and order.get(h.get("stage"), -1) < order[top[h.get("id")]]]
    if mism:
        out.append("- ⚠ state 阶段登记落后于实验记录：" + "；".join(mism[:5]))
    text = "\n".join(out) + "\n"
    d = proj / "logs" / "体检"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"体检_{now:%m%d_%H%M}.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
