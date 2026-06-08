#!/usr/bin/env python3
"""
LLM Smart Proxy v10.1 — 配置化通用代理
自动端口检测：启动时若配置端口被占用，自动尝试下一个端口
- 智能路由：独立问题跳过压缩，直接透传
- 两步编排：大请求 → 摘要 → 紧凑请求（按客户端差异化）
- 三重脱敏模式：auto/off/detect + 双端口安全模式
- 客户端识别：User-Agent → 来源工具 + 策略自适应
- 响应后处理：tool_calls 格式修复（mini 模型常见错误）
- Token 用量统计：按客户端/模型/会话维度，异常消耗告警
- 动态运行时控制：客户端聊天指令 /sanitize on/off + Admin API
- YAML 配置化，可嵌入 Hermes / Codex CLI / Cline / OpenClaw / WorkBuddy
"""
import http.client
import json
import datetime
import os
import sys
import hashlib
import re
import threading
import yaml

# ── 脱敏引擎 ──
try:
    from sanitizer_engine import load_rules as load_sanitizer_rules, sanitize_messages, log_mapping, sanitize_summary, detect_only, detect_summary, file_report_summary
    SANITIZER_AVAILABLE = True
except ImportError:
    SANITIZER_AVAILABLE = False

# ── 配置加载 ──
def load_config():
    cfg_path = os.environ.get("PROXY_CONFIG",
                              os.path.join(os.path.dirname(__file__), "config.yaml"))
    if not os.path.exists(cfg_path):
        print(f"\x1b[91m[ERROR] 配置文件不存在: {cfg_path}\x1b[0m")
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

CONFIG = load_config()

# 快捷访问
TARGET_HOST = CONFIG["upstream"]["host"]
TARGET_PORT = CONFIG["upstream"]["port"]
PROXY_PORT = CONFIG["proxy"]["port"]
UPSTREAM_TIMEOUT = CONFIG["upstream"]["timeout_sec"]
API_KEY = os.environ.get("PROXY_API_KEY", CONFIG["upstream"].get("api_key", ""))

BODY_STRIP_KEYS = set(CONFIG["body_strip"]["keys"])
HEADER_STRIP_PREFIXES = tuple(CONFIG["header_strip"]["prefixes"])

MAX_SYSTEM_CHARS = CONFIG["truncation"]["max_system_chars"]
MAX_MESSAGE_CHARS = CONFIG["truncation"]["max_message_chars"]
MAX_MESSAGES = CONFIG["truncation"]["max_messages"]
MAX_TOTAL_BYTES = CONFIG["truncation"]["max_total_bytes"]
HARD_TRUNCATE_TAIL_CHARS = CONFIG["truncation"]["hard_truncate_tail_chars"]

ROUTING_PROMPT = CONFIG["routing"]["prompt"]
COMPACT_THRESHOLD = CONFIG["orchestrator"]["compact_threshold_bytes"]
SUMMARY_MAX_CHARS = CONFIG["orchestrator"]["summary_max_chars"]

# 摘要缓存：{system指纹: 摘要文本}，同一会话复用
_summary_cache = {}

# ── 动态运行时状态 ──
_runtime_state = {
    "sanitizer_mode": None,    # None=使用config.yaml; "auto"/"off"/"detect"=覆盖
    "file_block": None,        # None=使用config.yaml; True/False=覆盖
}
_runtime_lock = threading.Lock()

# ── 魔法命令模式 ──
MAGIC_COMMANDS = [
    # /sanitize on|off|detect|status
    (re.compile(r'^/sanitize\s+on\s*$', re.I), 'enable'),
    (re.compile(r'^/sanitize\s+off\s*$', re.I), 'disable'),
    (re.compile(r'^/sanitize\s+detect\s*$', re.I), 'set_detect'),
    (re.compile(r'^/sanitize\s+mode\s+(\S+)\s*$', re.I), 'set_mode'),
    (re.compile(r'^/sanitize\s+status\s*$', re.I), 'status'),
    (re.compile(r'^/sanitize\s*$', re.I), 'status'),
    # 中文指令
    (re.compile(r'^(开启|启用|打开)\s*脱敏'), 'enable'),
    (re.compile(r'^(关闭|停用|禁用)\s*脱敏'), 'disable'),
    (re.compile(r'^脱敏\s*(开启|启用|打开)'), 'enable'),
    (re.compile(r'^脱敏\s*(关闭|停用|禁用)'), 'disable'),
    (re.compile(r'^脱敏\s*模式\s*(.+)'), 'set_mode'),
    (re.compile(r'^脱敏\s*状态'), 'status'),
    (re.compile(r'^查看\s*脱敏'), 'status'),
]

MODE_NAME_CN = {"auto": "自动脱敏（替换敏感信息）", "off": "已关闭", "detect": "仅检测（不替换原文）"}

def _normalize_mode(raw):
    """标准化模式名称，支持中英文"""
    m = raw.strip().lower()
    mapping = {
        'auto': 'auto', '自动': 'auto', 'auto模式': 'auto', '替换': 'auto',
        'off': 'off', '关闭': 'off', 'off模式': 'off', '禁用': 'off',
        'detect': 'detect', '检测': 'detect', '仅检测': 'detect', 'detect模式': 'detect', '探测': 'detect',
    }
    return mapping.get(m, m)

def _get_effective_san_mode():
    """获取当前生效的脱敏模式：运行时覆盖 > config.yaml"""
    with _runtime_lock:
        if _runtime_state["sanitizer_mode"] is not None:
            return _runtime_state["sanitizer_mode"]
    return CONFIG.get("sanitizer", {}).get("mode", "off")

def _get_effective_file_block():
    """获取当前生效的文件拦截策略"""
    with _runtime_lock:
        if _runtime_state["file_block"] is not None:
            return _runtime_state["file_block"]
    return CONFIG.get("file_detect", {}).get("on_attachment") == "block"

