#!/usr/bin/env python3
"""
实验统一入口：所有实验都通过它运行，由它强制执行晋级规则。

用法（在课题根目录）：
  python 实验.py --stage toy   --h H03 -- python hypotheses/H03_xxx/运行.py --seed 0
  python 实验.py --stage pilot --h H03 -- python ...
  python 实验.py --stage full  --h H03 -- python ...     # 需要 approvals/H03.ok（人工创建）
  python 实验.py --stage pilot --h H03 --where madar -- python ...   # 提交到 madar（账号强制 <slurm-account>），立即返回
  python 实验.py --用量                                  # 查看已用算力

强制检查：
  1. 该假设的 protocol.md 必须已 git 提交且未被修改（先预注册再跑）
  2. 分级限时：toy ≤ budget.toy_minutes，pilot ≤ budget.pilot_minutes
  3. full 必须有人工批准文件 approvals/<H编号>.ok（守卫 hook 禁止 AI 写 approvals/）
  4. 累计 GPU 分钟不得超过 budget.max_gpu_hours
  每次运行追加一行到 logs/实验记录.jsonl（守卫 hook 禁止 AI 改）
"""
import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import shutil
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import 集群  # noqa: E402  madar / ku-h100 SLURM 接口


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

ROOT = Path.cwd()
RECORD = ROOT / "logs" / "实验记录.jsonl"


def fail(msg: str) -> None:
    print(f"[实验.py 拒绝] {msg}", file=sys.stderr)
    sys.exit(3)


def used_minutes() -> float:
    """累计已用 GPU 分钟：本机按运行时长 × GPU 数；集群作业已结束按实际用时，未结束按申请时长预占。"""
    tot = 0.0
    for j in 集群.read_jobs(ROOT):
        m = j["minutes"] if j.get("minutes") is not None else j["limit_min"]
        tot += m * j.get("gpus", 1)
    if not RECORD.exists():
        return tot
    for ln in RECORD.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
            tot += r.get("minutes", 0) * r.get("n_gpus", 1)
        except Exception:
            pass
    return tot


def check_protocol(h: str) -> Path:
    """检查预注册：文件夹存在、protocol.md 已提交且工作区无改动。"""
    dirs = [d for d in glob.glob(str(ROOT / "hypotheses" / f"{h}_*")) if os.path.isdir(d)]
    if len(dirs) != 1:
        fail(f"找不到唯一的假设文件夹 hypotheses/{h}_*（找到 {len(dirs)} 个）")
    proto = Path(dirs[0]) / "protocol.md"
    relp = str(proto.relative_to(ROOT))
    if not proto.exists():
        fail(f"{relp} 不存在，请先预注册")
    if subprocess.run(["git", "ls-files", "--error-unmatch", relp], cwd=ROOT,
                      capture_output=True).returncode != 0:
        fail(f"{relp} 未提交，请先 git commit 预注册再跑实验")
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", relp], cwd=ROOT).returncode != 0:
        fail(f"{relp} 提交后被修改过，判定标准不得改")
    return proto


def pick_gpu(allowed: str, wait_min: float) -> tuple:
    """在允许的卡里挑空闲的一张（利用率 <30%）；都忙就每分钟查一次，最多等 wait_min 分钟。
    返回 (卡号字符串, 是否争用)。11-22 巡检：卡被别的任务占着时，计时实验作废、AI 每轮空转。"""
    ids = [g.strip() for g in allowed.split(",") if g.strip()]
    if not ids:
        return allowed, False
    t_end = time.time() + wait_min * 60
    while True:
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=30).stdout
            stat = {ln.split(",")[0].strip(): (int(ln.split(",")[1]), int(ln.split(",")[2]))
                    for ln in out.strip().splitlines()}
        except Exception:
            return ids[0], False          # 查不到就按原样跑
        free = [i for i in ids if i in stat and stat[i][0] < 30]
        if free:
            return min(free, key=lambda i: stat[i]), False
        if time.time() >= t_end:
            best = min((i for i in ids if i in stat), key=lambda i: stat[i], default=ids[0])
            print(f"[实验.py] 允许的卡都被占用（等了 {wait_min:.0f} 分钟），在最空的卡 {best} 上运行，记录标记为争用；计时类结果不可信")
            return best, True
        print(f"[实验.py] 允许的卡 {ids} 都忙（利用率 ≥30%），1 分钟后再查", flush=True)
        time.sleep(60)


