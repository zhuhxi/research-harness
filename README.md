# 个人 Research Harness

以 Claude Code 为核心的自主科研系统。你定目标与边界（`goal.yaml`），AI 在授权范围内自主循环，harness 负责多轮运行、额度断档恢复和停止条件。

## 目录
| 路径 | 作用 |
|---|---|
| `templates/项目/` | 新课题模板：CLAUDE.md、goal.yaml、research-state.yaml、findings.md、预注册模板、权限白名单 |
| `skills/autoresearch-loop/` | 单次自主迭代的方法（双层循环、Toy→Pilot→Full、外层决策、每日简报） |
| `harness/harness.py` | 主循环：每轮新会话调用无头 Claude；额度用完自动暂停重试；needs_approval/done/STOP 时停 |
| `hooks/守卫.py` | PreToolUse hook：锁 goal.yaml/eval/approvals/基线/实验记录、已提交的预注册；拦 rm -rf、push、改写历史；实验必须走 实验.py |
| `tools/实验.py` | 实验唯一入口：检查预注册已提交、toy/pilot 限时、Full 需 approvals/H编号.ok、累计 GPU 小时预算、写实验记录 |
| `tools/实验.py`（续） | 默认 `--where 自动`：估"排队 + 同步开销 + 预计用时 / 速度"，在本机 GPU、两个 SLURM 集群里选最先跑完的；运行中监测同卡争用 |
| `tools/集群.py` | SLURM 集群（madar H200、ku-h100 H100）：同步代码 → 提交 → 结束后拉回结果并按实际用时记账；`probe` 查空闲卡 |
| `tools/体检.py` | 体检报告：每轮耗时花费、守卫拦截、预注册顺序、实验超时/超上限/争用、测试集使用 |
| `tools/拼图.py` | 自动样本拼图：验证集上错得最自信 + 随机样本 |
| `tools/汇报PPT.py` | 固定版式汇报 PPT（JSON → pptx）：引导问题、术语旁注、代码页；`--严格` 报警、`--预览` 出总览图 |
| `harness/夜间巡检.py` | 巡检：研究 → 体检 → 样稿（论文段落 + PPT）→ 独立审查，循环到截止时刻，问题写进问题日志 |
| `skills/my-research-style/` | 写作/绘图/表格/PPT 标准：分派现有 skill、覆盖冲突规则；`references/偏好检查表.md` 为定稿偏好 |
| `skills/glance-check/` | 图表 PPT 一眼看懂检查（自造缩写、内部叫法、标题不是结论） |
| `skills/sentence-mimic/` | 逐句学一位作者的写法，抽句型内核，仿写到自己的段落 |
| `skills/research-idea-loop/` | 选题四步循环：文献空位表 → 小试探 → 追问 → 红队 → 预注册一天定生死 |
| `skills/top-venue-pipeline/` | 从方向到可投顶会稿件的全流程 |
| `skills/vibe-video/` | 1 分钟知识短片制作流程 |
| `skills/daily-brief/`、`skills/research-handoff/` | 每日简报、研究交接 |
| `scripts/新建课题.sh` | 由模板生成课题目录（skill、hook、实验.py 以软链接接入） |

安装 skill：把 `skills/<名字>` 软链接到 `~/.claude/skills/<名字>`。公开版里的本机路径、集群账号已换成占位符（如 `<slurm-account>`），用前改成自己的。汇报PPT.py 用的学校 PPT 模板（`templates/PPT/KU_template.pptx`）不随公开版提供，换成自己的 16:9 模板即可。

## 用法
```bash
bash scripts/新建课题.sh ~/research_projects/<课题名>
# 填 goal.yaml；填 eval/评估.py 的 _load_raw()（默认测试集封存：AI 拿不到测试标签，测试指标只能经 score() 获取并记录）
python harness/harness.py <课题目录> --iters 1     # 先试一轮
nohup python harness/harness.py <课题目录> &        # 过夜运行
touch <课题目录>/STOP                               # 随时停止
python harness/harness.py <课题目录> --交接         # 汇报前生成研究交接（--简报 同理）
```
需要你批准时 harness 会停下，原因写在 `research-state.yaml` 的 `approval_request`。批准 Full：`touch approvals/H编号.ok`，把 status 改回 `running` 再启动。

## 搭建进度
- [x] 第 1 周：模板、自主循环 skill、harness（额度暂停/恢复）、玩具课题试跑
- [x] 第 2 周：守卫 hook、实验入口（Full 门控、限时、算力预算）、madar SLURM（<slurm-account>，等作业不耗额度）
- [x] 第 3 周：每日简报（跨天/停下时自动）、每周研究交接（--交接 或完成时自动，含自测题）、额度暂停恢复已用假 claude 验证
- [x] 第 4 周：my-research-style（分派到 4 个现有 skill + 偏好覆盖 + 表格/PPT 规则 + 新偏好记录），已链接到 ~/.claude/skills 全局可用