def _get_runtime_status():
    """获取完整运行时状态快照"""
    with _runtime_lock:
        return {
            "sanitizer_mode": _get_effective_san_mode(),
            "sanitizer_mode_cn": MODE_NAME_CN.get(_get_effective_san_mode(), "未知"),
            "file_block": _get_effective_file_block(),
            "runtime_override": _runtime_state["sanitizer_mode"] is not None,
            "config_mode": CONFIG.get("sanitizer", {}).get("mode", "off"),
            "config_file_block": CONFIG.get("file_detect", {}).get("on_attachment") == "block",
            "safe_port": CONFIG.get("sanitizer", {}).get("port_safe", ""),
            "dynamic_control": CONFIG.get("dynamic_control", {}).get("enabled", True),
        }

def check_magic_command(text):
    """检查文本是否为魔法命令。返回 (action, mode_value) 或 (None, None)。
    action: enable|disable|set_detect|set_mode|status
    """
    if not text or not isinstance(text, str):
        return None, None

    cfg = CONFIG.get("dynamic_control", {})
    if not cfg.get("enabled", True) or not cfg.get("magic_command", True):
        return None, None

    text = text.strip()

    for pattern, action in MAGIC_COMMANDS:
        m = pattern.match(text)
        if m:
            if action == 'set_mode':
                raw_mode = m.group(1).strip()
                return 'set_mode', _normalize_mode(raw_mode)
            elif action == 'set_detect':
                return 'set_mode', 'detect'
            elif action == 'enable':
                return 'enable', 'auto'
            elif action == 'disable':
                return 'disable', 'off'
            elif action == 'status':
                return 'status', None

    return None, None

# ── 客户端识别 ──
def detect_client(user_agent):
    """
    根据 User-Agent 识别客户端类型。
    返回 (agent_name, details_dict)
    """
    cfg = CONFIG.get("client_detect", {})
    if not cfg.get("enabled", True) or not user_agent:
        return "unknown", {}

    ua_lower = user_agent.lower()
    for entry in cfg.get("patterns", []):
        agent = entry.get("agent", "unknown")
        matches = entry.get("match", [])
        if not matches:
            continue  # fallback 在最后
        for keyword in matches:
            if keyword.lower() in ua_lower:
                return agent, {"user_agent": user_agent, "match": keyword}
    # 最后的 fallback entry
    for entry in cfg.get("patterns", []):
        if not entry.get("match"):
            return entry.get("agent", "unknown"), {"user_agent": user_agent, "match": "fallback"}
    return "unknown", {"user_agent": user_agent}

# ── 按客户端获取编排参数 ──
def _get_orch_config(client):
    """合并默认 + 客户端特定编排参数"""
    orch = CONFIG.get("orchestrator", {})
    per_client = orch.get("per_client", {}).get(client, {})
    return {
        "compact_threshold": per_client.get("compact_threshold_bytes", orch.get("compact_threshold_bytes", 6000)),
        "summary_max_chars": per_client.get("summary_max_chars", orch.get("summary_max_chars", 300)),
        "system_extract_chars": per_client.get("system_extract_chars", 3000),
    }

def log(tag, data):
    now = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"\n\x1b[96m[{now}] {tag}\x1b[0m")
    print("\x1b[90m" + "-" * 60 + "\x1b[0m")
    try:
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
        elif isinstance(data, (dict, list)):
            text = json.dumps(data, ensure_ascii=False, indent=2)
        else:
            text = str(data)
        print(text)
    except Exception as e:
        print(f"\x1b[91m[解析失败: {e}]\x1b[0m")
        print(repr(data[:500]))

def _fingerprint(system_content):
    """生成 system prompt 指纹，用于缓存键"""
    key = (system_content or "")[:500]
    return hashlib.md5(key.encode()).hexdigest()[:12]

def _extract_model(body_data):
    return body_data.get("model", "gpt-5.4-mini")

def _build_summarize_request(body_data, all_system, all_history, orch_cfg=None):
    """构建摘要请求 — 极简，~1KB。orch_cfg 为客户端差异化参数"""
    if orch_cfg is None:
        orch_cfg = {}
    model = _extract_model(body_data)
    sys_extract = orch_cfg.get("system_extract_chars", 3000)
    text = ""
    if all_system:
        text += "系统上下文:\n" + all_system[:sys_extract] + "\n\n"
    if all_history:
        text += "对话历史:\n" + all_history[:2000]
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "用200字以内提炼以下信息的关键要点：用户是谁、正在做什么、当前关心什么。只输出摘要。"},
            {"role": "user", "content": text}
        ]
    }

def _build_compact_request(body_data, summary, last_user_msg, orch_cfg=None):
    """构建最终紧凑请求 — 摘要 + 用户提问，~1KB。orch_cfg 为客户端差异化参数"""
    if orch_cfg is None:
        orch_cfg = {}
    model = _extract_model(body_data)
    content = last_user_msg.get("content", "")
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": f"你是AI助手。对话上下文：{summary}"},
            {"role": "user", "content": content}
        ]
    }

def orchestrate_two_step(body_data, orch_cfg=None):
    """两步法：先摘要上下文 → 再紧凑回答。返回压缩后的请求体 bytes，失败返回 None"""
    if orch_cfg is None:
        orch_cfg = {}
    summary_max = orch_cfg.get("summary_max_chars", SUMMARY_MAX_CHARS)
    msgs = body_data.get("messages", [])
    all_system = ""
    history_msgs = []
    last_user = None
    for m in msgs:
        role = m.get("role", "")
        if role == "system":
            all_system += m.get("content", "") + "\n"
        elif role == "user":
            history_msgs.append(m)
            last_user = m
        elif role == "assistant":
            history_msgs.append(m)

    if not last_user:
        return None

    # 缓存命中 → 直接用
    fp = _fingerprint(all_system)
    if fp in _summary_cache:
        log("ORCHESTRATOR", f"缓存命中 {fp} → 跳过摘要")
        return json.dumps(_build_compact_request(body_data, _summary_cache[fp], last_user, orch_cfg),
                          ensure_ascii=False).encode("utf-8")

    # 组装历史文本
    all_history = ""
    for m in history_msgs[-5:]:
        role = m.get("role", "")
        content = str(m.get("content", ""))[:500]
        all_history += f"[{role}]: {content}\n"

    # Step 1: 请求摘要
    summarize_req = _build_summarize_request(body_data, all_system, all_history, orch_cfg)
    summarize_body = json.dumps(summarize_req, ensure_ascii=False).encode("utf-8")
    log("ORCHESTRATOR", f"Step 1: 摘要请求 ({len(summarize_body)} bytes)")

    try:
        status, resp = upstream_call("POST", "/v1/chat/completions", summarize_body)
    except Exception as e:
        log("ORCHESTRATOR", f"摘要调用失败: {e}")
        return None

    if status != 200:
        log("ORCHESTRATOR", f"摘要 HTTP {status} → 回退截断")
        return None

    summary = "对话中..."
    try:
        resp_data = json.loads(resp)
        summary = resp_data.get("choices", [{}])[0].get("message", {}).get("content", summary)
    except:
        pass

    summary = summary.strip()[:summary_max]
    _summary_cache[fp] = summary
    log("ORCHESTRATOR", f"Step 1 摘要: {summary}")

    # Step 2: 构建紧凑请求
    compact = _build_compact_request(body_data, summary, last_user, orch_cfg)
    return json.dumps(compact, ensure_ascii=False).encode("utf-8")

