#!/usr/bin/env python3
"""
固定版式的汇报 PPT 生成工具（基于 KU 模板）。AI 只写一份 JSON 描述内容，版面由本工具排，避免每版从零画、版式简陋。

用法：python 汇报PPT.py <内容.json> <输出.pptx> [--严格] [--预览]
      --预览：用 LibreOffice 渲染出 <输出>_总览.png（所有页拼成一张），生成后必须用 Read 看这张图，文字检查发现不了折行、字小、留白
      --严格：有检查问题就以退出码 2 结束（样稿流程用它保证不交出有毛病的 PPT）

内容 JSON：
{
  "title": "封面标题（写结论）", "subtitle": "副标题", "date": "2026-11-22",
  "target_minutes": 15,          # 规定时长；按慢语速（×1.3）检查各页分钟数之和
  # 内容页（figure/table）可选："引导"（标题下一行的引导问题：为什么现在看这页，标题回答它；用户 11-22 要求每页由动机引导）、
  #   "术语"：[["术语", "一句白话解释"], ...]（放在页面右侧，术语加粗一行、解释一行；术语用社区常用叫法）
  # 每页可选："一句话"（写进讲稿第一行"这页只说一句"）、"minutes"（这一页讲几分钟）、"notes"（讲稿正文）
  "slides": [
    {"type": "figure",  "title": "结论式标题", "image": "drafts/图_v3.png", "note": "说明或出处（可选，写进讲稿，不上页面）"},
    {"type": "table",   "title": "结论式标题", "header": ["Method", "Acc. (%) ↑"],
                        "rows": [["Baseline", "94.53"], ["Ours", "95.40"]],
                        "bold": [[1, 1]], "note": "说明或出处（可选，写进讲稿，不上页面）"},
    {"type": "code",    "title": "结论式标题", "code": ["def f(x):", "    if ok(x):", "        return 1"],
                        "重点": {"1": "①", "2": "②"},      # 行号（从 0 起）→ 编号；这些行加粗变蓝，其余变灰
                        "解释": [["① ok(x)", "先检查来源"], ["② return 1", "通过就放行"]]},   # 右侧同编号逐行解释
    {"type": "section", "title": "章节名"},
    {"type": "takeaways", "title": "Take-aways", "points": ["最多 3 条", "...", "..."]}
  ]
}
版式（KU 模板）：封面 = 白底蓝字（Title Slide Nega，不用紫色封面）；内容页 = Title Only；章节 = 灰底；
收尾 = 黑底 Take-aways（不加 Thank You 页）。不做单独的大数字页：关键数字写进标题，配图或表。表格为三线表风格：无底色、表头上下细线、末行下细线，粗体只给指定格。
"""
import copy
import json
import os
import sys
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Inches, Pt

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "PPT" / "KU_template.pptx"
if not TEMPLATE.exists():   # 工具被复制到别处时（11-22 巡检发现），退回 harness 仓库里的模板
    TEMPLATE = Path(os.environ.get("RESEARCH_HARNESS", "~/research-harness")) / "templates" / "PPT" / "KU_template.pptx"
L_COVER, L_CONTENT, L_SECTION, L_TAKEAWAY = 1, 11, 14, 15   # 模板里的版式编号
BOX = (Inches(0.75), Inches(1.9), Inches(11.85), Inches(5.0))  # 内容区：左、上、宽、高
GREY = RGBColor(0x55, 0x55, 0x55)
SPEC_DIR = Path(".")   # 内容 JSON 所在目录（main 里设置），用于解析图片相对路径
TABLE_MIN_PT = 12    # 表格字号下限，再小就看不清
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def text_width_in(s: str, pt: float) -> float:
    """粗略估算文字宽度（英寸）：中日韩字符按 1 个字宽，其他按 0.55 个字宽。"""
    # 中日韩字符和圆圈编号（①②③，0x2460–0x24FF）按 1 个字宽，其他按 0.48
    w = sum(1.0 if (ord(ch) > 0x2E80 or 0x2460 <= ord(ch) <= 0x24FF) else 0.48 for ch in s)  # 0.48 按渲染实测校准
    return w * pt / 72


def clear_slides(prs):
    """删掉模板自带的示例页（同时删除关系，避免生成重复文件）。"""
    sld = prs.slides._sldIdLst
    for s in list(sld):
        prs.part.drop_rel(s.rId)
        sld.remove(s)


