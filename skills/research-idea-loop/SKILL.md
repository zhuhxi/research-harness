---
name: research-idea-loop
description: Run the four-step research idea loop for a research direction — literature gap table, cheap probe experiments, six follow-up questions on surprises, reviewer red-team, 5-line topic table and a pre-registered one-day go/kill experiment. Use when the user wants to find a top-venue paper idea in a direction, says "按那套选题方法/四步循环来", or invokes /research-idea-loop.
---

# Research idea loop

The full playbook, in Chinese with the user's original wording, is at
`<你的笔记目录>/科研方法/选题循环_工作手册.md`. Read it first. It holds the 6 questions, the 5-line topic table, the prompts, and the lessons from the 2026-10-15 run. A worked example is in `<你的笔记目录>/反问题顶会调研/扩散流反问题/` (files 00–05).

The argument is the research direction (for example "扩散模型解反问题，EIT 作为例子"). If it is missing, ask for it.

## Principles

- AI ideas look better than they are. Ideas come from surprises in our own probes and from untested premises in other papers.
- Overlap with existing work is fine; do not avoid others. When a part is taken, change the selling point and keep the untested conditions.
- Report to the user in plain Chinese (说人话): verdict first (good or bad news), then a table, then the recommended next step. Give a recommendation, not a survey.
- Write every step's output into numbered files in a project folder: `00_…差距汇总`, `01_…探针结果`, `02_…红队`, `03_…生死实验计划`, and so on.

## Steps

1. **Literature → gap table.** Spawn parallel research subagents, one per subtopic, usually 3–4: methods, applications, evaluation/theory, latest submissions such as current-cycle OpenReview. Each paper row: conclusion, evidence, weakest point, untested conditions. Then consolidate the "untested conditions" column into a ranked list and design 3–5 cheap probes from it. Spot-check at least 3 key papers yourself; subagents can fabricate.
2. **Probes (see phenomena, don't test hypotheses).**
   - Use real compute in parallel: local GPUs, madar H200 via sbatch arrays (see the madar-hpc memory), and ku-h100 if its Slurm works (see the ku-h100-teaching memory; never run training on its login node).
   - Push variables to extremes (at least 64× the default), swap models and benchmarks, remove contamination.
   - Calibrate against published numbers first, so you know the framework is trustworthy.
   - Look at actual samples, not only scores; this catches silent failures.
3. **The 6 questions for each surprise.** Do the cheapest discriminating experiments first. Compare partial results only on matched sample subsets. Prefer exact, assumption-free statistics, and know the ideal value of every metric before interpreting it. Break every aggregate metric into subgroups.
4. **Filter.**
   - Red-team as a reviewer with a collision-check subagent. For each finding: overlap none / partial / full, and what remains novel.
   - Fill in the 5-line topic table.
   - Look for a theory piece: a toy model with a closed form that makes a falsifiable prediction, then test that prediction on data you already have.
   - Write the go/kill plan with pass / kill / middle criteria into a file BEFORE seeing results. Run it within a day and judge strictly by the written criteria.

Run independent tracks in the background in parallel: red-team, theory and experiments. Save a project memory with the folder, key numbers, collisions, verdict and venue plan.