def route_query(body_data):
    """判断用户问题是否需要对话历史上下文。
    返回 "DEPENDS" / "INDEPENDENT" / None（失败=安全回退，按DEPENDS处理）。
    单条消息直接免检返回 INDEPENDENT。
    """
    msgs = body_data.get("messages", [])
    if len(msgs) <= 2:
        return "INDEPENDENT"

    # 提取最后一条用户消息
    last_user = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            last_user = m.get("content", "")
            break
    if not last_user:
        return "INDEPENDENT"

    query = last_user[:500]  # 只取前500字足够了

    routing_req = {
        "model": _extract_model(body_data),
        "messages": [
            {"role": "user", "content": ROUTING_PROMPT.format(query=query)}
        ],
        "max_tokens": 3,
        "temperature": 0,
    }
    routing_body = json.dumps(routing_req, ensure_ascii=False).encode("utf-8")

    try:
        status, resp = upstream_call("POST", "/v1/chat/completions", routing_body)
        if status == 200:
            result = json.loads(resp)
            answer = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip().upper()
            if "DEPENDS" in answer:
                return "DEPENDS"
            return "INDEPENDENT"
    except:
        pass
    return None  # 失败 → 安全回退，当 DEPENDS 处理


def strip_body(body_bytes):
    """去掉非标准字段 + 智能截断消息，优先保护最后一条消息（当前提问）"""
    try:
        data = json.loads(body_bytes)
        changed = False
        # 顶层字段
        for key in list(data.keys()):
            if key in BODY_STRIP_KEYS:
                del data[key]
                changed = True
        # 消息内的非标准字段
        _msg_keys = tuple(CONFIG["body_strip"]["message_keys"])
        for msg in data.get("messages", []):
            for bad in _msg_keys:
                if bad in msg:
                    del msg[bad]
                    changed = True

        # 截断消息
        msgs = data.get("messages", [])
        if msgs:
            # System prompt: 只保留第一个，截断
            for m in msgs:
                if m.get("role") == "system":
                    content = m.get("content", "")
                    if len(content) > MAX_SYSTEM_CHARS:
                        m["content"] = content[:MAX_SYSTEM_CHARS] + "...[截断]"
                        changed = True
                    break  # 只处理第一个 system

            # 去重 + 只保留最近 N 条 user/assistant（不含 system）
            # ★ 关键：始终保持最后一条消息完整（那是用户当前提问）
            non_system = [m for m in msgs if m.get("role") != "system"]
            if len(non_system) > MAX_MESSAGES - 1:
                non_system = non_system[-(MAX_MESSAGES - 1):]
                changed = True

            # 截断单条消息：从旧到新截，最后一条尽量保留
            for i, m in enumerate(non_system):
                content = m.get("content", "")
                is_last = (i == len(non_system) - 1)
                limit = MAX_MESSAGE_CHARS * 2 if is_last else MAX_MESSAGE_CHARS
                if isinstance(content, str) and len(content) > limit:
                    m["content"] = content[:limit] + "...[截断]"
                    changed = True

            # 重组 messages（system 排第一）
            system_msgs = [m for m in msgs if m.get("role") == "system"]
            data["messages"] = system_msgs + non_system

        if changed:
            result = json.dumps(data, ensure_ascii=False).encode("utf-8")
            # 如果还超过总上限，硬截断 — 优先保留最后一条消息
            if len(result) > MAX_TOTAL_BYTES:
                # 逐步减少历史消息，但最后一条必须保留完整
                non_system_short = non_system
                while len(non_system_short) > 1:
                    non_system_short = non_system_short[-len(non_system_short)+1 if len(non_system_short) > 2 else 2:]
                    # 对非最后的消息大幅截断
                    for m in non_system_short[:-1]:
                        c = m.get("content", "")
                        if isinstance(c, str) and len(c) > 300:
                            m["content"] = c[:300] + "...[硬截断]"
                    # system 也压缩
                    for m in system_msgs:
                        c = m.get("content", "")
                        if isinstance(c, str) and len(c) > 1500:
                            m["content"] = c[:1500] + "...[硬截断]"
                    # 最后一条消息至少保留 HARD_TRUNCATE_TAIL_CHARS
                    if non_system_short:
                        last = non_system_short[-1]
                        c = last.get("content", "")
                        if isinstance(c, str) and len(c) > HARD_TRUNCATE_TAIL_CHARS * 4:
                            last["content"] = c[-HARD_TRUNCATE_TAIL_CHARS*4:]  # 取尾部
                    data["messages"] = system_msgs + non_system_short
                    result = json.dumps(data, ensure_ascii=False).encode("utf-8")
                    if len(result) <= MAX_TOTAL_BYTES:
                        break
            return result
    except:
        pass
    return body_bytes