def set_title(slide, text, warns, page, pt=28, lines_ok=1):
    """写标题并固定字号；按估算宽度判断会不会超过允许行数。"""
    t = slide.shapes.title
    t.text = text
    if pt == 28:                                   # 内容页标题：固定在左上，给引导问题留出位置
        from pptx.enum.text import MSO_ANCHOR
        t.left, t.top, t.width, t.height = Inches(0.75), Inches(0.62), Inches(11.85), Inches(0.80)
        t.text_frame.vertical_anchor = MSO_ANCHOR.BOTTOM
        from pptx.enum.text import PP_ALIGN
        for para in t.text_frame.paragraphs:
            para.alignment = PP_ALIGN.LEFT            # 模板默认居中，视线要从左上开始
    for para in t.text_frame.paragraphs:
        para.font.size = Pt(pt)
    width_in = t.width / 914400 - 0.2
    if text_width_in(text, pt) > width_in * lines_ok:
        warns.append(f"第 {page} 页标题估计超过 {lines_ok} 行（{pt}pt），请缩短：{text[:40]}…")
    return t


def add_note(slide, text):
    """说明与出处写进讲稿备注，不印在页面上（用户 11-22：页面上不放出处和页底灰色小字）。"""
    if not text:
        return
    tf = slide.notes_slide.notes_text_frame
    tf.text = (tf.text + "\n" if tf.text else "") + text


def add_picture_fit(slide, path, left, top, width, height, warns, page, left_align=False):
    """图片按比例缩放到给定区域内并居中，尽量填满版面。"""
    if not os.path.exists(path) and not os.path.isabs(path):
        # 相对路径：当前目录找不到时，再按内容 JSON 所在目录及其上一级找（11-22 巡检：换目录运行图片丢失）
        for base in (SPEC_DIR, SPEC_DIR.parent):
            if (base / path).exists():
                path = str(base / path); break
    if not os.path.exists(path):
        warns.append(f"第 {page} 页图片不存在：{path}")
        return
    pic = slide.shapes.add_picture(path, left, top)
    r = min(width / pic.width, height / pic.height)
    pic.width, pic.height = int(pic.width * r), int(pic.height * r)
    pic.left = int(left) if left_align else int(left + (width - pic.width) / 2)   # 旁边有术语解释时靠左
    pic.top = int(top + Inches(0.1))   # 靠上：第一眼落在左上
    return pic


def _border(cell, side, width_pt=1.0):
    """给单元格加一条细线（side: T 上 / B 下），用于三线表。"""
    tcPr = cell._tc.get_or_add_tcPr()
    tag = {"T": "lnT", "B": "lnB"}[side]
    for old in tcPr.findall(f"{{{A_NS}}}{tag}"):
        tcPr.remove(old)
    ln = etree.SubElement(tcPr, f"{{{A_NS}}}{tag}", w=str(int(width_pt * 12700)))
    sf = etree.SubElement(ln, f"{{{A_NS}}}solidFill")
    etree.SubElement(sf, f"{{{A_NS}}}srgbClr", val="000000")


