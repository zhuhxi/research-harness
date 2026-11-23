"""
固定评估（只读，AI 不得修改）。默认采用"测试集封存"：不直接给出测试标签，测试指标只能经 score()/correct_mask() 获取，每次使用都被记录。

新建课题后由 Research Director 填写 _load_raw()（数据怎么读、怎么划分），其余不用改。
规则：选参、早停、挑基线只用验证集；测试集只用于最终报告，同一主张只判一次。

用法（实验代码里）：
    from eval.评估 import load_split, score, correct_mask
    (xtr, ytr), (xva, yva), (xte, _) = load_split()      # 测试标签封存，返回 None
    acc = score(pred_test, h="H03", tag="pilot_s0")       # 测试指标，并记录一次使用
    ok = correct_mask(pred_test, h="H03", tag="配对")      # 逐样本对错（配对 bootstrap 用），同样记录

分类以外的任务（回归、重建等）：把 _per_sample() 换成对应的逐样本指标即可（score 取其均值）。
"""
import datetime as _dt
import json as _json
import os as _os

import numpy as np

# 类别名（可选，样本拼图用；不填就显示类别编号）
CLASS_NAMES = None

# 测试集使用记录（只追加；守卫禁止 AI 用工具改它）
_USE_LOG = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                         "logs", "测试集使用.jsonl")


def _load_raw():
    """【由人填写】返回 (训练, 验证, 测试) 三组 (输入, 标签)，划分固定（种子写死）。
    例：CIFAR-10 见 research_projects/玩具课题_CIFAR快训/eval/评估.py。"""
    raise NotImplementedError("请在新建课题后填写 eval/评估.py 的 _load_raw()")


def load_split():
    """返回 (训练, 验证, 测试) 三组 (输入, 标签)；测试标签封存，返回 None。"""
    tr, va, (xte, _) = _load_raw()
    return tr, va, (xte, None)


def _log_use(h, tag, n):
    _os.makedirs(_os.path.dirname(_USE_LOG), exist_ok=True)
    with open(_USE_LOG, "a", encoding="utf-8") as f:
        f.write(_json.dumps({"time": _dt.datetime.now().isoformat(timespec="seconds"),
                             "h": h, "tag": tag, "n": int(n)}, ensure_ascii=False) + "\n")


def _per_sample(pred, y):
    """逐样本指标（默认：分类是否正确）。"""
    return np.asarray(pred) == y


def correct_mask(pred_test, h: str, tag: str = ""):
    """逐样本指标数组（配对检验用）。必须写明假设编号 h，每次调用记录一次测试集使用。"""
    assert h, "必须写明是哪个假设在用测试集，如 h='H03'"
    # 批准门（用户 11-22）：测试集只用于最终报告，每个假设用一次都要人批准（approvals/测试集_H编号.ok）
    _ok = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "approvals", f"测试集_{h}.ok")
    if not _os.path.exists(_ok):
        raise PermissionError(f"测试集已封存：{h} 没有批准文件 approvals/测试集_{h}.ok。"
                              f"选参、比较请用验证集；需要最终报告时，把请求写进 research-state.yaml 的 pending_approvals，继续做别的。")
    *_, (_, yte) = _load_raw()
    yte = np.asarray(yte)
    pred_test = np.asarray(pred_test)
    assert len(pred_test) == len(yte), "预测长度与测试集不符"
    _log_use(h, tag, len(yte))
    return _per_sample(pred_test, yte)


def score(pred_test, h: str, tag: str = "") -> float:
    """测试集总指标（默认：准确率）。必须写明假设编号 h。"""
    return float(np.mean(correct_mask(pred_test, h, tag)))
