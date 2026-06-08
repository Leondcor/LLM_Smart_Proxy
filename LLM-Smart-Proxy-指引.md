# LLM Smart Proxy v10.1 — 功能简介 & 多客户端部署指引

> 本地 LLM 通用代理中间件。请求到达上游模型前，自动执行智能路由、上下文压缩、脱敏保护、客户端识别、响应修复和用量统计。

---

## 一、功能概览

```
┌─────────────┐      ┌───────────────────────────────────────────┐      ┌──────────┐
│  客户端       │      │           LLM Smart Proxy v10.1           │      │  上游模型  │
│  WorkBuddy   │─────▶│                                           │─────▶│ gpt-5.4  │
│  OpenClaw    │      │ ① 识别客户端  ② 请求清理  ③ 文件检测       │      │ mini /   │
│  Codex CLI   │      │ ④ 脱敏保护  ⑤ 智能路由  ⑥ 差异化压缩      │      │ sonnet   │
│  Hermes      │      │ ⑦ 响应修复  ⑧ SSE 适配  ⑨ Token 统计     │      │ ...      │
│  Cline       │      │                                           │      │          │
└─────────────┘      └───────────────────────────────────────────┘      └──────────┘
```

| 模块 | 功能 | 默认 |
|------|------|------|
| **客户端识别** | 解析 User-Agent，识别 WorkBuddy/OpenClaw/Hermes/Cline/Codex 等 8 种客户端 | ✅ |
| **请求清理** | 删除非标准字段、清理客户端特有 Header | ✅ |
| **文件检测** | 识别请求中的图片/PDF/Word 附件，报告不可脱敏文件 | ✅ |
| **脱敏保护** | auto（静默替换）/ detect（仅检测）/ off（关闭），支持 Header 切换 + 安全端口 + **聊天指令动态切换** | 可配 |
| **动态控制** | 任意客户端聊天中发送 `/sanitize on/off/detect` 实时切换，无需改配置 | ✅ |
| **智能路由** | 判断独立问题 vs 追问上下文，独立跳过压缩直传 | ✅ |
| **差异化压缩** | 超阈值请求两步压缩，按客户端调摘要策略 | ✅ |
| **响应修复** | 修复 mini 模型 tool_calls 格式错误（空参数/缺名称/非法 JSON） | ✅ |
| **SSE 适配** | 强制上游非流式 → proxy 转 SSE 流返回 | ✅ |
| **Token 统计** | 按客户端/模型/会话维度汇总，异常消耗告警，`GET /stats` 查看 | ✅ |
| **日志审计** | 实时请求/响应日志，脱敏映射记录 | ✅ |

### 场景效果

| 场景 | 无 Proxy | 有 Proxy |
|------|----------|----------|
| 闲聊/"你是哪个模型" | 直转上游 | 路由判断 INDEPENDENT → 跳过压缩 → 透传 |
| "继续改刚才的代码" | 上下文超限 502 | 路由判断 DEPENDS → 摘要压缩 → 紧凑请求 ✅ |
| 粘贴投标全文 | 敏感数据原样上传 | 脱敏替换 → 记录映射 → 安全透传 |
| 图片/PDF 附件 | 敏感文件裸传 | 检测告警/拦截 → 阻止泄露 |
| mini 模型 tool_call 格式错误 | 客户端报错 | 自动修复空参数/缺名称/非法 JSON ✅ |
| 48KB 系统提示 | 上游 502 | 截断+摘要压缩 → ~5KB ✅ |

---

## 二、架构与文件清单

```
~/.workbuddy/scripts/         (部署到桌面 LLM-Smart-Proxy\)
├── proxy.py                 # 主代理程序 (v10.1)
├── config.yaml              # 全部配置（上游/路由/脱敏/统计...）
├── sanitizer_rules.yaml     # 脱敏规则（9 类实体 + 白名单）
├── sanitizer_engine.py      # 脱敏引擎（proxy + CLI 共用）
├── sanitize.py              # 消息/文本 CLI 脱敏工具
├── file_sanitize.py         # 文件级脱敏工具（PDF/Word/OCR）
└── models.json              # WorkBuddy 模型配置（3个 proxy 模型）
```

