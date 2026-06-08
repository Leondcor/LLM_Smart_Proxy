#!/usr/bin/env python3
"""
脱敏引擎 — 共享模块，同时供 proxy.py 和 sanitize.py CLI 使用
"""
import re
import yaml
import os
import json
from datetime import datetime

# ── 规则加载 ──
def load_rules(rules_path=None):
    if rules_path is None:
        rules_path = os.path.join(os.path.dirname(__file__), "sanitizer_rules.yaml")
    with open(rules_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── 规则编译 ──
def compile_entities(entities_config):
    """编译实体规则为可执行列表，按优先级降序排列"""
    compiled = []
    for entity in entities_config:
        if entity.get("enabled") is False:
            continue
        rules = []
        for rule in entity.get("rules", []):
            flags = 0
            if not rule.get("case_sensitive", True):
                flags |= re.IGNORECASE
            try:
                compiled_re = re.compile(rule["pattern"], flags)
                rules.append({
                    "regex": compiled_re,
                    "description": rule.get("description", ""),
                    "min_length": rule.get("min_length", 0),
                    "exclude_pattern": rule.get("exclude_pattern"),
                })
            except re.error as e:
                print(f"[WARN] 规则编译失败 {entity['id']}: {rule['pattern']} — {e}")
        if rules:
            compiled.append({
                "id": entity["id"],
                "name": entity.get("name", entity["id"]),
                "priority": entity.get("priority", 50),
                "strategy": entity.get("strategy", "placeholder"),
                "placeholder": entity.get("placeholder", "[***]"),
                "mask_char": entity.get("mask_char", "*"),
                "keep_last": entity.get("keep_last", 0),
                "counter": entity.get("counter", False),
                "rules": rules,
            })
    compiled.sort(key=lambda x: x["priority"], reverse=True)
    return compiled


# ── 替换函数 ──
def _apply_strategy(entity, text, counter_state=None):
    """根据策略生成替换文本"""
    strategy = entity["strategy"]

    if strategy == "placeholder":
        placeholder = entity["placeholder"]
        if entity.get("counter") and "{index}" in placeholder:
            if counter_state is None:
                counter_state = {"idx": 0}
            counter_state["idx"] += 1
            return placeholder.replace("{index}", str(counter_state["idx"]))
        return placeholder

    elif strategy == "mask":
        mask_char = entity.get("mask_char", "*")
        keep_last = entity.get("keep_last", 0)
        if keep_last > 0 and len(text) > keep_last:
            return mask_char * (len(text) - keep_last) + text[-keep_last:]
        return mask_char * len(text)

    elif strategy == "remove":
        return ""

    return "[***]"


# ── 白名单保护 ──
def _protect_whitelist(text, whitelist_terms):
    """用占位符替换白名单术语，防止被误脱敏"""
    if not whitelist_terms:
        return text, {}
    protected = {}
    for i, term in enumerate(whitelist_terms):
        token = f"__WL_{i}__"
        text = text.replace(term, token)
        protected[token] = term
    return text, protected


def _restore_whitelist(text, protected):
    """恢复白名单术语"""
    for token, original in protected.items():
        text = text.replace(token, original)
    return text


# ── 核心脱敏函数 ──
def sanitize_text(text, rules_config, counter_state=None):
    """
    对单段文本执行脱敏。

    参数:
        text: 原始文本
        rules_config: 完整规则配置（load_rules 的返回值）
        counter_state: 可选的计数器状态字典，用于跨消息统一编号

    返回:
        (sanitized_text, replacements_list)
        replacements_list: [{"entity": "COMPANY_NAME", "original": "XX公司", "replaced": "[公司1]", "position": 42}, ...]
    """
    entities = compile_entities(rules_config.get("entities", []))
    whitelist_terms = rules_config.get("whitelist", {}).get("terms", [])
    settings = rules_config.get("settings", {})

    # 白名单保护
    text, protected = _protect_whitelist(text, whitelist_terms)

    replacements = []
    if counter_state is None:
        counter_state = {}

    # 按优先级处理每个实体类型
    for entity in entities:
        eid = entity["id"]
        if eid not in counter_state:
            counter_state[eid] = {"idx": 0}

        for rule in entity["rules"]:
            regex = rule["regex"]
            min_len = rule.get("min_length", 0)
            exclude_pat = rule.get("exclude_pattern")

            matches = list(regex.finditer(text))
            if not matches:
                continue

            # 从后往前替换，避免位置偏移
            for m in reversed(matches):
                matched_text = m.group(0)
                # 长度检查
                if min_len > 0 and len(matched_text) < min_len:
                    continue
                # 排除模式检查
                if exclude_pat and re.search(exclude_pat, matched_text):
                    continue

                replacement = _apply_strategy(entity, matched_text, counter_state[eid])
                start, end = m.start(), m.end()

                replacements.append({
                    "entity": eid,
                    "entity_name": entity["name"],
                    "original": matched_text,
                    "replaced": replacement,
                    "position": start,
                    "length": end - start,
                })
                # 执行替换
                text = text[:start] + replacement + text[end:]

    # 恢复白名单
    text = _restore_whitelist(text, protected)

    # 自定义关键词精确替换
    custom = rules_config.get("custom_keywords", {})
    for kw_type, kw_list in custom.items():
        for kw in kw_list:
            if kw in text:
                placeholder_map = {
                    "companies": "[公司名]",
                    "projects": "[项目名]",
                    "persons": "[人员]",
                }
                placeholder = placeholder_map.get(kw_type, "[***]")
                replacements.append({
                    "entity": f"CUSTOM_{kw_type.upper()}",
                    "entity_name": f"自定义{kw_type}",
                    "original": kw,
                    "replaced": placeholder,
                    "position": text.find(kw),
                    "length": len(kw),
                })
                text = text.replace(kw, placeholder)

    return text, replacements


# ── 文件检测 ──
def _detect_mime_from_data_url(data_url):
    """从 data: URL 中提取 MIME 类型，如 data:image/png;base64,... → image/png"""
    if not data_url or not isinstance(data_url, str):
        return None
    m = re.match(r"data:([^;]+)", data_url)
    return m.group(1) if m else None


def _classify_file(mime_type):
    """
    分类文件类型，返回 (category, can_sanitize_text)
    category: "image" | "document" | "text" | "unknown"
    can_sanitize_text: 是否可提取文本进行脱敏
    """
    if not mime_type:
        return "unknown", False

    mime = mime_type.lower()
    # 图片 — 含文字但 proxy 层无法 OCR
    if mime.startswith("image/"):
        return "image", False
    # PDF — 二进制，需要专门的解析库
    if mime == "application/pdf":
        return "document", False
    # Word
    if mime in ("application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
        return "document", False
    # 纯文本
    if mime.startswith("text/") or mime == "application/json":
        return "text", True
    return "unknown", False


def _scan_multimodal(content_array):
    """
    扫描多模态 content 数组，返回文件检测报告。

    返回: {
        "has_files": bool,
        "files": [{"type": "image_url"/"file", "mime": "image/png", "category": "image", "index": 0}, ...],
        "text_blocks": [{"index": 0, "text": "..."}, ...],
        "unsanitizable": 无法脱敏的文件数
    }
    """
    report = {"has_files": False, "files": [], "text_blocks": [], "unsanitizable": 0}

    for i, part in enumerate(content_array):
        if not isinstance(part, dict):
            continue

        if part.get("type") == "text" and isinstance(part.get("text"), str):
            report["text_blocks"].append({"index": i, "text": part["text"]})

        elif part.get("type") == "image_url":
            url = part.get("image_url", {}).get("url", "")
            mime = _detect_mime_from_data_url(url)
            cat, can_sanitize = _classify_file(mime)
            report["has_files"] = True
            report["files"].append({
                "type": "image_url",
                "mime": mime or "unknown",
                "category": cat,
                "index": i,
                "url_preview": url[:60] + "..." if len(url) > 60 else url,
            })
            if not can_sanitize:
                report["unsanitizable"] += 1

        elif part.get("type") in ("file", "file_url", "file_id"):
            mime = part.get("mime_type", part.get("format", ""))
            cat, can_sanitize = _classify_file(mime)
            report["has_files"] = True
            report["files"].append({
                "type": part["type"],
                "mime": mime or "unknown",
                "category": cat,
                "index": i,
                "file_id": part.get("file_id", part.get("file_url", ""))[:80],
            })
            if not can_sanitize:
                report["unsanitizable"] += 1

    return report


# ── 消息级脱敏（用于 proxy.py）──
def sanitize_messages(body_data, rules_config):
    """
    对请求体中的所有消息内容执行脱敏。
    支持纯文本 content 和多模态 content 数组。

    返回:
        (sanitized_body_data, total_replacements, file_reports)
        file_reports: [{msg_index, report}] 多模态消息的文件检测报告列表
    """
    total_replacements = []
    file_reports = []
    counter_state = {}

    for mi, msg in enumerate(body_data.get("messages", [])):
        content = msg.get("content")

        if isinstance(content, str):
            sanitized, reps = sanitize_text(content, rules_config, counter_state)
            msg["content"] = sanitized
            total_replacements.extend(reps)

        elif isinstance(content, list):
            # 多模态 content 数组
            report = _scan_multimodal(content)
            if report["has_files"]:
                file_reports.append({"msg_index": mi, "report": report})

            # 只对 text 块做脱敏
            for tb in report["text_blocks"]:
                sanitized, reps = sanitize_text(tb["text"], rules_config, counter_state)
                content[tb["index"]]["text"] = sanitized
                total_replacements.extend(reps)

    return body_data, total_replacements, file_reports


# ── 日志 ──
def log_mapping(replacements, rules_config, source="proxy"):
    """将脱敏映射记录到日志文件"""
    settings = rules_config.get("settings", {})
    if not settings.get("log_mapping", True):
        return

    log_path = settings.get("mapping_log", "../logs/sanitizer_mapping.log")
    log_dir = os.path.join(os.path.dirname(__file__), os.path.dirname(log_path))
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    abs_log = os.path.join(os.path.dirname(__file__), log_path)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(abs_log, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"[{timestamp}] source={source} | {len(replacements)} 处替换\n")
        f.write(f"{'='*60}\n")
        for r in replacements:
            f.write(f"  [{r['entity']}] {r['original'][:80]} → {r['replaced']}\n")


# ── 摘要 ──
def sanitize_summary(replacements, file_reports=None):
    """生成脱敏操作摘要"""
    lines = []
    if replacements:
        by_entity = {}
        for r in replacements:
            eid = r["entity"]
            by_entity[eid] = by_entity.get(eid, 0) + 1

        parts = []
        for eid, count in sorted(by_entity.items(), key=lambda x: -x[1]):
            name = next((r["entity_name"] for r in replacements if r["entity"] == eid), eid)
            parts.append(f"{name}×{count}")

        lines.append(f"脱敏 {len(replacements)} 处：{', '.join(parts)}")
    else:
        lines.append("无敏感信息检出")

    if file_reports:
        lines.append(file_report_summary(file_reports))

    return " | ".join(lines)


# ── 仅检测不替换（detect 模式）──
def detect_only(body_data, rules_config):
    """
    检测请求体中的敏感信息，不修改原文。
    返回 (replacements_list, file_reports)
    """
    import copy
    dup = copy.deepcopy(body_data)
    _, replacements, file_reports = sanitize_messages(dup, rules_config)
    return replacements, file_reports


def detect_summary(replacements, file_reports=None):
    """生成检测报告摘要"""
    lines = []

    # 文本脱敏部分
    if replacements:
        by_entity = {}
        for r in replacements:
            eid = r["entity"]
            by_entity[eid] = by_entity.get(eid, 0) + 1
        lines.append(f"⚠ 检出 {len(replacements)} 处敏感信息：")
        for eid, count in sorted(by_entity.items(), key=lambda x: -x[1]):
            name = next((r["entity_name"] for r in replacements if r["entity"] == eid), eid)
            lines.append(f"  [{name}] ×{count}")
    else:
        lines.append("✓ 文本未检出敏感信息")

    # 文件检测部分
    if file_reports:
        for fr in file_reports:
            rpt = fr["report"]
            for f in rpt["files"]:
                badge = "⚠" if f["category"] in ("image", "document") else "📄"
                lines.append(f"  {badge} 附件 [{f['type']}] {f['mime']} — 文本脱敏不可覆盖")

    return "\n".join(lines)


def file_report_summary(file_reports):
    """生成文件检测摘要（供 proxy 日志使用）"""
    if not file_reports:
        return ""

    parts = []
    for fr in file_reports:
        rpt = fr["report"]
        for f in rpt["files"]:
            cat_label = {"image": "图片", "document": "文档", "text": "文本", "unknown": "未知"}
            label = cat_label.get(f["category"], f["category"])
            parts.append(f"{label}({f['mime']})")
    return f"📎 附件检测: {len(file_reports)}条消息含文件 — {', '.join(parts)}"
