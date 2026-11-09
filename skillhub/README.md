# 🏠 金蝶经营门户 · DSH 插件助手

[![Local First](https://img.shields.io/badge/🏠-本地优先-green.svg)](PRIVACY.md)
[![No Telemetry](https://img.shields.io/badge/📡-无遥测-blue.svg)](PRIVACY.md)
[![DSH](https://img.shields.io/badge/DSH-plugin%20v1.0.2-blue.svg)](https://github.com/LittleBeaverStudio/kingdee-portal)
[![License](https://img.shields.io/badge/License-No--Resale%201.0-yellow.svg)](https://github.com/LittleBeaverStudio/kingdee-portal/blob/main/LICENSE)

> 💡 这是一个 **WorkBuddy Skill**：让 Agent 帮你在 DSH 里安装、升级、卸载
> [金蝶经营门户本地插件](https://github.com/LittleBeaverStudio/kingdee-portal)。
> 插件本体在 GitHub；本 Skill 负责把它装到你电脑上。

---

## 🌟 能做什么

| 🎯 场景 | ✨ 效果 |
|---|---|
| 🆕 首次安装 | 自动 clone → 装依赖 → 挂 DSH profile → 启用 bundle |
| 🔄 升级 | `git pull --ff-only` + 依赖检查，不碰运行数据 |
| 🗑️ 卸载 | 移除 bundle 与依赖，并告知如何彻底删数据 |
| 🩺 排障 | 白屏 / 端口占用 / 账套选择 / 左侧无入口 |
| 🔒 隐私问答 | 直接回答数据放在哪、会不会外发 |

---

## 📥 安装这个 Skill

```bash
skillhub install kingdee-portal-dsh --dir ~/.workbuddy/skills
```

装好后对 Agent 说：**「帮我装一下金蝶门户 DSH 插件」**。

> ⚠️ 安装前 Agent 会先问你目标目录和 DSH profile 名称，不会自作主张改文件。

---

## 🔒 隐私与数据

| 🧩 数据 | 📍 位置 | 📤 外发 |
|---|---|---|
| 🔑 金蝶账号密码 | 🧠 进程内存，重启即清空 | ❌ 从不 |
| 🍪 会话令牌 | 🪟 `sessionStorage` | ❌ 从不 |
| 💾 运行库 | 🏠 `.portal-state-dsh/` | ❌ 从不 |
| 📊 看板数据 | 🧠 内存 + 本机缓存 | ✅ 仅往返你自己的金蝶服务器 |

🚫 无遥测 · 🚫 无埋点 · 🚫 不索取密码 · 🚫 写操作默认关闭

📄 [完整隐私政策](PRIVACY.md) · 🛠️ [安全说明](SECURITY.md)

---

## 📦 相关链接

| 🔗 | 地址 |
|---|---|
| 🏠 插件仓库 | https://github.com/LittleBeaverStudio/kingdee-portal |
| 📖 使用文档 | https://github.com/LittleBeaverStudio/kingdee-portal#readme |
| 🔐 在线隐私政策 | https://github.com/LittleBeaverStudio/kingdee-portal/blob/main/PRIVACY.md |
| ⚖️ 许可证 | [小河狸工作室非转售许可 1.0](https://github.com/LittleBeaverStudio/kingdee-portal/blob/main/LICENSE) |

---

## 🧰 前置条件

- 🐍 Python 3.10+ · 🟢 Node.js + pnpm · 🧩 可运行的 DSH 客户端
- 🪟 已在 Windows + DSH 桌面版验证；他机首次安装尚待验证
