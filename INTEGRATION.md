# LLM Smart Proxy — 全客户端接入指南 v10.1

> 你的 AI 工具 → Proxy → 上游模型；Proxy 做三件事：**省钱（压缩上下文）、保密（脱敏）、提速（智能路由）**。

---

## 支持客户端

| 客户端 | 类型 | 识别方式 |
|--------|------|----------|
| **WorkBuddy** | 全量级桌面 Agent | UA: `codebuddy` / `workbuddy` |
| **Codex CLI** | 轻量 CLI Agent | UA: `codex` / `opencode` |
| **OpenClaw** | 全量级消息 Agent | UA: `openclaw` |
| **Hermes** | 终端 Agent | UA: `hermes` |
| **Cline** | VS Code 插件 Agent | UA: `cline` |
| **Cursor** | IDE Agent | UA: `cursor` |
| **Continue** | IDE 插件 | UA: `continue` |
| **Curl/通用** | CLI / 脚本 | UA: `curl` / `httpie` |

---

## 通用前提：启动 Proxy

```bash
cd ~/.workbuddy/scripts
python3 proxy.py
```

看到以下输出即成功：

```
============================================================
  LLM Smart Proxy v10.1 — 配置化通用代理
  监听: http://0.0.0.0:4002
  转发: http://ai.qomolo.com:8080
============================================================
```

---

## 各客户端配置

### 1. WorkBuddy

编辑 `models.json`（路径取决于 WorkBuddy 版本），把 URL 改为 Proxy：

```json
{
  "models": [{
    "id": "sonnet-via-proxy",
    "name": "Sonnet (via Proxy)",
    "url": "http://127.0.0.1:4002/v1/chat/completions",
    "apiKey": "Pwd12345westwell"
  }]
}
```

---

### 2. Codex CLI

```bash
# Windows PowerShell
[Environment]::SetEnvironmentVariable("CODEBASE_API_BASE_URL", "http://127.0.0.1:4002/v1", "User")

# macOS / Linux
echo 'export CODEBASE_API_BASE_URL="http://127.0.0.1:4002/v1"' >> ~/.zshrc
```

Codex CLI 会自动把请求路由到 Proxy。重启终端生效。

---

### 3. OpenClaw

#### 方案 A：环境变量（推荐，最简单）

```bash
# Windows PowerShell
[Environment]::SetEnvironmentVariable("OPENAI_BASE_URL", "http://127.0.0.1:4002/v1", "User")
[Environment]::SetEnvironmentVariable("OPENAI_API_KEY", "Pwd12345westwell", "User")

# macOS / Linux
export OPENAI_BASE_URL="http://127.0.0.1:4002/v1"
export OPENAI_API_KEY="Pwd12345westwell"
```

然后在 OpenClaw 中用 `openai/模型名` 即可。

#### 方案 B：自定义 Provider（更精细控制）

编辑 `~/.openclaw/openclaw.json`：

```json5
{
  models: {
    mode: "merge",
    providers: {
      "llm-proxy": {
        baseUrl: "http://127.0.0.1:4002/v1",
        apiKey: "Pwd12345westwell",
        api: "openai-completions",
        models: [
          {
            id: "sonnet-4.6",
            name: "Sonnet 4.6 (Proxy)",
            contextWindow: 200000,
            maxTokens: 8192,
            input: ["text"]
          },
          {
            id: "opus-4.8",
            name: "Opus 4.8 (Proxy)",
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
      model: { primary: "llm-proxy/sonnet-4.6" }
    }
  }
}
```

**注意**：OpenClaw 默认会发自己的 `OpenClaw-Gateway/<version>` User-Agent，Proxy 已支持识别。

验证：

```bash
openclaw doctor
openclaw models list     # 确认 llm-proxy/sonnet-4.6 已注册
```

---

### 4. Hermes

编辑 `~/.hermes/models.json`：