# ── 脱敏中间件 v9：多模式 ──
def sanitize_body(body_bytes, headers=None, force_mode=None):
    """
    对请求体中的消息内容执行脱敏。支持多模态文件检测。

    模式优先级：
      1. force_mode 参数（SafeProxyHandler 强制）
      2. HTTP Header X-Sanitize-Mode（可选覆盖）
      3. config.yaml sanitizer.mode

    模式说明：
      - auto:   静默脱敏替换
      - off:    跳过
      - detect: 仅检测记录，不替换原文

    返回: (sanitized_bytes, summary_str, mode_used, replacements_list_or_None, file_reports_or_None)
    """
    if not SANITIZER_AVAILABLE:
        return body_bytes, "脱敏引擎未加载", "off", None, None

    cfg = CONFIG.get("sanitizer", {})
    # 运行时覆盖 > 配置文件（force_mode 最高优先级）
    mode = _get_effective_san_mode()

    # force_mode 最高优先级（SafeProxyHandler 使用）
    if force_mode:
        mode = force_mode

    # HTTP Header 覆盖
    if headers and not force_mode:
        header_mode = headers.get("X-Sanitize-Mode", "").lower().strip()
        if header_mode in ("auto", "off", "detect"):
            mode = header_mode
        # confirm 头：detect 模式下带 X-Sanitize-Confirm → 临时升级为 auto
        if mode == "detect" and headers.get("X-Sanitize-Confirm", "").strip().lower() in ("true", "1", "yes"):
            mode = "auto"

    if mode == "off":
        return body_bytes, "脱敏已关闭", mode, None, None

    try:
        data = json.loads(body_bytes)
        rules = load_sanitizer_rules()

        if mode == "detect":
            # 仅检测，不修改原文
            reps, file_reports = detect_only(data, rules)
            log_mapping(reps, rules)
            summary = detect_summary(reps, file_reports)
            if file_reports:
                log("SANITIZER", f"[DETECT] {file_report_summary(file_reports)}")
            log("SANITIZER", f"[DETECT] {summary}")
            return body_bytes, summary, mode, reps, file_reports

        # mode == "auto": 完整脱敏
        data, reps, file_reports = sanitize_messages(data, rules)
        log_mapping(reps, rules)
        if file_reports:
            log("SANITIZER", f"[FILE] {file_report_summary(file_reports)}")
        summary = sanitize_summary(reps, file_reports)
        log("SANITIZER", summary)
        return json.dumps(data, ensure_ascii=False).encode("utf-8"), summary, mode, reps, file_reports

    except Exception as e:
        log("SANITIZER", f"脱敏失败: {e} → 透传原文")
        return body_bytes, f"脱敏异常: {e}", "off", None, None