class GpuShareWatch:
    """运行期间每 5 秒查一次本卡上的计算进程；同时出现 ≥2 个进程 = 有别的任务（或 AI 自己并行启动的运行）在抢卡。
    11-22 长跑巡检：90 s 上限按墙钟算，同一配置卡忙时 70 s、卡闲时 34 s，作废与否取决于卡忙不忙。"""

    def __init__(self, gpu: str):
        import threading
        self.gpu, self.max_procs, self._stop = gpu.split(",")[0].strip(), 0, threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        try:
            uuid = {ln.split(",")[0].strip(): ln.split(",")[1].strip() for ln in subprocess.run(
                ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=30).stdout.strip().splitlines()}.get(self.gpu)
        except Exception:
            return
        while uuid and not self._stop.wait(5):
            try:
                out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,gpu_uuid", "--format=csv,noheader"],
                                     capture_output=True, text=True, timeout=30).stdout
                n = sum(1 for ln in out.strip().splitlines() if ln.split(",")[-1].strip() == uuid)
                self.max_procs = max(self.max_procs, n)
            except Exception:
                pass

    def __enter__(self):
        self._t.start(); return self

    def __exit__(self, *exc):
        self._stop.set()


# —— 自动选算力（用户 11-22：有 4090、H100、madar，按"哪里结束得快"自动选）——
# 相对本机 4090 的速度倍数（中小模型训练的粗估；goal.yaml compute.speed 可按实测覆盖）
SPEED = {"本机": 1.0, "ku-h100": 1.5, "madar": 1.8}
# 固定开销（分钟）：同步代码、作业启动、结果拉回
OVERHEAD = {"本机": 0.0, "ku-h100": 1.5, "madar": 3.0}


def _history_minutes(h: str, stage: str):
    """同一假设同一阶段以往本机运行的中位用时（分钟），作为预计用时；没有就返回 None。"""
    if not RECORD.exists():
        return None
    xs = sorted(r["minutes"] for r in map(json.loads, RECORD.read_text(encoding="utf-8").splitlines())
                if r.get("h") == h and r.get("stage") == stage and r.get("returncode") == 0
                and r.get("where", "本机") == "本机")
    return xs[len(xs) // 2] if xs else None


def _local_free(allowed: str) -> bool:
    ids = [g.strip() for g in allowed.split(",") if g.strip()]
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=30).stdout
        util = {ln.split(",")[0].strip(): int(ln.split(",")[1]) for ln in out.strip().splitlines()}
        return any(util.get(i, 100) < 30 for i in ids)
    except Exception:
        return True


