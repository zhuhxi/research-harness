#!/usr/bin/env python3
"""
SLURM 集群接口：madar（H200）与 ku-h100（4×H100 NVL）（由 实验.py 和 harness.py 调用，不直接给 AI 用）。

  提交：同步课题代码到 madar → 生成 sbatch（账号强制 <slurm-account>）→ 提交 → 记录作业号后立即返回
  刷新：查询本课题所有未结束作业；结束的作业把结果同步回本机，并按实际用时记账

约定：
  - 账号只允许 <slurm-account>（ALLOWED_ACCOUNTS 写死，goal.yaml 也只读）
  - 远端目录：<slurm.remote_dir>/<课题名>/，放在 /dpc/<slurm-account>（主目录配额小）
  - 作业记录：logs/作业.jsonl（守卫 hook 禁止 AI 改）
"""
import datetime as dt
import json
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

# 账号白名单：用户规定所有新作业一律用 <slurm-account>
ALLOWED_ACCOUNTS = {"<slurm-account>"}

# 默认集群配置；goal.yaml 的 slurm 段可覆盖（account 除外，必须在白名单内）
DEFAULTS = {
    "host": "madar",
    "account": "<slurm-account>",
    "partition": "gpu",
    "gres": "gpu:nvidia_h200:1",
    "cpus": 8,
    "mem": "64G",
    "modules": ["intel_h200_gpu"],
    "python": "~/.conda/envs/SimVQ/bin/python",
    "remote_dir": "/dpc/<slurm-account>/harness",
    "exclude": [".git", "logs", "__pycache__", ".claude", "approvals"],
}

# ku-h100：单节点 4×H100 NVL，登录节点即计算节点；没有卡隔离（作业里能看到全部卡），
# 每人 gpu-student 同时最多 2 个作业；家目录满，一律放 /data/student_storage（用户 11-22：三处算力自动选）
KU_H100 = {
    "host": "ku-h100",
    "account": "",                 # 该集群不用 --account
    "partition": "gpu-student",
    "gres": "gpu:1",
    "cpus": 8,
    "mem": "64G",
    "modules": [],
    "python": "<集群存储目录>/q2_ttrl/venv/vl/bin/python",
    "remote_dir": "<集群存储目录>/harness",
    "exclude": DEFAULTS["exclude"],
    "pick_free_gpu": True,         # 无卡隔离：作业开始时自己挑显存占用最少的卡
    "max_my_jobs": 2,
}
CLUSTERS = {"madar": DEFAULTS, "ku-h100": KU_H100}

JOBS = Path("logs") / "作业.jsonl"


def cfg(root: Path, name: str = "madar") -> dict:
    """读取并校验集群配置。goal.yaml 里 madar 用 slurm 段覆盖，ku-h100 用 ku_h100 段覆盖。"""
    goal = yaml.safe_load(open(root / "goal.yaml", encoding="utf-8")) or {}
    over = goal.get("slurm") if name == "madar" else goal.get("ku_h100")
    c = dict(CLUSTERS[name], **(over or {}))
    c["name"] = name
    if name == "madar" and c["account"] not in ALLOWED_ACCOUNTS:
        sys.exit(f"[集群] 拒绝：账号 {c['account']} 不在允许列表 {ALLOWED_ACCOUNTS}")
    c["remote"] = f"{c['remote_dir']}/{root.name}"
    return c


def probe(name: str, timeout=25):
    """查一个集群现在能不能马上开跑。返回 {"free": 空闲卡数, "mine": 我在跑/排队的作业数, "pending": 分区排队数}；
    ssh 不通返回 None（自动选择时跳过该集群）。"""
    c = CLUSTERS[name]
    if name == "ku-h100":
        # 无卡隔离、Slurm 不记 GPU 占用：直接看 nvidia-smi（显存 <2 GB 且利用率 <30% 视为空闲）
        cmd = ("nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits; echo ==; "
               f"squeue -u $USER -h -p {c['partition']} | wc -l")
    else:
        cmd = (f"scontrol show node -o | grep nvidia_h200 | grep -v DRAIN | grep -o 'AllocTRES=[^ ]*' | grep -o 'gres/gpu=[0-9]*' ; echo ==; "
               f"scontrol show node -o | grep nvidia_h200 | grep -vc DRAIN; echo ==; "
               f"squeue -u $USER -h -p {c['partition']} | wc -l; echo ==; squeue -p {c['partition']} -h -t PD | wc -l")
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", c["host"], cmd],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            return None
        parts = [x.strip() for x in r.stdout.split("==")]
        if name == "ku-h100":
            gpus = [ln.split(",") for ln in parts[0].splitlines() if "," in ln]
            free = sum(1 for u, m in gpus if int(u) < 30 and int(m) < 2000)
            return {"free": free, "mine": int(parts[1] or 0), "pending": 0}
        alloc = sum(int(x.split("=")[1]) for x in parts[0].split())
        n_nodes = int(parts[1] or 0)
        return {"free": max(0, 8 * n_nodes - alloc), "mine": int(parts[2] or 0), "pending": int(parts[3] or 0)}
    except Exception:
        return None