def nonstream_to_sse(resp_body_bytes):
    """将非流式 chat.completion 响应转为 SSE 流格式"""
    try:
        data = json.loads(resp_body_bytes)
        # 已经是 SSE 流就不转换
        if isinstance(data, str) and data.startswith("data:"):
            return resp_body_bytes
        if data.get("object") == "chat.completion.chunk":
            return resp_body_bytes

        # 非流式 → 转 SSE 流
        data["object"] = "chat.completion.chunk"
        choices = data.get("choices", [])
        for c in choices:
            msg = c.pop("message", {})
            c["delta"] = msg

        sse_line = "data: " + json.dumps(data, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"
        return sse_line.encode("utf-8")
    except:
        return resp_body_bytes

# ── 响应后处理：tool_calls 格式修复 ──
def fix_response_body(resp_body_bytes):
    """
    修复上游返回的常见格式问题（mini 模型高发）。
    返回 (fixed_bytes, fixes_applied_list)
    """
    cfg = CONFIG.get("response_fix", {})
    if not cfg.get("enabled", True):
        return resp_body_bytes, []

    tc_cfg = cfg.get("tool_calls", {})
    fixes = []

    try:
        data = json.loads(resp_body_bytes)
    except:
        return resp_body_bytes, []

    choices = data.get("choices", [])
    for choice in choices:
        msg = choice.get("message", choice.get("delta"))
        if not msg:
            continue
        tool_calls = msg.get("tool_calls", [])
        if not tool_calls and isinstance(msg.get("content"), str):
            # 有时 mini 模型把 tool_call 写到 content 里
            content = msg["content"]
            if "<tool_call>" in content or "function_call" in content:
                log("RESPONSE_FIX", f"检测到 tool_call 混入 content，跳过修复（需上游修复）")

        for tc in tool_calls:
            fn = tc.get("function", {})
            if not fn:
                continue

            # 1. 修复空 arguments
            if tc_cfg.get("fix_empty_arguments") and fn.get("arguments") in (None, "", "null"):
                fn["arguments"] = "{}"
                fixes.append("empty_arguments→{}")

            # 2. 修复缺失 name
            if tc_cfg.get("fix_missing_name") and not fn.get("name"):
                # 尝试从 arguments 推断或设占位符
                fn["name"] = "unknown_tool"
                fixes.append(f"missing_name→unknown_tool({tc.get('id','?')})")

            # 3. 修复非法 JSON arguments
            if tc_cfg.get("fix_invalid_json") and fn.get("arguments"):
                try:
                    json.loads(fn["arguments"])
                except (json.JSONDecodeError, TypeError):
                    # 尝试包装 — 简单修复：如果是裸文本，包成 {"input":"..."}
                    fixed_args = json.dumps({"input": str(fn["arguments"])}, ensure_ascii=False)
                    fn["arguments"] = fixed_args
                    fixes.append(f"invalid_json→wrapped({tc.get('id','?')})")

        # 4. 修复缺失 role
        if tc_cfg.get("fix_missing_role") and tool_calls and not msg.get("role"):
            msg["role"] = "assistant"
            fixes.append("missing_role→assistant")

    if fixes:
        log("RESPONSE_FIX", f"修复了 {len(fixes)} 处: {fixes}")
        return json.dumps(data, ensure_ascii=False).encode("utf-8"), fixes

    return resp_body_bytes, []

# ── Token 用量统计 ──
_token_stats_lock = threading.Lock()
_token_stats = {
    "sessions": {},     # key: session_id → {client, model, requests, total_tokens, prompt_tokens, completion_tokens}
    "requests": [],     # 最近 100 条请求记录
}

def _get_session_id(ip, user_agent):
    """生成会话 ID：IP + User-Agent 前 60 字符的 hash"""
    key = f"{ip}|{user_agent[:60]}"
    return hashlib.md5(key.encode()).hexdigest()[:10]

def track_token_usage(client, model, session_id, usage):
    """记录单次请求的 token 用量"""
    cfg = CONFIG.get("stats", {})
    if not cfg.get("enabled", True):
        return

    with _token_stats_lock:
        # 按会话汇总
        if session_id not in _token_stats["sessions"]:
            _token_stats["sessions"][session_id] = {
                "client": client,
                "model": model,
                "requests": 0,
                "total_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }
        sess = _token_stats["sessions"][session_id]
        sess["requests"] += 1
        sess["total_tokens"] += usage.get("total_tokens", 0)
        sess["prompt_tokens"] += usage.get("prompt_tokens", 0)
        sess["completion_tokens"] += usage.get("completion_tokens", 0)

        # 请求记录
        record = {
            "client": client, "model": model, "session": session_id,
            "usage": usage,
            "time": datetime.datetime.now().isoformat(),
        }
        _token_stats["requests"].append(record)
        if len(_token_stats["requests"]) > 100:
            _token_stats["requests"] = _token_stats["requests"][-100:]

        # 告警
        total = usage.get("total_tokens", 0)
        prompt = usage.get("prompt_tokens", 0)
        warn_total = cfg.get("warn_total_tokens", 8000)
        warn_prompt = cfg.get("warn_prompt_tokens", 6000)
        if total > warn_total or prompt > warn_prompt:
            alert = []
            if total > warn_total:
                alert.append(f"总token={total}>{warn_total}")
            if prompt > warn_prompt:
                alert.append(f"prompt={prompt}>{warn_prompt}")
            log("STATS ALERT", f"⚠ 异常消耗 [{client}/{model}]: {', '.join(alert)}")

def get_stats_summary():
    """生成统计摘要"""
    with _token_stats_lock:
        sessions = _token_stats["sessions"]
        total_requests = sum(s["requests"] for s in sessions.values())
        total_tokens = sum(s["total_tokens"] for s in sessions.values())

        by_client = {}
        by_model = {}
        for sid, s in sessions.items():
            c = s["client"]
            m = s["model"]
            by_client.setdefault(c, {"requests": 0, "tokens": 0})
            by_client[c]["requests"] += s["requests"]
            by_client[c]["tokens"] += s["total_tokens"]
            by_model.setdefault(m, {"requests": 0, "tokens": 0})
            by_model[m]["requests"] += s["requests"]
            by_model[m]["tokens"] += s["total_tokens"]

        return {
            "sessions": len(sessions),
            "total_requests": total_requests,
            "total_tokens": total_tokens,
            "by_client": dict(sorted(by_client.items(), key=lambda x: -x[1]["tokens"])),
            "by_model": dict(sorted(by_model.items(), key=lambda x: -x[1]["tokens"])),
            "sessions_detail": {
                sid: dict(s) for sid, s in sessions.items()
            },
        }

def strip_headers(headers):
    """只保留必要的请求头"""
    keep = {"authorization", "content-type", "accept", "user-agent",
            "content-length", "host"}
    clean = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in keep:
            clean[k] = v
        elif any(kl.startswith(p) for p in HEADER_STRIP_PREFIXES):
            continue  # 丢弃
        else:
            clean[k] = v  # 保留未知头
    return clean

# ── 上游重试配置 ──
_retry_cfg = CONFIG["upstream"].get("retry", {})
RETRY_MAX = _retry_cfg.get("max_retries", 3)
RETRY_BACKOFF_BASE = _retry_cfg.get("backoff_base_sec", 1)
RETRY_BACKOFF_MAX = _retry_cfg.get("backoff_max_sec", 10)
RETRY_ON_ERRORS = _retry_cfg.get("on_errors", [
    "Name or service not known",
    "Connection refused",
    "Connection reset",
    "timed out",
    "Temporary failure",
])

def _should_retry(error_str):
    """判断异常是否应触发重试"""
    if not error_str:
        return False
    s = str(error_str).lower()
    return any(pattern.lower() in s for pattern in RETRY_ON_ERRORS)

import threading

def upstream_call(method, path, body):
    """同步调用 Sub2API，返回 (status, body_bytes) 或异常。
    内置重试逻辑：DNS/连接/TCP 瞬时故障自动重试（指数退避）。
    """
    clean_headers = strip_headers({
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
    })
    if body:
        clean_headers["Content-Length"] = str(len(body))
    clean_headers.pop("Host", None)
    clean_headers.pop("host", None)

    last_error = None
    for attempt in range(RETRY_MAX + 1):
        try:
            log("UPSTREAM REQUEST", f"{method} {path}" + (f" [重试 {attempt}/{RETRY_MAX}]" if attempt > 0 else ""))
            log("UPSTREAM HEADERS", json.dumps(dict(clean_headers), indent=2))

            conn = http.client.HTTPConnection(TARGET_HOST, TARGET_PORT, timeout=UPSTREAM_TIMEOUT)
            conn.request(method, path, body=body, headers=clean_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            conn.close()

            log("UPSTREAM RESPONSE", f"Status: {resp.status}")
            return resp.status, resp_body

        except Exception as e:
            last_error = e
            err_str = str(e)
            retryable = _should_retry(err_str)

            if retryable and attempt < RETRY_MAX:
                wait = min(RETRY_BACKOFF_BASE * (2 ** attempt), RETRY_BACKOFF_MAX)
                log("RETRY", f"⏳ 上游故障 [{err_str[:80]}]，{wait:.1f}s 后第 {attempt+1}/{RETRY_MAX} 次重试...")
                import time as _time
                _time.sleep(wait)
            elif retryable:
                log("RETRY", f"❌ 已达最大重试次数 {RETRY_MAX}，最终错误: {err_str[:120]}")
                raise
            else:
                # 非可重试错误 → 直接抛出
                log("UPSTREAM ERROR", f"不可重试: {err_str[:120]}")
                raise

    # 理论上不会到这里（重试耗尽会 raise），但作为安全兜底
    if last_error:
        raise last_error
    return 502, b"Unknown proxy error"


from http.server import HTTPServer, BaseHTTPRequestHandler

# SSE 心跳间隔（秒）— 告诉客户端连接还活着
HEARTBEAT_INTERVAL = CONFIG["proxy"]["heartbeat_interval_sec"]

class ProxyHandler(BaseHTTPRequestHandler):
    sanitizer_force_mode = None  # 子类覆盖此属性强制脱敏模式

    def _stream_proxy(self, method, body):
        """立即返回 SSE 流头，后台调上游，用 SSE 心跳防止超时"""
        # 客户端信息
        ua = self.headers.get("User-Agent", "")
        client_ip = self.client_address[0] if hasattr(self, "client_address") else "127.0.0.1"
        client, _ = detect_client(ua)
        session_id = _get_session_id(client_ip, ua)

        # 1. 立刻发响应头（不阻塞）
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # 2. 在后台线程调上游
        result = {"done": False, "status": 0, "body": None, "error": None}

        def do_upstream():
            try:
                result["status"], result["body"] = upstream_call(method, self.path, body)
            except Exception as e:
                result["error"] = str(e)
            result["done"] = True

        t = threading.Thread(target=do_upstream, daemon=True)
        t.start()

        # 3. 发心跳，等上游返回
        import time as _time
        heartbeat_seq = 0
        while not result["done"]:
            heartbeat_seq += 1
            hb = json.dumps({"type": "heartbeat", "seq": heartbeat_seq}, ensure_ascii=False)
            try:
                self.wfile.write(f"data: {hb}\n\n".encode("utf-8"))
                self.wfile.flush()
            except:
                break
            t.join(timeout=HEARTBEAT_INTERVAL)

        # 4. 上游返回（成功或失败）
        if result["error"]:
            err_sse = json.dumps({"error": result["error"]}, ensure_ascii=False)
            self.wfile.write(f"data: {err_sse}\n\ndata: [DONE]\n\n".encode("utf-8"))
            self.wfile.flush()
            return

        resp_status = result["status"]
        resp_body = result["body"]

        if resp_status != 200:
            log("UPSTREAM ERROR", f"HTTP {resp_status}: {resp_body[:300]}")
            err_data = {"error": f"Upstream HTTP {resp_status}"}
            try:
                upstream_err = json.loads(resp_body)
                if "error" in upstream_err:
                    err_data["error"] = upstream_err["error"]
            except:
                pass
            err_sse = json.dumps(err_data, ensure_ascii=False)
            self.wfile.write(f"data: {err_sse}\n\ndata: [DONE]\n\n".encode("utf-8"))
            self.wfile.flush()
            return

        # ── 响应后处理：tool_calls 格式修复 ──
        resp_body, fixes = fix_response_body(resp_body)

        # ── Token 用量统计 ──
        try:
            resp_data = json.loads(resp_body)
            usage = resp_data.get("usage", {})
            if usage:
                model = resp_data.get("model", "unknown")
                track_token_usage(client, model, session_id, usage)
        except:
            pass

        try:
            data = json.loads(resp_body)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content:
                content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
            log("UPSTREAM OK", f"{len(resp_body)} bytes → \"{content[:200]}\"")
        except:
            log("RAW RESPONSE", resp_body[:800])

        sse_body = nonstream_to_sse(resp_body)
        self.wfile.write(sse_body)
        self.wfile.flush()

    def _send_magic_response(self, message, is_error=False):
        """直接向客户端发送魔法命令响应（SSE 格式），不调上游"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Proxy-Command", "magic")
        self.end_headers()

        response_data = {
            "id": "proxy-cmd-" + datetime.datetime.now().strftime("%H%M%S"),
            "object": "chat.completion.chunk",
            "created": int(datetime.datetime.now().timestamp()),
            "model": "proxy-runtime-control",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": message
                },
                "finish_reason": "stop"
            }]
        }

        self.wfile.write(("data: " + json.dumps(response_data, ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode("utf-8"))
        self.wfile.flush()

    def _apply_runtime_change(self, action, mode_value):
        """应用运行时配置变更，返回反馈消息"""
        with _runtime_lock:
            old_mode = _get_effective_san_mode()

            if action == 'enable':
                _runtime_state["sanitizer_mode"] = "auto"
                msg = f"✅ 脱敏监测已开启\n当前模式: {MODE_NAME_CN['auto']}"
            elif action == 'disable':
                _runtime_state["sanitizer_mode"] = "off"
                msg = f"⏸ 脱敏监测已关闭\n（可随时发送 /sanitize on 重新开启）"
            elif action == 'set_mode':
                _runtime_state["sanitizer_mode"] = mode_value
                cn = MODE_NAME_CN.get(mode_value, mode_value)
                msg = f"✅ 脱敏模式已切换: {mode_value}\n说明: {cn}"
            elif action == 'status':
                st = _get_runtime_status()
                override = "⚡运行时覆盖" if st["runtime_override"] else "📋配置文件默认"
                msg = (
                    f"📊 脱敏监测状态\n"
                    f"模式: {st['sanitizer_mode']} ({st['sanitizer_mode_cn']})\n"
                    f"来源: {override}\n"
                    f"文件拦截: {'🔒开启' if st['file_block'] else '⚠警告/记录'}\n"
                    f"安全端口: {'已启用 :' + st['safe_port'] if st['safe_port'] else '未配置'}\n"
                    f"\n可用命令:\n"
                    f"  /sanitize on     → 开启自动脱敏\n"
                    f"  /sanitize off    → 关闭脱敏\n"
                    f"  /sanitize detect → 仅检测不替换\n"
                    f"  /sanitize status → 查看状态"
                )
            else:
                msg = f"未知命令: {action}"

            new_mode = _get_effective_san_mode()
            if old_mode != new_mode:
                log("RUNTIME", f"脱敏模式变更: {old_mode} → {new_mode} (命令: {action})")

        return msg

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        # ── 魔法命令检测（在任何处理之前）──
        magic_action = None
        magic_mode = None
        try:
            body_data = json.loads(body)
            msgs = body_data.get("messages", [])
            # 取最后一条 user 消息检查
            last_user_msg = ""
            for m in reversed(msgs):
                if m.get("role") == "user":
                    content = m.get("content", "")
                    if isinstance(content, str):
                        last_user_msg = content
                    break

            magic_action, magic_mode = check_magic_command(last_user_msg)
            if magic_action:
                log("MAGIC", f"检测到魔法命令: {magic_action} mode={magic_mode} ← \"{last_user_msg[:80]}\"")
        except:
            pass

        # 纯魔法命令 → 直接响应，不调上游
        if magic_action:
            msg = self._apply_runtime_change(magic_action, magic_mode)
            # 安全端口提醒
            if self.sanitizer_force_mode and magic_action in ('enable', 'disable', 'set_mode'):
                msg += "\n\n⚠ 当前通过安全端口连接（强制 auto 模式），模式变更仅对普通端口生效"
            self._send_magic_response(msg)
            return

        # ── Admin API: POST /admin/sanitize（程序化运行时控制）──
        if self.path in ("/admin/sanitize", "/admin/sanitize/"):
            dc_cfg = CONFIG.get("dynamic_control", {})
            if dc_cfg.get("admin_api", True):
                admin_key = dc_cfg.get("admin_key", "")
                if admin_key:
                    req_key = self.headers.get("X-Admin-Key", "")
                    if req_key != admin_key:
                        self.send_response(401)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Unauthorized"}, ensure_ascii=False).encode("utf-8"))
                        return
                try:
                    cmd = json.loads(body)
                    mode = cmd.get("mode", "").strip().lower()
                    file_block = cmd.get("file_block")
                    response_data = {"success": True, "changes": []}

                    with _runtime_lock:
                        if mode in ("auto", "off", "detect"):
                            old = _runtime_state["sanitizer_mode"]
                            _runtime_state["sanitizer_mode"] = mode
                            response_data["changes"].append(f"mode: {old} → {mode}")
                        if file_block is not None and isinstance(file_block, bool):
                            old = _runtime_state["file_block"]
                            _runtime_state["file_block"] = file_block
                            response_data["changes"].append(f"file_block: {old} → {file_block}")
                        response_data["status"] = _get_runtime_status()

                    log("ADMIN API", f"POST /admin/sanitize: {response_data['changes']}")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(response_data, ensure_ascii=False, indent=2).encode("utf-8"))
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Invalid JSON body"}, ensure_ascii=False).encode("utf-8"))
                return

        # ── 客户端识别 ──
        ua = self.headers.get("User-Agent", "")
        client_ip = self.client_address[0] if hasattr(self, "client_address") else "127.0.0.1"
        client, client_info = detect_client(ua)
        if client != "unknown":
            log("CLIENT", f"检测: {client} ({client_info.get('match', '?')}) ← {ua[:80]}")

        # 按客户端获取编排参数
        orch_cfg = _get_orch_config(client)

        # 预处理（去非标准字段 + 截断）
        orig_size = len(body) if body else 0
        body = strip_body(body) if body else body
        new_size = len(body) if body else 0

        # ── 脱敏（在路由之前，确保敏感数据不进编排/上游）──
        file_reports = None
        if body:
            body, sanitizer_summary, sanitizer_mode, _, file_reports = sanitize_body(
                body, self.headers, force_mode=self.sanitizer_force_mode)
        else:
            sanitizer_summary = ""

        # ── 文件内容拦截 ──
        if file_reports and self.sanitizer_force_mode:
            # 安全端口强制脱敏模式 — 检查是否应拦截
            fd_cfg = CONFIG.get("file_detect", {})
            if fd_cfg.get("enabled", True) and fd_cfg.get("on_attachment") == "block":
                unsan = sum(fr["report"]["unsanitizable"] for fr in file_reports)
                if unsan > 0:
                    # 列出附件详情
                    details = []
                    for fr in file_reports:
                        for f in fr["report"]["files"]:
                            if f["category"] in ("image", "document"):
                                details.append(f"{f['type']}({f['mime']})")
                    self.send_response(409)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    block_msg = {
                        "error": "包含无法脱敏的文件附件，请求已被安全端口拦截",
                        "details": {
                            "unsanitizable_files": unsan,
                            "file_types": details,
                            "hint": "请使用 sanitize.py --preflight 预处理文件，或通过普通端口 + X-Sanitize-Mode: detect 仅检测"
                        }
                    }
                    self.wfile.write(json.dumps(block_msg, ensure_ascii=False).encode("utf-8"))
                    log("SANITIZER", f"⛔ 文件拦截: {details}")
                    return

        # ── 路由判断 + 两步编排 ──
        final_body = body
        compact_threshold = orch_cfg.get("compact_threshold", COMPACT_THRESHOLD)
        if new_size > compact_threshold:
            try:
                body_data = json.loads(body)
                route = route_query(body_data)
                log("ROUTING", f"路由判断: {route}")

                if route == "INDEPENDENT":
                    log("ROUTING", "独立问题 → 跳过压缩，直接透传")
                else:
                    compact = orchestrate_two_step(body_data, orch_cfg)
                    if compact:
                        final_body = compact
                        log("ORCHESTRATOR", f"两步压缩: {orig_size}→{new_size}→{len(final_body)} bytes")
                    else:
                        log("ORCHESTRATOR", "两步法跳过，使用截断结果")
            except Exception as e:
                log("ORCHESTRATOR", f"编排失败: {e}")

        size_note = ""
        if len(final_body) != orig_size:
            size_note = f" | 最终: {orig_size}→{len(final_body)} bytes"
        log("REQUEST", f"POST {self.path} | {len(final_body)} bytes | 客户端: {client}{size_note}")
        if final_body:
            try:
                log("BODY", json.loads(final_body))
            except:
                log("BODY (raw)", final_body[:3000])

        try:
            self._stream_proxy("POST", final_body)
        except Exception as e:
            log("ERROR", f"流响应失败: {e}")

    def do_GET(self):
        # ── Admin API: 脱敏状态查询 ──
        if self.path == "/admin/sanitize" or self.path == "/admin/sanitize/":
            dc_cfg = CONFIG.get("dynamic_control", {})
            if dc_cfg.get("admin_api", True):
                # 可选 admin_key 验证
                admin_key = dc_cfg.get("admin_key", "")
                if admin_key:
                    req_key = self.headers.get("X-Admin-Key", "")
                    if req_key != admin_key:
                        self.send_response(401)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Unauthorized"}, ensure_ascii=False).encode("utf-8"))
                        return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(_get_runtime_status(), ensure_ascii=False, indent=2).encode("utf-8"))
                return

        # ── Admin API: 脱敏运行时控制（POST 不支持在 GET 中，用 /admin/config 做查询入口）──
        if self.path == "/admin/config" or self.path == "/admin/config/":
            dc_cfg = CONFIG.get("dynamic_control", {})
            if dc_cfg.get("admin_api", True):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                summary = {
                    "runtime": _get_runtime_status(),
                    "config_summary": {
                        "upstream": f"{CONFIG['upstream']['host']}:{CONFIG['upstream']['port']}",
                        "proxy_port": CONFIG["proxy"]["port"],
                        "safe_port": CONFIG.get("sanitizer", {}).get("port_safe", ""),
                        "sanitizer_config_mode": CONFIG.get("sanitizer", {}).get("mode", "off"),
                        "file_detect": CONFIG.get("file_detect", {}).get("on_attachment", "warn"),
                        "dynamic_control": CONFIG.get("dynamic_control", {}).get("enabled", True),
                    }
                }
                self.wfile.write(json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"))
                return

        # ── Token 用量统计 ──
        if self.path == "/stats" or self.path == "/stats/":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            summary = get_stats_summary()
            self.wfile.write(json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Sub2API Proxy OK\n")

    def log_message(self, format, *args):
        pass

# ── 安全端口处理器：强制脱敏 ──
class SafeProxyHandler(ProxyHandler):
    """安全端口处理器 — 所有请求强制脱敏 (mode=auto)"""
    sanitizer_force_mode = "auto"


def _find_available_port(preferred):
    """自动端口检测：若首选端口被占，自动尝试 +1/+2/... 最多10次"""
    import socket
    bind = CONFIG["proxy"]["bind"]
    for offset in range(10):
        port = preferred + offset
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind((bind, port))
            s.close()
            if offset:
                print(f"\x1b[93m⚠ 端口 {preferred} 被占用，已自动切换到 {port}\x1b[0m")
            return port
        except OSError:
            if offset == 0:
                print(f"\x1b[93m⚠ 端口 {preferred} 被占用，正在尝试 {preferred+1}...\x1b[0m")
    print(f"\x1b[91m✗ 错误: 端口 {preferred}~{preferred+9} 均被占用，无法启动代理")
    print(f"  请运行: netstat -ano | findstr :{preferred}")
    print(f"  或修改 config.yaml 中的 proxy.port\x1b[0m")
    sys.exit(1)


if __name__ == "__main__":
    san_cfg = CONFIG.get("sanitizer", {})
    san_mode = san_cfg.get("mode", "off")
    safe_port = san_cfg.get("port_safe", "")

    # 自动端口检测
    global PROXY_PORT
    PROXY_PORT = _find_available_port(PROXY_PORT)

    print("\x1b[95m" + "=" * 60)
    print("  LLM Smart Proxy v10.1 — 配置化通用代理")
    print(f"  监听: http://{CONFIG['proxy']['bind']}:{PROXY_PORT}")
    if safe_port:
        print(f"  安全端口: http://{CONFIG['proxy']['bind']}:{safe_port} (强制脱敏)")
    print(f"  转发: http://{TARGET_HOST}:{TARGET_PORT}")
    print(f"  超时: {UPSTREAM_TIMEOUT}s | 重试: {RETRY_MAX}次 (退避 {RETRY_BACKOFF_BASE}s)")
    print(f"  路由: {'开启' if CONFIG['routing']['enabled'] else '关闭'}")
    print(f"  客户端识别: {'开启' if CONFIG.get('client_detect', {}).get('enabled', True) else '关闭'}")
    print(f"  响应修复: {'开启' if CONFIG.get('response_fix', {}).get('enabled', True) else '关闭'}")
    print(f"  用量统计: {'开启' if CONFIG.get('stats', {}).get('enabled', True) else '关闭'} (GET /stats)")

    mode_desc = {"auto": "自动脱敏", "off": "关闭", "detect": "仅检测不替换"}
    eff_mode = _get_effective_san_mode()
    override_tag = " ⚡运行时覆盖" if _runtime_state["sanitizer_mode"] is not None else ""
    print(f"  脱敏: {mode_desc.get(eff_mode, eff_mode)}{override_tag}")
    dc_cfg = CONFIG.get("dynamic_control", {})
    if dc_cfg.get("enabled", True):
        cmd_enabled = "✅" if dc_cfg.get("magic_command", True) else "❌"
        api_enabled = "✅" if dc_cfg.get("admin_api", True) else "❌"
        print(f"  动态控制: 魔法命令{cmd_enabled} | Admin API{api_enabled}")
        if dc_cfg.get("magic_command", True):
            print(f"    指令: /sanitize on|off|detect|status 或 开启脱敏/关闭脱敏/脱敏状态")
        if dc_cfg.get("admin_api", True):
            print(f"    API: GET/POST /admin/sanitize | GET /admin/config")
    fd_cfg = CONFIG.get("file_detect", {})
    if fd_cfg.get("enabled", True):
        print(f"  文件检测: 开启 | 附件策略: {fd_cfg.get('on_attachment', 'warn')} (image/pdf/word)")
    print("  接入方式: 将模型 URL 改为:")
    print(f"  http://localhost:{PROXY_PORT}/v1/chat/completions")
    if safe_port:
        print(f"  安全模式: http://localhost:{safe_port}/v1/chat/completions")
    print("  或设环境变量: PROXY_API_KEY=xxx (覆盖配置文件密钥)")
    print("  Ctrl+C 停止")
    print("=" * 60 + "\x1b[0m\n")
    sys.stdout.flush()

    # 启动安全端口（可选）
    if safe_port:
        import threading
        safe_port_int = int(safe_port)
        safe_server = HTTPServer((CONFIG["proxy"]["bind"], safe_port_int), SafeProxyHandler)
        t = threading.Thread(target=safe_server.serve_forever, daemon=True)
        t.start()

    server = HTTPServer((CONFIG["proxy"]["bind"], PROXY_PORT), ProxyHandler)
    server.serve_forever()