```json
{
  "models": [{
    "id": "sonnet-via-proxy",
    "name": "Sonnet 4.6 (via Proxy)",
    "provider": "openai-compatible",
    "url": "http://127.0.0.1:4002/v1/chat/completions",
    "apiKey": "Pwd12345westwell"
  }]
}
```

---

### 5. Cline (VS Code)

Cline 设置 → **API Provider** 选择 `OpenAI Compatible`：
- **Base URL**: `http://127.0.0.1:4002/v1`
- **API Key**: `Pwd12345westwell`
- **Model ID**: `sonnet-4.6`

---

### 6. Cursor

Cursor Settings → Models → 添加自定义模型：
- **Provider**: `openai`
- **Base URL**: `http://127.0.0.1:4002/v1`
- **API Key**: `Pwd12345westwell`

---

### 7. Curl / 通用脚本（测试用）

```bash
curl http://127.0.0.1:4002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer Pwd12345westwell" \
  -d '{
    "model": "sonnet-4.6",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

---

## 通用功能：魔法命令

在任何已接入客户端的聊天中发送以下指令（即时生效，不消耗上游 Token）：

| 指令 | 功能 |
|------|------|
| `/sanitize on` | 开启脱敏 |
| `/sanitize off` | 关闭脱敏 |
| `/sanitize detect` | 仅检测不替换 |
| `/sanitize status` | 查看状态 |
| `开启脱敏` / `关闭脱敏` / `脱敏状态` | 中文指令 |

---

## 可选：安全端口（双端口模式）

在 `config.yaml` 中设置：

```yaml
sanitizer:
  port_safe: "4003"   # 安全端口，强制脱敏
```

启动 Proxy 后会同时监听两个端口：
- `4002` — 普通端口（可动态切换脱敏）
- `4003` — 安全端口（强制脱敏 + 文件拦截）

---

## 按客户端差异化：Proxy 内部处理

Proxy 识别客户端后，会差异化调整策略（配置在 `config.yaml` 的 `orchestrator.per_client`）：

| 客户端 | 摘要长度 | 压缩阈值 | 说明 |
|--------|----------|----------|------|
| Codex | 200 字符 | 6000 bytes | system prompt 自带上下文，摘要更短 |
| Hermes | 350 字符 | 6000 bytes | structured prompt，更多上下文 |
| OpenClaw | 350 字符 | 6000 bytes | 多Agent场景，保留更多上下文 |
| WorkBuddy | 300 字符 | 8000 bytes | 请求体更大，阈值提高 |
| 其他 | 300 字符 | 6000 bytes | 默认策略 |

---

## 验证 Proxy 是否正常工作

### 查看 Token 统计

浏览器打开：`http://127.0.0.1:4002/stats`

```json
{
  "sessions": 3,
  "total_requests": 127,
  "total_tokens": 45230,
  "by_client": {
    "workbuddy": { "requests": 89, "tokens": 34520 },
    "openclaw":  { "requests": 23, "tokens": 6870 },
    "hermes":    { "requests": 15, "tokens": 3840 }
  }
}
```

### 终端日志

每次请求 Proxy 终端会打印：

```
[10:30:15.234] CLIENT
  检测: openclaw (openclaw) ← OpenClaw-Gateway/1.0 ...
[10:30:15.456] SANITIZER
  [Email] 3处 [Phone] 2处
[10:30:17.890] ORCHESTRATOR
  两步压缩: 24530→8200→1240 bytes
```

按客户端分色显示，便于调试。

---

## 故障排查

| 问题 | 检查项 |
|------|--------|
| 客户端报 `Connection refused` | Proxy 是否启动？端口号是否正确？ |
| Proxy 终端无日志 | 是否指向 `127.0.0.1:4002`（不是 ai.qomolo.com）？ |
| 脱敏未生效 | `/sanitize status` 检查；确认 config.yaml 中 `sanitizer.mode: auto` |
| OpenClaw 不识别 | 确认命名格式是否为 `llm-proxy/model` 或 `openai/model` |
| Token 无节省 | 只有长对话（>6000 bytes）才触发压缩；短对话直接透传 |