def ssh(c: dict, cmd: str, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", c["host"], cmd],
                          capture_output=True, text=True, timeout=timeout)


def push_code(root: Path, c: dict) -> None:
    """本机 → 远端：同步课题代码（排除 git、日志等）。"""
    ssh(c, f"mkdir -p {shlex.quote(c['remote'])}")
    ex = sum([["--exclude", e] for e in c["exclude"]], [])
    r = subprocess.run(["rsync", "-a", *ex, f"{root}/", f"{c['host']}:{c['remote']}/"],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode:
        sys.exit(f"[集群] 同步代码失败：{r.stderr[-300:]}")


def pull_results(root: Path, c: dict) -> None:
    """远端 → 本机：只拉 hypotheses/ 下更新的文件与 slurm 日志；预注册文件不回传。"""
    subprocess.run(["rsync", "-a", "--update", "--exclude", "protocol.md",
                    f"{c['host']}:{c['remote']}/hypotheses/", f"{root}/hypotheses/"],
                   capture_output=True, timeout=1800)
    (root / "logs" / "slurm").mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", f"{c['host']}:{c['remote']}/slurm_logs/", f"{root}/logs/slurm/"],
                   capture_output=True, timeout=600)


def read_jobs(root: Path) -> list:
    p = root / JOBS
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def write_jobs(root: Path, jobs: list) -> None:
    p = root / JOBS
    p.parent.mkdir(exist_ok=True)
    p.write_text("".join(json.dumps(j, ensure_ascii=False) + "\n" for j in jobs), encoding="utf-8")


def submit(root: Path, h: str, stage: str, cmd: list, minutes: int, gpus: int = 1, cluster: str = "madar") -> str:
    """提交作业，返回作业号。命令在远端课题目录下执行。"""
    c = cfg(root, cluster)
    # 课题外的数据目录不会同步到集群（11-22 巡检）：声明了 extra_dirs 却没配 slurm.remote_data 就拒绝提交
    goal = yaml.safe_load(open(root / "goal.yaml", encoding="utf-8")) or {}
    extra = (goal.get("env") or {}).get("extra_dirs") or []
    if extra and not (goal.get("slurm") or {}).get("remote_data"):
        sys.exit(f"[集群] 拒绝：课题用到本机目录 {extra}，集群上没有。这是停机事项：请研究主管在 goal.yaml 的 slurm 下"
                 f"填 remote_data（集群上的数据路径），代码通过环境变量 HARNESS_DATA_DIR 读取数据目录")
    push_code(root, c)
    name = f"{root.name[:12]}_{h}_{stage}"
    # gres 为空表示纯 CPU 作业（如 prod 分区）
    gres_line = f"#SBATCH --gres={c['gres'].rsplit(':', 1)[0]}:{gpus}" if c["gres"] else ""
    hh, mm = divmod(int(minutes), 60)
    script = "\n".join([
        "#!/bin/bash",
        f"#SBATCH --account={c['account']}" if c["account"] else "",       # madar 强制 <slurm-account>；ku-h100 不用账号
        f"#SBATCH --partition={c['partition']}",
        gres_line,
        f"#SBATCH --cpus-per-task={c['cpus']}",
        f"#SBATCH --mem={c['mem']}",
        f"#SBATCH --time={hh:02d}:{mm:02d}:00",
        f"#SBATCH --job-name={name}",
        f"#SBATCH --output={c['remote']}/slurm_logs/%j.out",
        "module purge" if c["modules"] else "",      # ku-h100 没有 module 命令
        *[f"module load {m}" for m in c["modules"]],
        f"cd {shlex.quote(c['remote'])}",
        # ku-h100 没有卡隔离：开跑时挑显存占用最少的卡
        # 同时开跑的作业会挑到同一张卡（11-23 JEV 课题：两个 54 GB 作业都挑了 0 号卡，一个 OOM）：
        # 挑卡前加锁，按空闲显存最多挑；锁保持 45 秒（够模型把显存占上），下一个作业再挑时就能看到
        ("exec 9>/tmp/harness_gpu_pick.lock; flock 9; "
         "export CUDA_VISIBLE_DEVICES=$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits"
         " | sort -t, -k2 -nr | head -1 | cut -d, -f1 | tr -d ' '); "
         "( sleep 45; flock -u 9 ) &") if c.get("pick_free_gpu") else "",
        # 集群上的数据目录（goal.yaml slurm.remote_data），代码用 HARNESS_DATA_DIR 读取
        f"export HARNESS_DATA_DIR={shlex.quote(str((goal.get('slurm') or {}).get('remote_data', '')))}",
        # 命令里的 python 替换为集群上的 python 解释器
        " ".join(c["python"] if (i == 0 and a in ("python", "python3")) else shlex.quote(a)
                 for i, a in enumerate(cmd)),
    ]) + "\n"
    rs = f"{c['remote']}/slurm_logs/{name}.sbatch"
    ssh(c, f"mkdir -p {c['remote']}/slurm_logs")
    # 通过 stdin 写入 sbatch 脚本再提交
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", c["host"],
                        f"cat > {shlex.quote(rs)} && sbatch --parsable {shlex.quote(rs)}"],
                       input=script, capture_output=True, text=True, timeout=120)
    if r.returncode:
        sys.exit(f"[集群] 提交失败：{r.stderr[-300:]}")
    jid = r.stdout.strip().split(";")[0]
    jobs = read_jobs(root)
    jobs.append({"job": jid, "cluster": cluster, "h": h, "stage": stage, "cmd": " ".join(cmd), "gpus": gpus,
                 "limit_min": minutes, "submitted": dt.datetime.now().isoformat(timespec="seconds"),
                 "state": "PENDING", "minutes": None})
    write_jobs(root, jobs)
    return jid


