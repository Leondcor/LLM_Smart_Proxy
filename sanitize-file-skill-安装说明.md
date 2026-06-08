# sanitize-file Skill — 安装与分享指南

> "帮我把这个文件脱敏" — 一句话触发，自动识别替换敏感信息并生成脱敏副本。

支持 **WorkBuddy / OpenClaw / Hermes / Codex CLI / OpenCode** 五种 AI 编程助手。

---

## 快速安装

### WorkBuddy（本机已安装）

将 `sanitize-file-skill.zip` 解压到 `%USERPROFILE%\.workbuddy\skills\sanitize-file\` 即可。

### 其他工具

| 工具 | 安装路径 | 适配文件 |
|------|---------|---------|
| WorkBuddy | `%USERPROFILE%\.workbuddy\skills\sanitize-file\` | `SKILL.md` |
| OpenClaw | `~/.openclaw/skills/sanitize-file/` | `adapters/openclaw/SKILL.md` |
| Hermes | `~/.hermes/skills/sanitize-file/` | `adapters/hermes/SKILL.md` |
| Codex CLI | `~/.codex/skills/sanitize-file/` | `adapters/codex-cli/sanitize-file.md` |
| OpenCode | `~/.config/opencode/skills/sanitize-file/` | `adapters/opencode/SKILL.md` |

> 所有工具共用同一套核心脚本 (`scripts/sanitize.py` + `sanitizer_engine.py` + `sanitizer_rules.yaml`)，适配器仅是不同格式的指令文件。

---

## 使用方式

安装后在聊天框中直接说话：

| 你说的 | Agent 做的事 |
|---|---|
| `@"XX文件" 帮我脱敏` | 找到文件 → 跑 sanitize.py → 出脱敏副本 + 报告 |
| `把 test.md 脱敏一下` | 同上 |
| `sanitize this file` | 同上 |

**不需要手动输入任何命令。**

---

## 分享给他人

将 `Sanitize-Skill-Standalone\` 文件夹（独立部署包）发送给对方即可。

对方可以选择：
- **手动安装**：按 `安装说明.md` 操作
- **AI 自助安装**：将对应工具的"导入话术"粘贴到目标 Agent 聊天框，Agent 会自主完成部署

导入话术详见独立部署包中的 `安装说明.md` → 第 3 章"导入 Agent 的介绍话术"。

---

## 文件结构

```
sanitize-file-skill.zip  ← 完整打包（≈30KB）
  ├── SKILL.md                  ← WorkBuddy 版本
  ├── scripts/                  ← 脱敏引擎（所有工具共用）
  │   ├── sanitize.py
  │   ├── sanitizer_engine.py
  │   └── sanitizer_rules.yaml
  ├── adapters/                 ← 多工具适配器
  │   ├── openclaw/SKILL.md
  │   ├── hermes/SKILL.md
  │   ├── codex-cli/sanitize-file.md
  │   └── opencode/SKILL.md
  ├── requirements.txt          ← pip install pyyaml
  └── 安装说明.md               ← 完整文档（含 Agent 自助话术）
```

---

## 依赖

- Python 3.10+（`pip install pyyaml`）
- 无需 LLM-Smart-Proxy 完整版