def add_table(slide, header, rows, bold, warns, page, box=BOX, left_align=False):
    BOX = box                      # 内容区可被"引导""术语"缩小
    ncol = len(header)
    data = [header] + rows
    # 每列所需宽度（按最长单元格），字号从 20pt 往下降直到放得下
    pt = 28 if len(rows) <= 6 else 20   # 行少就用大字：表是本页第一眼，别缩在中间一小块
    while True:
        # 表头是粗体，比估算宽约 12%（11-22 巡检："Seeds" 表头折行）
        need = [max([text_width_in(str(header[c]), pt) * 1.12] + [text_width_in(str(r[c]), pt) for r in rows]) + 0.35
                for c in range(ncol)]
        if sum(need) <= BOX[2] / 914400 or pt <= TABLE_MIN_PT:
            break
        pt -= 1
    if sum(need) > BOX[2] / 914400:
        warns.append(f"第 {page} 页表格在 {TABLE_MIN_PT}pt 下仍放不下，请缩短单元格文字或拆页")
    row_h = Inches(pt / 72 * 2.0)
    total_w = min(Inches(sum(need)), BOX[2])
    height = row_h * len(data)
    if height > BOX[3]:
        warns.append(f"第 {page} 页表格 {len(rows)} 行太高，请拆页")
    left = int(BOX[0]) if left_align else int(BOX[0] + (BOX[2] - total_w) / 2)   # 旁边有术语解释时靠左，表和解释挨着读
    top = int(BOX[1] + Inches(0.15))   # 靠上：第一眼落在左上（用户 11-22：视线从左到右、从上到下）
    import re as _re
    for c, v in enumerate(header):            # 表头不用带点的缩写（用户 11-22："什么什么.的写法看着太难受"）
        ab = _re.findall(r"\b[A-Za-z]{1,6}\.(?=\s|$|\))", str(v))
        if ab:
            warns.append(f"第 {page} 页表头\"{v}\"有带点缩写 {ab}：写完整词（如 Validation accuracy），放不下就删词")
    for r, row in enumerate(rows):
        for c, v in enumerate(row[:1]):       # 行名同样检查
            ab = _re.findall(r"\b[A-Za-z]{1,6}\.(?=\s|$|\))", str(v))
            if ab:
                warns.append(f"第 {page} 页行名\"{v}\"有带点缩写 {ab}：写完整词")
    for r, row in enumerate(rows):            # 空格子要写明原因（用户 11-22："这里没有数据什么意思?"）
        for c, v in enumerate(row):
            if str(v).strip() in ("—", "-", "–", "", "N/A", "n/a"):
                warns.append(f"第 {page} 页表格第 {r + 1} 行第 {c + 1} 列是\"{v}\"：写明原因（如\"论文未报告\"\"参照\"\"不适用\"）")
    shape = slide.shapes.add_table(len(data), ncol, left, top, total_w, height)
    tbl = shape.table
    # 去掉模板表格样式（底色、条纹），改为三线表
    tblPr = tbl._tbl.tblPr
    tblPr.set("firstRow", "0"); tblPr.set("bandRow", "0")
    sid = tblPr.find(f"{{{A_NS}}}tableStyleId")
    if sid is None:
        sid = etree.SubElement(tblPr, f"{{{A_NS}}}tableStyleId")
    sid.text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"  # No Style, No Grid
    for c in range(ncol):
        tbl.columns[c].width = int(total_w * need[c] / sum(need))
    bold = {tuple(x) for x in (bold or [])}
    for r, row in enumerate(data):
        tbl.rows[r].height = row_h
        for c in range(ncol):
            cell = tbl.cell(r, c)
            cell.fill.background()
            cell.text = str(row[c])
            p = cell.text_frame.paragraphs[0]
            p.font.size = Pt(pt)
            p.font.color.rgb = RGBColor(0, 0, 0)
            p.font.bold = (r == 0) or ((r - 1, c) in bold)
            cell.text_frame.word_wrap = False
            if r == 0:
                _border(cell, "T", 1.2); _border(cell, "B", 0.75)
            if r == len(data) - 1:
                _border(cell, "B", 1.2)
    if pt < 18:   # 投影上 16pt 的表在 13.3 英寸宽页面里已经很小（11-22 巡检）
        warns.append(f"第 {page} 页表格字号降到 {pt}pt，投影上看不清：缩短单元格文字（细节进讲稿）、删列或拆页")
    return shape


def push_para_fonts(prs):
    """把段落级字体（pPr/defRPr：字号、粗体、颜色）下推到每个文字块（run）。
    python-pptx 的 paragraph.font 只写 defRPr，LibreOffice 渲染时不认，字号会退回模板默认（11-22 巡检发现）。"""
    def fix(tf):
        for para in tf.paragraphs:
            pPr = para._p.pPr
            d = pPr.find(f"{{{A_NS}}}defRPr") if pPr is not None else None
            if d is None:
                continue
            for r in para.runs:
                rPr = r._r.get_or_add_rPr()
                for k in ("sz", "b"):
                    if d.get(k) is not None and rPr.get(k) is None:
                        rPr.set(k, d.get(k))
                fill = d.find(f"{{{A_NS}}}solidFill")
                if fill is not None and rPr.find(f"{{{A_NS}}}solidFill") is None:
                    rPr.insert(0, copy.deepcopy(fill))
    for slide in prs.slides:
        for sh in slide.shapes:
            if sh.has_text_frame:
                fix(sh.text_frame)
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    for c in row.cells:
                        fix(c.text_frame)