**文件大小：** 7 个文件，合计 ~112KB。

**依赖：**
- Proxy 运行：Python 3.10+、`pyyaml`
- 文件脱敏（按需）：`pdfplumber`、`python-docx`（Word/PDF 文本提取）
- OCR（按需）：系统安装 Tesseract + `pytesseract Pillow`

---

## 三、macOS 部署

### 3.1 环境准备

```bash
# 安装依赖
pip3 install pyyaml

# 创建脚本目录并放入 7 个文件
mkdir -p ~/.workbuddy/scripts
# 将 proxy.py, config.yaml, sanitizer_rules.yaml, sanitizer_engine.py,
#     sanitize.py, file_sanitize.py, models.json
# 复制到 ~/.workbuddy/scripts/
```

### 3.2 修改配置

编辑 `~/.workbuddy/scripts/config.yaml`，核心字段：

```yaml
upstream:
  host: "ai.qomolo.com"      # ← 你的上游地址
  port: 8080
  api_key: "你的密钥"          # ← 或设环境变量 PROXY_API_KEY

proxy:
  port: 4001                  # ← 首选端口（若被占用自动切换）
  bind: "0.0.0.0"

sanitizer:
  mode: "off"                 # auto | off | detect
  port_safe: ""               # 可选安全端口，如 "4002"

dynamic_control:
  enabled: true               # 聊天指令 + Admin API
  magic_command: true         # /sanitize on|off|detect|status
  admin_api: true

file_detect:
  on_attachment: "warn"       # log | warn | block
```

### 3.3 启动

```bash
cd ~/.workbuddy/scripts
python3 proxy.py
```

**自动端口检测**：若配置的端口（默认 4001）被其他程序占用，proxy 会自动尝试下一个端口（4002、4003…），直到找到可用端口。启动成功后会显示实际使用的端口号。

```
⚠ 端口 4001 被占用，正在尝试 4002...
⚠ 端口 4001 被占用，已自动切换到 4002
```

成功输出：

```
============================================================
  LLM Smart Proxy v10.1 — 配置化通用代理
  监听: http://0.0.0.0:4002        ← 可能因自动检测而不同于配置文件
  转发: http://ai.qomolo.com:8080
  路由: 开启 | 客户端识别: 开启 | 响应修复: 开启 | 用量统计: 开启
  脱敏: 关闭
  动态控制: 魔法命令✅ | Admin API✅
    指令: /sanitize on|off|detect|status 或 开启脱敏/关闭脱敏/脱敏状态
    API: GET/POST /admin/sanitize | GET /admin/config
  文件检测: 开启 | 附件策略: warn (image/pdf/word)
  接入方式: http://localhost:4002/v1/chat/completions
  Ctrl+C 停止
============================================================
```

### 3.4 配置各客户端

> **⚠ 端口号**：以下示例使用默认端口 `4001`。若 proxy 启动时因端口冲突自动切换，请使用启动日志中显示的实际端口号替换 `4001`。

#### Hermes

`~/.hermes/models.json`：

```json
{
  "models": [
    {
      "id": "qomolo-sonnet-via-proxy",
      "name": "Sonnet 4.6 (Proxy)",
      "provider": "openai-compatible",
      "url": "http://127.0.0.1:4001/v1/chat/completions",
      "apiKey": "你的密钥",
      "maxInputTokens": 200000,
      "maxOutputTokens": 8192
    }
  ]
}
```

#### Codex CLI

```bash
# 写入 ~/.zshrc 或 ~/.bash_profile
export CODEX_API_BASE_URL="http://127.0.0.1:4001/v1"
export CODEX_API_KEY="你的密钥"
```

或 `~/.codex/config.yaml`：

```yaml
api_base_url: "http://127.0.0.1:4001/v1"
api_key: "你的密钥"
```

#### Cline（VS Code 插件）

1. VS Code → Cline 设置 → API Provider 选 **"OpenAI Compatible"**
2. Base URL：`http://127.0.0.1:4001/v1`
3. API Key：你的密钥
4. Model ID：填上游实际模型名

#### OpenClaw

