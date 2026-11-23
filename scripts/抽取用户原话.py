#!/usr/bin/env python3
"""
从 ~/.claude/projects/*/*.jsonl 抽取"用户亲手输入"的消息，按关键词筛出写作 / 绘图 / PPT 相关要求。

只保留：type=user、非子代理（isSidechain=False）、origin.kind=human 的文本；
去掉 <system-reminder>、<pasted_content> 等系统/粘贴内容；按文本去重（分叉对话会复制历史）。

输出（目录 偏好整理/，不入 git）：
  第1步_用户原话_全部.jsonl   所有用户原话
  第1步_筛选结果.jsonl        命中关键词的原话（含分类、日期、来源对话）
  第1步_统计.txt              数量统计
用法：python scripts/抽取用户原话.py
"""
import glob
import sys
import json
import os
import re
from collections import Counter
from pathlib import Path

SRC = os.path.expanduser("~/.claude/projects")
OUT = Path(__file__).resolve().parent.parent / "偏好整理"
TOPIC = sys.argv[1] if len(sys.argv) > 1 else "写作"   # 写作（默认）或 实验

# 分类关键词（命中任一即归入该类；一条可属多类）
CATS = {
    "论文写作": r"论文|稿|摘要|引言|introduction|abstract|related work|相关工作|段落|措辞|表达|句子|句式|写法|写作|"
                r"AI ?味|套话|夸张|口吻|语气|英文|翻译|润色|术语|contribution|贡献|审稿|reviewer|latex|overleaf|"
                r"标题|caption|图注|表注|结论|motivation|动机",
    "科研绘图": r"图|figure|fig\b|plot|画|配色|颜色|字体|字号|坐标|legend|图例|线宽|子图|matplotlib|"
                r"示意图|流程图|框图|drawio|tikz|svg|pdf|dpi|风格|素",
    "PPT汇报": r"ppt|幻灯|slide|汇报|组会|导师|老师|讲|页|beamer|演示|一眼",
    "表格": r"表格|table|表\d|列|行|加粗|粗体",
}
# 实验主题的分类关键词（python 抽取用户原话.py 实验）
CATS_EXP = {
    "实验设计": r"实验|试探|探针|试点|pilot|toy|消融|ablation|对照|基线|baseline|判据|预注册|假设|决定性",
    "统计与可靠性": r"种子|seed|方差|显著|置信|配对|bootstrap|噪声|泄露|过拟合|验证集|测试集|早停|检查点|复现|口径|分母|作弊|挑",
    "效率与资源": r"分钟|min\b|小时|超时|timeout|gpu|显存|hpc|madar|slurm|sbatch|集群|并行|token|额度|后台|等待",
    "训练与模型": r"训练|学习率|epoch|loss|损失|网络|u-?net|模型|参数量|权重|正则|数据集|仿真|实测|kit4",
}
# 表达"要求/偏好/否定"的信号词：筛选时要求同时命中，减少闲聊
SIGNAL = r"不要|别|不能|不该|应该|要|必须|改|换|删|太|过于|难看|丑|乱|简洁|清楚|统一|一律|以后|每次|总是|偏好|喜欢|"\
         r"风格|规范|原则|标准|看不懂|看懂|AI|模板|照|参考|范文|为什么"

# 去掉的系统/粘贴片段
STRIP = re.compile(r"<(system-reminder|pasted_content|command-[a-z]+|local-command-[a-z]+)[^>]*>.*?</\1[^>]*>", re.S)


def text_of(content) -> str:
    """把消息内容统一成纯文本（只取 text 块，忽略图片与工具结果）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def main():
    OUT.mkdir(exist_ok=True)
    seen, rows = set(), []
    n_files = 0
    for f in sorted(glob.glob(f"{SRC}/*/*.jsonl")):
        n_files += 1
        proj = Path(f).parent.name
        for ln in open(f, encoding="utf-8", errors="ignore"):
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("type") != "user" or d.get("isSidechain"):
                continue
            o = d.get("origin")
            if not (isinstance(o, dict) and o.get("kind") == "human"):
                continue
            t = STRIP.sub("", text_of(d.get("message", {}).get("content"))).strip()
            if len(t) < 4 or t.startswith("<") or t.startswith("This session is being continued"):
                continue
            if t in seen:  # 分叉/续接对话会重复历史消息
                continue
            seen.add(t)
            rows.append({"time": d.get("timestamp", "")[:16], "proj": proj,
                         "session": d.get("sessionId", ""), "text": t})

    rows.sort(key=lambda r: r["time"])
    with open(OUT / "第1步_用户原话_全部.jsonl", "w", encoding="utf-8") as fo:
        for r in rows:
            fo.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 关键词筛选：命中某类关键词 且 含要求信号词
    hits, cat_cnt = [], Counter()
    cats_use = CATS_EXP if TOPIC == "实验" else CATS
    for r in rows:
        low = r["text"].lower()
        cats = [c for c, pat in cats_use.items() if re.search(pat, low, re.I)]
        if cats and re.search(SIGNAL, r["text"], re.I):
            r = dict(r, cats=cats)
            hits.append(r)
            cat_cnt.update(cats)
    with open(OUT / f"{'实验_' if TOPIC == '实验' else ''}第1步_筛选结果.jsonl", "w", encoding="utf-8") as fo:
        for r in hits:
            fo.write(json.dumps(r, ensure_ascii=False) + "\n")

    chars = sum(len(r["text"]) for r in hits)
    stat = [f"扫描对话文件：{n_files}",
            f"用户原话（去重后）：{len(rows)} 条",
            f"命中写作/绘图/PPT/表格：{len(hits)} 条，共 {chars} 字",
            "分类（可重叠）：" + "，".join(f"{k} {v}" for k, v in cat_cnt.most_common()),
            "时间范围：" + (f"{hits[0]['time']} ~ {hits[-1]['time']}" if hits else "无")]
    (OUT / f"{'实验_' if TOPIC == '实验' else ''}第1步_统计.txt").write_text("\n".join(stat) + "\n", encoding="utf-8")
    print("\n".join(stat))


if __name__ == "__main__":
    main()