def add_lead(slide, text):
    """标题下一行的引导问题：黑色常规字、不加框。"""
    from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN
    tb = slide.shapes.add_textbox(Inches(0.75), Inches(1.45), Inches(11.85), Inches(0.45))
    tb.text_frame.word_wrap = True; tb.text_frame.auto_size = MSO_AUTO_SIZE.NONE   # 否则 LibreOffice 会居中
    p = tb.text_frame.paragraphs[0]; p.text = text; p.alignment = PP_ALIGN.LEFT
    p.font.size = Pt(18); p.font.color.rgb = RGBColor(0, 0, 0)


def add_terms(slide, terms, left, top, width, height):
    """页旁术语白话解释：术语加粗一行，解释下一行常规字重，不加框。"""
    from pptx.enum.text import MSO_AUTO_SIZE
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame; tf.word_wrap = True; tf.auto_size = MSO_AUTO_SIZE.NONE
    first = True
    for term, expl in terms:
        for txt, bold, after in ((term, True, 0), (expl, False, 10)):
            p = tf.paragraphs[0] if first else tf.add_paragraph(); first = False
            p.text = txt; p.font.size = Pt(16); p.font.bold = bold
            p.font.color.rgb = RGBColor(0, 0, 0); p.space_after = Pt(after)


BLUE = RGBColor(0x00, 0x4B, 0xA8)   # 关键代码行用蓝（配色死规定：只用黑、蓝、红）


def add_code(slide, lines, keys, left, top, width, height):
    """代码页左侧：浅灰底代码块；关键行加粗变蓝并在行末标编号，其余行变灰（用户 11-22：按 1→2→3 看、不回头）。"""
    from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN
    longest = max((len(x) for x in lines), default=1)
    pt = max(11, min(18, int((width / 914400 - 0.5) * 72 / (0.6 * longest))))   # 等宽字体约 0.6 字宽
    height = min(height, Inches(len(lines) * pt * 1.3 * 1.2 / 72 + 0.4))         # 底框贴合代码高度
    bg = slide.shapes.add_shape(1, left, top, width, height)
    bg.fill.solid(); bg.fill.fore_color.rgb = RGBColor(0xF3, 0xF4, 0xF6); bg.line.fill.background()
    bg.shadow.inherit = False
    tb = slide.shapes.add_textbox(left + Inches(0.15), top + Inches(0.15), width - Inches(0.3), height - Inches(0.3))
    tf = tb.text_frame; tf.word_wrap = True; tf.auto_size = MSO_AUTO_SIZE.NONE
    for k, line in enumerate(lines):
        p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT; p.line_spacing = 1.3
        key = keys.get(str(k))
        r = p.add_run(); r.text = line
        r.font.name = "Courier New"; r.font.size = Pt(pt); r.font.bold = bool(key)
        r.font.color.rgb = BLUE if key else RGBColor(0x9A, 0x9F, 0xA8)
        if key:
            m = p.add_run(); m.text = "  " + key
            m.font.size = Pt(pt + 2); m.font.bold = True; m.font.color.rgb = BLUE
    return bg


QUOTE_HINTS = ("的话说", "原话", "——")   # 页面上不放引用式旁白句（用户 11-22）


SLOW = 1.3            # 慢语速系数（用户语速不快、会停顿）
BODY_MAX = 90         # 每页正文字数上限（不含标题）；超了说明这页不够"扫一眼就懂"


def _body_chars(sd: dict) -> int:
    """估算一页正文字数：要点、表格、图注、引导、术语（不含标题和讲稿）。"""
    # 只算页面上看得到的字：note 只进讲稿，不算（11-22 巡检发现误报）；引导和术语在页面上，要算
    parts = list(sd.get("points", [])) + [sd.get("caption", ""), sd.get("引导", "")]
    parts += [x for pair in sd.get("术语", []) for x in pair]
    for row in [sd.get("header", [])] + sd.get("rows", []):
        parts += [str(x) for x in row]
    import re
    # 中文按字计，英文和数字按词计（原先按字母计，英文表格被严重高估）
    return sum(len(re.findall(r"[\u2e80-\u9fff]", str(p))) + len(re.findall(r"[A-Za-z0-9][\w.±%()\-]*", str(p)))
               for p in parts)