**方式一：环境变量（最简单）**
```bash
export OPENAI_BASE_URL="http://127.0.0.1:4001/v1"
export OPENAI_API_KEY="你的密钥"
```

**方式二：自定义 Provider**
编辑 `~/.openclaw/openclaw.json`：
```json5
{
  models: {
    mode: "merge",
    providers: {
      "qomolo-proxy": {
        baseUrl: "http://127.0.0.1:4001/v1",
        apiKey: "你的密钥",
        api: "openai-completions",
        models: [
          {
            id: "sonnet-4.6",
            name: "Sonnet 4.6 (via Proxy)",
            contextWindow: 200000,
            maxTokens: 8192,
            input: ["text"]
          }
        ]
      }
    }
  },
  agents: {
    defaults: {
      model: { primary: "qomolo-proxy/sonnet-4.6" }
    }
  }
}
```

### 3.5 设为开机自启（可选）

创建 `~/Library/LaunchAgents/com.llm-proxy.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.llm-proxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3</string>
        <string>/Users/你的用户名/.workbuddy/scripts/proxy.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/你的用户名/.workbuddy/scripts</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/你的用户名/.workbuddy/logs/proxy.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/你的用户名/.workbuddy/logs/proxy_error.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.llm-proxy.plist
```

---

## 四、Windows 部署

### 4.1 环境准备

```powershell
# 安装 Python 3.10+（https://www.python.org/downloads/，勾选 Add to PATH）
pip install pyyaml

# 创建脚本目录
mkdir %USERPROFILE%\.workbuddy\scripts
# 将 7 个文件复制到上述目录
```

### 4.2 启动

创建 `start-proxy.bat` 放在桌面：

```bat
@echo off
title LLM Smart Proxy
cd /d %USERPROFILE%\.workbuddy\scripts
python proxy.py
pause
```

### 4.3 配置各客户端

#### Hermes（Windows）

模型配置路径：`%USERPROFILE%\.hermes\models.json`，内容同 macOS。

#### Codex CLI（Windows）

```powershell
# 永久设置
[Environment]::SetEnvironmentVariable("CODEX_API_BASE_URL", "http://127.0.0.1:4001/v1", "User")
[Environment]::SetEnvironmentVariable("CODEX_API_KEY", "你的密钥", "User")
```

#### Cline（VS Code，Windows）

与 macOS 完全一致。

#### OpenClaw（Windows）

```powershell
# 环境变量方式
[Environment]::SetEnvironmentVariable("OPENAI_BASE_URL", "http://127.0.0.1:4001/v1", "User")
[Environment]::SetEnvironmentVariable("OPENAI_API_KEY", "你的密钥", "User")
```

自定义 Provider 配置内容同 macOS，路径 `%USERPROFILE%\.openclaw\openclaw.json`。

### 4.4 设为开机自启

**任务计划程序**：`Win+R` → `taskschd.msc` → 创建任务 → 触发器：登录时 → 操作：启动 `python.exe proxy.py`，起始于 `%USERPROFILE%\.workbuddy\scripts`

---

## 五、WorkBuddy 配置

### 5.1 模型配置

WorkBuddy 模型配置文件：`~/.codebuddy/models.json`（Windows：`C:\Users\用户名\.codebuddy\models.json`）

```json
{
  "models": [
    {
      "id": "qomolo-sonnet-4-6-proxy",
      "name": "Sonnet 4.6 [Proxy]",
      "vendor": "Anthropic",
      "url": "http://127.0.0.1:4001/v1/chat/completions",
      "apiKey": "Pwd12345westwell",
      "maxInputTokens": 200000,
      "maxOutputTokens": 8192,
      "supportsToolCall": true,
      "supportsImages": false
    },
    {
      "id": "qomolo-opus-4-8-proxy",
      "name": "Opus 4.8 [Proxy]",
      "vendor": "Anthropic",
      "url": "http://127.0.0.1:4001/v1/chat/completions",
      "apiKey": "Pwd12345westwell",
      "maxInputTokens": 200000,
      "maxOutputTokens": 8192,
      "supportsToolCall": true,
      "supportsImages": false
    },
    {
      "id": "qomolo-haiku-4-5-proxy",
      "name": "Haiku 4.5 [Proxy]",
      "vendor": "Anthropic",
      "url": "http://127.0.0.1:4001/v1/chat/completions",
      "apiKey": "Pwd12345westwell",
      "maxInputTokens": 200000,
      "maxOutputTokens": 8192,
      "supportsToolCall": true,
      "supportsImages": false
    }
  ],
  "availableModels": [
    "qomolo-sonnet-4-6-proxy",
    "qomolo-opus-4-8-proxy",
    "qomolo-haiku-4-5-proxy"
  ]
}
```