def choose_where(a, budget: dict, goal: dict, est_min: float) -> tuple:
    """估计每处的完成时间 = 排队等待 + 固定开销 + 预计用时 / 速度倍数，返回 (最快的地方, 说明表)。"""
    comp = goal.get("compute") or {}
    allowed = comp.get("allowed", ["本机", "ku-h100", "madar"])
    speed = dict(SPEED, **(comp.get("speed") or {}))
    rows, why_local = [], ""
    # 只能本机的情况：纯 CPU 课题；goal 有单次时长上限（上限按本机 4090 定，换卡计时不可比）；用了本机数据目录又没配集群数据
    if str(budget.get("gpus", "0") or "").strip() == "":
        why_local = "纯 CPU 课题"
    elif budget.get("max_run_seconds") and a.stage != "full":
        why_local = "goal 有单次时长上限，按本机计时"
    elif (goal.get("env") or {}).get("extra_dirs") and not (goal.get("slurm") or {}).get("remote_data"):
        why_local = "数据只在本机"
    # goal 不允许本机时不强制本机（11-23 JEV 课题：设了 max_run_seconds、allowed 只有 ku-h100，却被送到本机找不到数据）
    if (why_local and "本机" in allowed) or allowed == ["本机"]:
        return "本机", f"只用本机（{why_local or 'goal 只允许本机'}）"
    for w in allowed:
        if w == "本机":
            wait = 0.0 if _local_free(str(budget.get("gpus", "0"))) else float(budget.get("gpu_wait_minutes", 30))
        else:
            st = 集群.probe(w)
            if st is None:
                rows.append((w, None, "连不上")); continue
            cap = 集群.CLUSTERS[w].get("max_my_jobs", 99)
            if st["mine"] >= cap:
                wait = 60.0           # 自己的作业数到上限，要等前面的跑完
            elif st["free"] > 0:
                wait = 2.0 if st["pending"] <= st["free"] else 10.0
            else:
                wait = 60.0
        total = wait + OVERHEAD.get(w, 3.0) + est_min / speed.get(w, 1.0)
        rows.append((w, total, f"等 {wait:.0f} + 开销 {OVERHEAD.get(w, 3.0):.1f} + 运行 {est_min / speed.get(w, 1.0):.1f}"))
    ok = [r for r in rows if r[1] is not None]
    best = min(ok, key=lambda r: r[1])[0] if ok else "本机"
    table = "；".join(f"{w} {'—' if t is None else f'{t:.1f} 分钟'}（{d}）" for w, t, d in rows)
    return best, f"预计 {est_min:.1f} 分钟（本机口径）→ {table} → 选 {best}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["toy", "pilot", "full"])
    ap.add_argument("--h", help="假设编号，如 H03")
    ap.add_argument("--where", choices=["自动", "本机", "madar", "ku-h100"], default="自动",
                    help="在哪里跑；默认自动：比较本机 4090、ku-h100、madar 谁先跑完")
    ap.add_argument("--预计分钟", dest="est", type=float, default=None,
                    help="本机 4090 上的预计用时；不给就用同假设同阶段的历史中位数，再没有就按阶段上限的一半")
    ap.add_argument("--gpus", type=int, default=1, help="集群作业 GPU 数")
    ap.add_argument("--用量", dest="usage", action="store_true", help="只显示已用算力")
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="-- 之后是实际命令")
    a = ap.parse_args()

    budget = (yaml.safe_load(open(ROOT / "goal.yaml", encoding="utf-8")) or {}).get("budget", {})
    max_min = float(budget.get("max_gpu_hours", 24)) * 60
    if a.usage:
        print(f"已用 {used_minutes():.1f} / {max_min:.0f} GPU 分钟")
        return

    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not (a.stage and a.h and cmd):
        fail("用法：python 实验.py --stage toy --h H03 -- <命令>")

    hdir = check_protocol(a.h).parent  # 该假设的文件夹

    # 自动选算力：估计三处完成时间，选最快的（用户 11-22）
    if a.where == "自动":
        goal = yaml.safe_load(open(ROOT / "goal.yaml", encoding="utf-8")) or {}
        default = {"toy": budget.get("toy_minutes", 5), "pilot": budget.get("pilot_minutes", 60),
                   "full": float(budget.get("full_hours", 24)) * 60}[a.stage] / 2
        est = a.est or _history_minutes(a.h, a.stage) or default
        a.where, note = choose_where(a, budget, goal, float(est))
        print(f"[实验.py] 算力选择：{note}", flush=True)
        # 选了本机但卡都忙：边等边看集群，每 3 分钟重估一次，集群更快就改交集群
        # （用户 11-22："排队的时候不断看 madar 和 h100 那边是否能排上队"）
        local_gpus = str(budget.get("gpus", "0") or "").strip()
        if a.where == "本机" and local_gpus and "只用本机" not in note:
            t_end = time.time() + float(budget.get("gpu_wait_minutes", 30)) * 60
            while not _local_free(local_gpus) and time.time() < t_end:
                print("[实验.py] 本机卡都忙，3 分钟后重新比较本机与集群", flush=True)
                time.sleep(180)
                if _local_free(local_gpus):
                    break
                w, note = choose_where(a, budget, goal, float(est))
                if w != "本机":
                    print(f"[实验.py] 等卡期间重估：{note}", flush=True)
                    a.where = w
                    break

    # 分级限时与 Full 门控
    limits = {"toy": budget.get("toy_minutes", 5), "pilot": budget.get("pilot_minutes", 60)}
    if a.stage == "full":
        if not (ROOT / "approvals" / f"{a.h}.ok").exists():
            fail(f"Full 实验需要人工批准：把请求写进 research-state.yaml 的 pending_approvals（理由与证据），"
                 f"然后继续做别的假设，不要停机；用户批准后会创建 approvals/{a.h}.ok")
        limit_min = float(budget.get("full_hours", 24)) * 60 if a.where != "本机" else None
    else:
        limit_min = float(limits[a.stage])

    gpus = str(budget.get("gpus", "0") or "").strip()
    # 本机实验按 1 张卡记账；gpus 为空 = 纯 CPU 课题，本机运行不记 GPU 账、不受 GPU 预算限制（11-22 巡检发现：原先 CPU 课题被一律拒绝）
    cpu_only = (gpus == "") and a.where == "本机"
    n_gpus = 0 if cpu_only else 1
    remain = float("inf") if cpu_only else max_min - used_minutes()
    if remain <= 0:
        fail(f"算力预算已用完（{max_min:.0f} GPU 分钟）：这是停机事项，把 status 改为 needs_approval 并写清申请追加的理由")

    # —— 集群：提交后立即返回，不占用本机与额度 ——
    if a.where != "本机":
        minutes = int(min(limit_min, remain / a.gpus))
        # 给了预计用时就按 max(15, 2.5 × 预计) 申请，不按阶段上限：集群作业未结束时按申请时长预扣预算，
        # 一批短作业都按 60 分钟预扣会把预算“占满”（11-23 H22：17 个约 7 分钟的作业预扣 1020 分钟）
        if a.est:
            minutes = int(min(minutes, max(15, 2.5 * a.est)))
        if a.where == "ku-h100":
            minutes = min(minutes, 360)       # gpu-student 每作业 ≤6 小时、≤2 卡
            a.gpus = min(a.gpus, 2)
        if minutes < 1:
            fail("剩余算力预算不足 1 分钟")
        jid = 集群.submit(ROOT, a.h, a.stage, cmd, minutes, a.gpus, cluster=a.where)
        acct = "账号 <slurm-account>，" if a.where == "madar" else ""
        print(f"[实验.py] 已提交 {a.where} 作业 {jid}（{acct}限时 {minutes} 分钟）。"
              f"结果会自动同步回 hypotheses/。若本轮没有别的事可做，"
              f"把 research-state.yaml 的 waiting_for_jobs 设为 true 后结束本轮。")
        return

    # 运行：限时 = min(分级上限, 剩余预算)
    timeout_s = None
    if limit_min is not None:
        timeout_s = min(limit_min, remain / max(n_gpus, 1)) * 60
    contended = False
    if not cpu_only:      # 本机 GPU 运行：在允许的卡里挑空闲的一张，都忙就等
        gpus, contended = pick_gpu(gpus, float(budget.get("gpu_wait_minutes", 30)))
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=gpus)
    t0 = time.time()
    timed_out = False
    watch = GpuShareWatch(gpus) if not cpu_only else None
    try:
        if watch:
            watch.__enter__()
        r = run_child(cmd, cwd=ROOT, env=env, timeout=timeout_s, stderr=subprocess.PIPE, text=True)
        rc, err_text = r.returncode, (r.stderr or "")
        sys.stderr.write(err_text)          # 错误输出照常显示给调用者
    except subprocess.TimeoutExpired:
        rc, timed_out, err_text = -9, True, ""
    finally:
        if watch:
            watch.__exit__()
    minutes = (time.time() - t0) / 60
    shared = bool(watch) and watch.max_procs >= 2   # 运行中有别的进程同卡：计时不可信
    contended = contended or shared
    # goal 规定的单次运行硬上限（如"每次训练 ≤ 2 分钟"）：超了就标记作废
    max_s = budget.get("max_run_seconds")
    over = bool(max_s) and a.stage != "full" and minutes * 60 > float(max_s)
    # 同卡争用时超限不算配置本身的问题：标 rerun_needed，按 protocol 在空闲时重跑，而不是直接作废
    rerun_needed = over and contended
    if rerun_needed:
        over = False

    # 追加实验记录（AI 不可改）
    RECORD.parent.mkdir(exist_ok=True)
    with open(RECORD, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "time": dt.datetime.now().isoformat(timespec="seconds"),
            "h": a.h, "stage": a.stage, "cmd": " ".join(cmd), "where": "本机",
            "minutes": round(minutes, 2), "n_gpus": n_gpus,
            "returncode": rc, "timed_out": timed_out, "over_limit": over,
            "gpu": gpus, "contended": contended, "rerun_needed": rerun_needed,
            "gpu_procs_max": (watch.max_procs if watch else 0),   # 实际用的卡；contended=允许的卡都被别的任务占着，计时类结果不可信
            # 失败时留下错误输出末尾，重跑成功后也能查到第一次为什么失败（11-22 巡检）
            **({"stderr_tail": err_text[-600:]} if rc not in (0, -9) and err_text else {}),
        }, ensure_ascii=False) + "\n")
    if rerun_needed:
        print(f"[实验.py] 本次 {minutes*60:.0f} s 超过上限 {max_s} s，但运行中同卡最多有 {watch.max_procs} 个进程（争用）："
              f"不算作废也不算有效，记为 rerun_needed，卡空闲时重跑同一配置；不要并行启动多个计时实验", file=sys.stderr)
    if over:
        print(f"[实验.py] 本次运行 {minutes*60:.0f} s，超过 goal 的单次上限 {max_s} s：按 goal 结果作废，"
              f"不得作为结论或写进论文/PPT；要放宽上限须写进 pending_approvals", file=sys.stderr)

    # 自动画样本拼图：本次运行新产生的 预测_验证_*.npz 各画一张（失败不影响实验结果）
    if rc == 0 and not timed_out:
        # rglob：预测文件常放在 结果/ 子目录（11-22 巡检：原先只查假设目录本层，拼图一直没生成）
        for f in sorted(hdir.rglob("预测_验证_*.npz")):
            if f.stat().st_mtime >= t0 - 1:
                # 用课题环境的 python（PATH 里第一个，harness 已把 env.python_bin 放到最前）：评估.py 依赖课题环境的包，如 torchvision
                py = shutil.which("python") or sys.executable
                r = subprocess.run([py, str(Path(__file__).resolve().parent / "拼图.py"), str(ROOT), str(f)],
                                   cwd=ROOT, capture_output=True, text=True, timeout=300)
                print(f"[实验.py] 样本拼图：{(r.stdout or r.stderr).strip()[-200:]}")

    if timed_out:
        print(f"[实验.py] 超过 {a.stage} 限时，已终止（{minutes:.1f} 分钟）", file=sys.stderr)
    sys.exit(rc if rc >= 0 else 124)


if __name__ == "__main__":
    main()