def build(spec: dict, out: str) -> list:
    warns = []
    prs = Presentation(str(TEMPLATE))
    clear_slides(prs)
    # 封面：白底蓝字
    s = prs.slides.add_slide(prs.slide_layouts[L_COVER])
    set_title(s, spec.get("title", ""), warns, 1, pt=36, lines_ok=2)
    for ph in s.placeholders:
        idx = ph.placeholder_format.idx
        if idx == 1:
            ph.text = spec.get("subtitle", "")
            ph.top = ph.top + Inches(0.30)          # 模板里副标题紧贴标题框，两行标题时几乎粘在一起（11-22 巡检）
            for para in ph.text_frame.paragraphs:
                para.font.size = Pt(20)
        elif idx == 10:
            ph.text = spec.get("date", "")
    slides = spec.get("slides", [])
    if not slides or slides[-1].get("type") != "takeaways":
        warns.append("最后一页不是 Take-aways（要求以 Take-aways 收尾）")
    for i, sd in enumerate(slides, start=2):
        t = sd.get("type")
        if t == "section":
            s = prs.slides.add_slide(prs.slide_layouts[L_SECTION])
            set_title(s, sd.get("title", ""), warns, i, pt=44, lines_ok=2)
            for ph in s.placeholders:
                if ph.placeholder_format.idx == 1:
                    ph.text = sd.get("subtitle", "")
        elif t == "takeaways":
            s = prs.slides.add_slide(prs.slide_layouts[L_TAKEAWAY])
            title = s.shapes.title
            title.text = sd.get("title", "Take-aways")
            title.left, title.top, title.width, title.height = Inches(0.9), Inches(0.9), Inches(11.5), Inches(1.4)
            pts = sd.get("points", [])
            if len(pts) > 3:
                warns.append(f"第 {i} 页 Take-aways 有 {len(pts)} 条，超过 3 条")
            tb = s.shapes.add_textbox(Inches(1.6), Inches(2.6), Inches(10.1), Inches(4.0))
            tf = tb.text_frame; tf.word_wrap = True
            for k, txt in enumerate(pts):
                p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
                p.text = f"{k + 1}.  {txt}"
                p.font.size = Pt(26); p.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                p.space_after = Pt(18)
        else:
            s = prs.slides.add_slide(prs.slide_layouts[L_CONTENT])
            title = sd.get("title", "")
            set_title(s, title, warns, i)
            if title.rstrip().endswith(("？", "?")):
                warns.append(f"第 {i} 页标题是问句：改成本页图表能直接证明的陈述句（问题放进\"引导\"）")
            box = list(BOX)
            if sd.get("引导"):
                add_lead(s, sd["引导"])
                box[1] += Inches(0.35); box[3] -= Inches(0.35)
            elif t in ("figure", "table"):
                warns.append(f"第 {i} 页没有\"引导\"：每页先说为什么现在看这页（承接上一页的问题），标题回答它")
            tw = Inches(3.3)                             # 术语栏窄一点，别把主表挤小
            if sd.get("术语"):
                box[2] -= tw + Inches(0.4)               # 右侧给术语解释留位置
            for txt in list(sd.get("points", [])) + [sd.get("caption", ""), sd.get("引导", "")]:
                if any(h in str(txt) for h in QUOTE_HINTS):
                    warns.append(f"第 {i} 页有引用式旁白句：口头带过、写进讲稿 —— {str(txt)[:30]}")
            main = None
            if t == "figure":
                main = add_picture_fit(s, sd.get("image", ""), *box, warns, i, left_align=bool(sd.get("术语")))
            elif t == "table":
                main = add_table(s, sd.get("header", []), sd.get("rows", []), sd.get("bold"), warns, i, box,
                                 left_align=bool(sd.get("术语")))
            elif t == "code":
                half = int(box[2] * 0.52)
                main = add_code(s, sd.get("code", []), sd.get("重点", {}), box[0], box[1] + Inches(0.15), half, box[3] - Inches(0.3))
                rows = sd.get("解释", [])
                if len(rows) != len(sd.get("重点", {})):
                    warns.append(f"第 {i} 页代码重点有 {len(sd.get('重点', {}))} 处，右侧解释有 {len(rows)} 行：要一一对应")
                if rows:
                    add_table(s, ["代码", "意思"], rows, None, warns, i,
                              (box[0] + half + Inches(0.3), box[1], box[2] - half - Inches(0.3), box[3]), left_align=True)
            elif t == "number":
                # 用户 11-22：单独一个大数字的页面太空、太像 AI，已停用
                warns.append(f"第 {i} 页用了已停用的大数字页：把关键数字写进标题，并配图（figure）或表（table）")
            else:
                warns.append(f"第 {i} 页类型未知：{t}")
            if sd.get("术语"):                            # 紧贴主图表右侧、顶部对齐，表和解释挨着读
                x = (main.left + main.width + Inches(0.4)) if main is not None else box[0] + box[2] + Inches(0.4)
                y = main.top if main is not None else box[1]
                add_terms(s, sd["术语"], x, y, tw, box[3])
            add_note(s, sd.get("note", ""))
    # 讲稿：第一行"这页只说一句"，再接正文；按慢语速检查总时长；检查每页正文字数
    total = 0.0
    for k, (sd, s) in enumerate(zip([{"一句话": spec.get("一句话", ""), "minutes": spec.get("cover_minutes", 0.25)}] + slides,
                                      prs.slides), start=1):
        note = ""
        if sd.get("一句话"):
            note = "这页只说一句：" + sd["一句话"]
        if sd.get("notes"):
            note = (note + "\n" if note else "") + sd["notes"]
        if note:
            s.notes_slide.notes_text_frame.text = note
        elif k > 1 and sd.get("type") not in ("section",):
            warns.append(f"第 {k} 页没有\"一句话\"（讲稿第一行，翻到这页扫一眼就能接上）")
        total += float(sd.get("minutes", 0) or 0)
        if k > 1 and sd.get("type") in ("figure", "table") and _body_chars(sd) > BODY_MAX:
            warns.append(f"第 {k} 页正文约 {_body_chars(sd)} 字，超过 {BODY_MAX}：缩成关键词 + 例子，细节放讲稿")
    tgt = spec.get("target_minutes")
    if tgt and total:
        if total * SLOW > float(tgt):
            warns.append(f"各页合计 {total:.2f} 分钟，按慢语速 ×{SLOW} 约 {total * SLOW:.1f} 分钟，超过规定的 {tgt} 分钟：删页，不要靠讲快")
    elif tgt:
        warns.append("给了 target_minutes 但各页没有 minutes，无法检查时长")
    push_para_fonts(prs)
    prs.save(out)
    return warns