> 3 个模型全部走 proxy。WorkBuddy 左下角模型选择器显示：Sonnet 4.6 [Proxy] / Opus 4.8 [Proxy] / Haiku 4.5 [Proxy]

---

## 六、脱敏功能详解

### 6.1 三种运行模式

| 模式 | 效果 | 适用场景 |
|------|------|----------|
| `off` | 不检测不替换 | 日常使用 |
| `detect` | 仅检测记录日志，原文不动 | 想先看看有哪些敏感信息 |
| `auto` | 静默替换脱敏 | 处理投标书/用户数据 |

### 6.2 切换方式

| 方式 | 操作 | 适用场景 |
|------|------|----------|
| **聊天指令 ⚡** | 在任意客户端聊天框发送 `/sanitize on` / `off` / `detect` / `status` | **最方便**，无需离开聊天界面 |
| 改配置文件 | `config.yaml` → `sanitizer.mode: "auto"` | 永久生效，重启后保留 |
| HTTP Header | 请求头加 `X-Sanitize-Mode: auto` | 单次请求临时切换 |
| 安全端口 | 连接 `:4002` 替代 `:4001`（需先配置 `port_safe: "4002"`） | 持续强制脱敏 |

**聊天指令示例（中英文均支持）：**

```
/sanitize on        → 开启自动脱敏（替换敏感信息）
/sanitize off       → 关闭脱敏
/sanitize detect    → 仅检测不替换
/sanitize status    → 查看当前状态

开启脱敏             → 同上
关闭脱敏             → 同上
脱敏模式 检测         → 仅检测不替换
脱敏状态             → 查看状态
```

指令发送后，proxy 直接响应确认消息，**不经过上游模型**，零 token 消耗。

### 6.3 文件附件检测

Proxy 自动识别请求中的附件内容：

| 附件类型 | 能脱敏文本？ | 行为 |
|----------|-------------|------|
| 纯文本 / JSON | ✅ 正常脱敏 | text 块中的敏感信息被替换 |
| 图片（png/jpg/webp） | ❌ 无法 OCR | 报告告警 → 日志记录 |
| PDF / Word 文档 | ❌ 二进制 | 报告告警 → 日志记录 |

`config.yaml` 控制：

```yaml
file_detect:
  on_attachment: "warn"   # log=静默 / warn=警告 / block=拦截(返回409)
```

### 6.4 预检 CLI

```bash
# 敏感任务前扫描目录
python sanitize.py --preflight 投标文件/

# 工具列出检出项 → 用户确认是否继续
# y → 继续
# n → 取消
```

### 6.5 独立脱敏工具

```bash
# 管道模式
echo "报价 328 万" | python sanitize.py

# 文件模式
python sanitize.py 投标书.docx -o 脱敏版.txt

# 报告模式（仅看检出项）
python sanitize.py 投标书.txt --report

# 交互模式（逐条确认）
python sanitize.py 投标书.txt --interactive
```

### 6.6 sanitize.py 独立工具详解

`sanitize.py` 是一把**可脱离 proxy 独立运行**的脱敏瑞士军刀。不依赖 proxy，不依赖网络，纯本地。在文件发往 AI 之前先过一遍它。

#### 四种运行模式

| 模式 | 命令 | 说明 |
|------|------|------|
| **管道** | `cat file \| python sanitize.py` | 从 stdin 读取，脱敏后输出到 stdout |
| **文件** | `python sanitize.py 输入.txt -o 输出.txt` | 读文件 → 脱敏 → 写文件 |
| **报告** | `python sanitize.py 文件.txt --report` | 仅扫描检出项，不修改原文 |
| **预检** | `python sanitize.py --preflight 目录/` | 扫描整个目录，人工确认后决定是否继续 |
| **交互** | `python sanitize.py 文件.txt --interactive` | 逐条显示检出内容，用户逐条选择替换/保留 |