def refresh(root: Path) -> int:
    """刷新未结束作业的状态；结束的拉回结果并记账。返回仍在排队/运行的作业数。"""
    jobs = read_jobs(root)
    open_jobs = [j for j in jobs if j["state"] in ("PENDING", "RUNNING", "CONFIGURING")]
    if not open_jobs:
        return 0
    # 按集群分组查询（旧记录没有 cluster 字段 = madar）
    for name in sorted({j.get("cluster", "madar") for j in open_jobs}):
        group = [j for j in open_jobs if j.get("cluster", "madar") == name]
        c = cfg(root, name)
        ids = ",".join(j["job"] for j in group)
        try:
            r = ssh(c, f"sacct -nPX -j {ids} --format=JobID,State,ElapsedRaw")
        except subprocess.TimeoutExpired:
            continue
        if r.returncode:
            continue  # 查询失败：保守视为仍在运行
        info = {}
        for ln in r.stdout.split():
            parts = ln.split("|")
            if len(parts) == 3:
                info[parts[0]] = (parts[1].split()[0], int(parts[2] or 0))
        finished = False
        for j in group:
            if j["job"] in info:
                st, el = info[j["job"]]
                j["state"] = st
                if st not in ("PENDING", "RUNNING", "CONFIGURING"):
                    j["minutes"] = round(el / 60, 2)
                    finished = True
        if finished:
            pull_results(root, c)
    write_jobs(root, jobs)
    return sum(j["state"] in ("PENDING", "RUNNING", "CONFIGURING") for j in jobs)


def cancel(root: Path, jid: str) -> bool:
    """取消本课题记录里一个仍在排队的作业（用于改投更快的地方，11-23：用户要求排队时不断看另两处能否更快）。
    只取消 PENDING 状态的作业；取消后状态记为 CANCELLED、用时 0，不计预算。"""
    jobs = read_jobs(root)
    for j in jobs:
        if j["job"] == str(jid):
            if j["state"] != "PENDING":
                print(f"[集群] 作业 {jid} 状态 {j['state']}，只取消排队中的作业")
                return False
            c = cfg(root, j.get("cluster", "madar"))
            r = ssh(c, f"scancel {shlex.quote(str(jid))}")
            if r.returncode:
                print(f"[集群] 取消失败：{r.stderr[-200:]}")
                return False
            j["state"], j["minutes"] = "CANCELLED", 0.0
            write_jobs(root, jobs)
            return True
    print(f"[集群] 本课题记录里没有作业 {jid}")
    return False


if __name__ == "__main__":
    # 手动用法：python 集群.py 刷新 | python 集群.py 取消 <作业号>   （在课题根目录）
    root = Path.cwd()
    if sys.argv[1:] == ["刷新"]:
        n = refresh(root)
        print(f"仍在排队/运行：{n} 个作业")
        for j in read_jobs(root)[-10:]:
            print(j["job"], j["h"], j["stage"], j["state"], j["minutes"])
    elif len(sys.argv) == 3 and sys.argv[1] == "取消":
        print("已取消" if cancel(root, sys.argv[2]) else "未取消")
    else:
        print(__doc__)