def preview(pptx: str) -> str:
    """PPT → PDF → 每页 PNG → 拼成一张总览图；返回路径（失败返回空串）。"""
    import glob, shutil, subprocess, tempfile
    tmp = tempfile.mkdtemp()
    try:
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, pptx],
                       capture_output=True, timeout=180)
        pdfs = glob.glob(os.path.join(tmp, "*.pdf"))
        if not pdfs:
            return ""
        subprocess.run(["pdftoppm", "-png", "-r", "45", pdfs[0], os.path.join(tmp, "p")], capture_output=True, timeout=180)
        from PIL import Image
        ims = [Image.open(f) for f in sorted(glob.glob(os.path.join(tmp, "p-*.png")))]
        if not ims:
            return ""
        w, h = ims[0].size; c = 3; r = (len(ims) + c - 1) // c
        g = Image.new("RGB", (c * (w + 8) + 8, r * (h + 8) + 8), "#888888")
        for i, im in enumerate(ims):
            g.paste(im, (8 + (i % c) * (w + 8), 8 + (i // c) * (h + 8)))
        out = os.path.splitext(pptx)[0] + "_总览.png"
        g.save(out)
        return out
    except Exception:
        return ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    spec = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    SPEC_DIR = Path(args[0]).resolve().parent
    w = build(spec, args[1])
    print(f"已生成：{args[1]}（{len(spec.get('slides', [])) + 1} 页）")
    for x in w:
        print(f"[检查] {x}")
    if "--预览" in sys.argv:
        pv = preview(args[1])
        print(f"[预览] {pv}  ← 用 Read 打开逐页看：折行、字太小、留白、元素重叠" if pv else "[预览] 渲染失败（需要 soffice 与 pdftoppm）")
    sys.exit(2 if (w and "--严格" in sys.argv) else 0)