#### 输入格式支持

| 格式 | 支持 | 说明 |
|------|------|------|
| `.txt` | ✅ | 纯文本，直接扫描 |
| `.json` | ✅ | 自动解析为 OpenAI API 请求体，提取 messages |
| `.md` | ✅ | Markdown，直接扫描 |
| `.py` / `.js` / `.html` 等 | ✅ | 作为纯文本处理 |
| `.docx` | ⚠️ | 仅提取纯文本（不保留表格结构） |
| `.pdf` | ⚠️ | 依赖 pdfplumber，需单独 `pip install pdfplumber` |

#### 检出示例

```bash
$ python sanitize.py --preflight 投标书/
[PREFLIGHT] 扫描 投标书/ 目录...

⚠ 检出 12 处敏感信息：
  [手机号] ×3
  [身份证号] ×1
  [公司名称] ×4
  [金额] ×3
  [邮箱] ×1

是否继续？(y=脱敏处理 / n=取消 / q=退出): y
```

#### 和 proxy 的关系

```
          ┌─────────────────────────────┐
          │  sanitize.py（预处理层）     │
          │  文件 → 文本提取 → 脱敏     │
          │  用户审核 → 确认             │
          └───────────┬─────────────────┘
                      │ 脱敏后的文本
                      ▼
          ┌─────────────────────────────┐
          │  proxy（传输层）            │
          │  文本脱敏 + 附件拦截        │
          │  路由 + 压缩 + 转发          │
          └─────────────────────────────┘
```

**两者互补，不重复。** sanitize.py 处理消息文本脱敏（纯文本/JSON），file_sanitize.py 处理文件内容提取+脱敏（PDF/Word/图片），proxy 处理传输链路（拦截 base64 附件 + 实时消息脱敏 + 路由压缩）。

### 6.7 file_sanitize.py 文件级脱敏工具

`file_sanitize.py` 是专门处理**物理文件**的脱敏工具。它直接从 PDF、Word、图片中提取文本，然后在本地脱敏——**文件内容不过网**。

#### 核心理念

```
               ┌──────────────────────────────────┐
               │  file_sanitize.py（文件层）       │
               │  PDF→文本  Word→文本  图片→OCR   │
               │  规则脱敏 → 人工审核 → 输出      │
               └──────────────┬───────────────────┘
                              │ 脱敏后的纯文本
                              ▼
               ┌──────────────────────────────────┐
               │  sanitize.py（消息层）            │
               │  文本/JSON 消息脱敏               │
               └──────────────┬───────────────────┘
                              │
                              ▼
               ┌──────────────────────────────────┐
               │  proxy（传输层）                  │
               │  附件拦截 + 路由 + 压缩 + 转发    │
               └──────────────────────────────────┘
```

三层防御：文件提取脱敏 → 消息脱敏 → 传输拦截。

#### 文件格式支持

| 格式 | 提取方式 | 质量 | 依赖 |
|------|---------|------|------|
| `.txt` `.md` `.json` `.yaml` `.py` ... | 直接读取 | 完美 | 无 |
| `.docx` | python-docx 段落+表格提取 | 良好 | `pip install python-docx` |
| `.pdf`（文字型） | pdfplumber 文本+表格提取 | 良好 | `pip install pdfplumber` |
| `.pdf`（扫描件） | OCR（需 Tesseract） | 一般 | `pip install pytesseract Pillow` |
| `.png` `.jpg` `.bmp` | OCR（需 Tesseract） | 取决于图片质量 | `pip install pytesseract Pillow` |

#### 用法

```bash
# 一次性安装所有依赖（推荐）
pip install pdfplumber python-docx pytesseract Pillow

# 检查 OCR 环境
python file_sanitize.py --check-ocr

# 单文件脱敏
python file_sanitize.py 投标书.pdf                    # PDF → 脱敏 → 终端输出
python file_sanitize.py 报价单.docx -o 脱敏版.txt      # Word → 脱敏 → 保存

# 交互式逐条审查
python file_sanitize.py 合同.pdf --interactive

# 图片 OCR 脱敏
python file_sanitize.py 截图.png --ocr

# 扫描件 PDF OCR
python file_sanitize.py 扫描件.pdf --ocr --interactive

# 预检目录
python file_sanitize.py --preflight 投标文件/
```

