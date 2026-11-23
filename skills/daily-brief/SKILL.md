---
name: daily-brief
description: 生成 5 分钟能读完的每日科研简报，写到 logs/简报/<日期>.md。harness 每天自动调用一次，harness 停下时也会调用；也可手动 /daily-brief。
---

# 每日简报

目标：Research Director 早上 5 分钟看完，就知道昨天做了什么、方向对不对、有没有要他决定的事。

## 取材（只用文件，不凭记忆）
1. `git log --since="上次简报时间"`（上次简报 = `logs/简报/` 里最新一份的日期；没有则取全部）
2. 这段时间新增或修改的 `hypotheses/H*/result.md` 和结果 JSON
3. `findings.md` 新增行，`research-state.yaml`
4. `logs/实验记录.jsonl`、`logs/作业.jsonl`（算力用量、失败或超时的实验）
5. `logs/harness.log` 里的出错、被拒、额度暂停

## 格式（严格控制篇幅：正文 ≤ 40 行）
```markdown
# 简报 YYYY-MM-DD

**一句话**：（今天最重要的一件事，带数字）

## 需要你决定（没有就写"无"）
- （写明 logs/最新审查.md 覆盖到哪些假设、哪个提交；之后新判定的假设标"未审查"，不能写成"当前没有高中问题"；logs/最新审查.md 中尚未处理的 [高] 问题必须列出，不能写"无"；status=needs_approval 的停机请求放最前；再逐条列 research-state.yaml 的 pending_approvals：事项、理由、证据路径；最后是需要你知道的风险）

## 做了什么
| 假设 | 类别 | 阶段 | 结论 | 关键数字（均值±标准差，种子数） | 证据 |
|---|---|---|---|---|---|

## 失败与异常
- 否定的方向、出错/超时的实验、被守卫拦截的操作、额度暂停

## 当前最好 vs 基线
- 一行，带证据路径

## 下一步
- 1–3 条，来自 research-state.yaml 的 next_action

## 算力
- 本机 / madar 已用 GPU 分钟，占预算比例
```

## 规则
- 核对数字用 Read 工具直接读结果 JSON 和 result.md，不依赖 shell；不得以"shell 被拒"为由跳过核对。
- 开头注明"截至第 X 轮、提交号 Y"，读者能判断是否过时。
- 每个数字都从结果文件里读，标注证据路径；读不到就写"未找到证据"，不要补。
- 结论原样照抄 result.md 的判定，不在简报里升级（"存疑"不能写成"有效"）。
- 没有新进展也要写，并说明原因（如：等集群作业、额度暂停、连续出错）。
- 写完 `git add logs/简报/ && git commit -m "简报 YYYY-MM-DD"`。