#### 预检模式交互流程

```bash
$ python file_sanitize.py --preflight 投标文件/

文件预检扫描 — 5 个文件
============================================================

  [1/5] 技术方案.pdf ... ⚠ 8处
  [2/5] 报价单.docx ... ⚠ 3处
  [3/5] 营业执照.png ... 跳过（图片，用 --ocr 开启 OCR）
  [4/5] 投标函.txt ... ⚠ 2处
  [5/5] 资质证明.pdf ... ✓ 清洁

⚠ 共检出 13 处敏感信息

  [公司名称] ×5
    → XX科技有限公司
    → XX建设集团有限公司
  [金额] ×3
    → 328万元
  [手机号] ×2
    → 13812345678
  [项目编号] ×2
  [邮箱] ×1

涉及文件:
  技术方案.pdf [pdf] — 公司名称, 金额, 项目编号 (8处)
  报价单.docx [docx] — 金额, 公司名称 (3处)

发现敏感信息。如何处理? (p=逐文件脱敏 / s=跳过继续 / c=取消): p
```

#### Tesseract OCR 安装指南

```bash
# 检查当前状态
python file_sanitize.py --check-ocr

# macOS
brew install tesseract tesseract-lang
pip install pytesseract Pillow

# Windows
# 1. 下载安装: https://github.com/UB-Mannheim/tesseract/wiki
#    推荐 tesseract-ocr-w64-setup-5.x.x.exe (64位)
# 2. 安装时勾选 Chinese (Simplified)
# 3. 添加 C:\Program Files\Tesseract-OCR 到系统 PATH
# 4. pip install pytesseract Pillow

# Linux (Ubuntu/Debian)
sudo apt install tesseract-ocr tesseract-ocr-chi-sim
pip install pytesseract Pillow

# 验证
tesseract --version
tesseract --list-langs  # 应包含 chi_sim
```

> **OCR 在本地运行，图片不会上传到任何服务器。**

#### 推荐完整工作流

```
Step 1  pip install pdfplumber python-docx pytesseract Pillow   ← 一次性装齐
Step 2  python file_sanitize.py --preflight 投标文件/            ← 预扫，看检出
Step 3  人工逐条核对预检报告                                      ← 人眼防线
Step 4  python file_sanitize.py 合同.pdf --interactive           ← 逐文件脱敏
Step 5  将脱敏版 .txt 粘贴到对话                                  ← 不发原文件
Step 6  proxy 安全端口（4002）block 模式兜底                     ← 拦截 base64
```

**安全保证**：六步覆盖全链路 — file_sanitize 本地提取+脱敏（文件不过网）→ proxy 拦截漏网的 base64 附件 → 上游永远看不到裸数据。

### 6.8 动态运行时控制（v10.1 新增）

Proxy 支持在运行时动态切换脱敏模式，**无需重启、无需改配置文件**。在任意客户端聊天中发送指令即可。

#### 原理

```
┌──────────────────┐
│  WorkBuddy       │  用户输入 "/sanitize on"
│  / Hermes /      │──────────────────────────────┐
│  Codex / Cline   │                              │
└──────────────────┘                              ▼
                                        ┌──────────────────┐
                                        │  LLM Smart Proxy  │
                                        │  ① 拦截魔法命令    │
                                        │  ② 更新运行时状态  │
                                        │  ③ 返回确认消息    │
                                        │  ④ 不调上游模型    │
                                        └──────────────────┘
```

**关键特征：**
- 命令在 proxy 层拦截，**不会**发送到上游模型（零 token 消耗）
- 运行时覆盖优先于 config.yaml，重启后恢复配置文件设定
- 支持中英文指令，所有客户端通用
- 安全端口（:4002）始终强制 auto 模式，指令变更仅对普通端口生效

#### 全部指令

| 英文指令 | 中文指令 | 效果 |
|----------|---------|------|
| `/sanitize on` | `开启脱敏` / `启用脱敏` | 开启自动脱敏（替换敏感信息） |
| `/sanitize off` | `关闭脱敏` / `停用脱敏` | 关闭脱敏 |
| `/sanitize detect` | `脱敏模式 检测` / `脱敏 仅检测` | 仅检测不替换原文 |
| `/sanitize status` | `脱敏状态` / `查看脱敏` | 查看当前运行时状态（含配置来源） |
| `/sanitize mode auto` | `脱敏模式 auto` | 同 on，显式指定模式 |

#### Admin API（程序化控制）

其他脚本/工具可通过 HTTP API 控制 proxy：

```bash
# 查看当前运行时状态
curl http://127.0.0.1:4001/admin/sanitize

# 切换脱敏模式
curl -X POST http://127.0.0.1:4001/admin/sanitize \
  -H "Content-Type: application/json" \
  -d '{"mode": "detect", "file_block": false}'

# 查看完整运行时配置（含上游/端口等摘要）
curl http://127.0.0.1:4001/admin/config
```

响应示例（`GET /admin/sanitize`）：
```json
{
  "sanitizer_mode": "auto",
  "sanitizer_mode_cn": "自动脱敏（替换敏感信息）",
  "file_block": false,
  "runtime_override": true,
  "config_mode": "off",
  "dynamic_control": true
}
```

其中 `runtime_override: true` 表示当前模式来自运行时指令覆盖，而非 config.yaml。

#### config.yaml 配置

```yaml
dynamic_control:
  enabled: true                # 总开关
  magic_command: true          # 聊天指令控制
  admin_api: true              # HTTP API 控制
  admin_key: ""                # API 密钥（留空=无认证，本地使用安全）
  response_inject: true        # 指令执行后返回确认消息
```

#### AI Agent 自动调用指南

其他 AI（通过 WorkBuddy/Codex/Hermes 等工具读取本文档时），可通过以下方式控制脱敏：

1. **最简单**：在对话中发送 `/sanitize on`（一行即可）
2. **通过 API**：POST `http://127.0.0.1:4001/admin/sanitize`，body `{"mode":"auto"}`
3. **查看状态**：GET `http://127.0.0.1:4001/admin/sanitize`
4. **注意事项**：
   - 指令消息的 role 必须是 `"user"`，内容完全匹配指令格式
   - 如果指令嵌入在多行消息中，需放在第一行独立出现
   - 切换后，后续所有请求都受新模式影响（直到下次切换或重启）

---

## 七、验证方法

### 7.1 Proxy 运行状态

```bash
curl http://127.0.0.1:4001/
# 返回 "Sub2API Proxy OK" 即正常
```

### 7.2 路由是否生效

观察终端日志：

```
[17:03:21] CLIENT    检测: workbuddy (codebuddy) ← WorkBuddy/1.0
[17:03:21] ROUTING   路由判断: INDEPENDENT
[17:03:21] ROUTING   独立问题 → 跳过压缩，直接透传
```

### 7.3 Token 统计

```bash
curl http://127.0.0.1:4001/stats | python -m json.tool
```

### 7.4 脱敏是否生效

```bash
echo "深圳西井科技有限公司电话 13812345678" | python sanitize.py
# 输出应替换为占位符
```

### 7.5 动态控制是否生效

在任意客户端聊天框发送：

```
/sanitize status
```

应返回当前脱敏状态。再发送：

```
/sanitize detect
```

应收到确认 `脱敏模式已切换: detect`。

---

## 八、常见问题

| 问题 | 解决 |
|------|------|
| `yaml` 模块找不到 | `pip install pyyaml` |
| Connection refused | Proxy 未启动，检查终端报错 |
| 上游 502（请求太大） | 调低配置中 `compact_threshold_bytes` |
| 脱敏太激进 | 在 `sanitizer_rules.yaml` 的 `whitelist.terms` 加白名单 |
| 客户端提示 404 | URL 末尾需 `/v1/chat/completions` |
| 文件附件未被识别 | 确认请求中附件以 `image_url` 或 `file` 格式传递 |
