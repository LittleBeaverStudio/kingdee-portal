---
name: kingdee-portal-dsh
description: >-
  Install, upgrade, or remove the local "Kingdee Portal" DSH plugin
  (LittleBeaverStudio/kingdee-portal). Use when the user wants a Kingdee Cloud
  business dashboard inside DeepSeek Harness / DSH, asks to install or update
  the 金蝶门户 / 金蝶经营门户 plugin, or wants to know where its data is stored.
  The skill runs only local commands, never asks for the user's Kingdee password.
version: 1.0.2
license: LittleBeaverStudio No-Resale License 1.0
author: LittleBeaverStudio
slug: kingdee-portal-dsh
displayName: 金蝶经营门户 · DSH 插件助手
summary: 在 DSH 中安装与管理金蝶经营门户本地插件，用本人金蝶账号查看经营看板，数据不出本机。
tags: [金蝶, DSH, 本地优先, 经营看板, 财务]
homepage: https://github.com/LittleBeaverStudio/kingdee-portal
metadata:
  author: 小河狸工作室
  version: 1.0.2
---

# 🏠 金蝶经营门户 · DSH 插件助手

[![Local First](https://img.shields.io/badge/🏠-本地优先-green.svg)](PRIVACY.md)
[![No Telemetry](https://img.shields.io/badge/📡-无遥测-blue.svg)](PRIVACY.md)
[![DSH](https://img.shields.io/badge/DSH-plugin%20v1.0.2-blue.svg)](https://github.com/LittleBeaverStudio/kingdee-portal)
[![License](https://img.shields.io/badge/License-No--Resale%201.0-yellow.svg)](https://github.com/LittleBeaverStudio/kingdee-portal/blob/main/LICENSE)

> ⚠️ **本 Skill 是安装助手，不是门户本体。** 它指导 Agent 在本机 clone 公开仓库、安装 DSH 插件并做基本检查；
> 门户代码来自 [LittleBeaverStudio/kingdee-portal](https://github.com/LittleBeaverStudio/kingdee-portal)。
> 所有操作都在用户电脑上完成，**不需要、也绝不要索取用户的金蝶账号密码**。

---

## ✅ 什么时候用

| 💬 用户说 | 🎯 做什么 |
|---|---|
| 「装一下金蝶门户 DSH 插件」 | 走 [🚀 安装](#-安装) |
| 「升级 / 更新金蝶门户插件」 | 走 [🔄 升级](#-升级) |
| 「卸掉 / 移除这个插件」 | 走 [🗑️ 卸载](#️-卸载) |
| 「插件白屏 / 打不开 / 端口被占」 | 走 [🩺 故障排查](#️-故障排查) |
| 「它会不会上传我的数据？」 | 直接答 [🔒 隐私与数据](#-隐私与数据) |

---

## 🧰 前置检查

```bash
python --version        # 需要 3.10+
dsh --version           # 需要已安装 DSH CLI
git --version
```

⚠️ 执行任何写入前，**先向用户确认**：安装目标目录、要修改的 DSH profile 名称（常见 `desktop` / `web`）。
不要猜测路径，不要覆盖已有目录。

---

## 🚀 安装

1️⃣ **确认目标目录**。默认建议 `<用户目录>/kingdee-portal`；若已存在，先问用户是升级还是换目录。

2️⃣ **clone 公开仓库**（唯一允许的远程源）：

```bash
git clone https://github.com/LittleBeaverStudio/kingdee-portal.git <目标目录>
cd <目标目录>
python -m pip install -r requirements.txt
```

3️⃣ **挂到 DSH profile**：

```bash
dsh plugin --profile <profile> add "link:<目标目录>/dsh-plugin"
```

4️⃣ **启用 bundle** — 编辑 `~/.dsh/profiles/<profile>/package.json`，在
`dsh.profile.bundles` 数组**追加** `"@local/kingdee-portal"`（保留原有条目）：

```json
{
  "dependencies": { "@local/kingdee-portal": "link:<目标目录>/dsh-plugin" },
  "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "@local/kingdee-portal"] } }
}
```

5️⃣ **重启 DSH** → 左侧导航出现「金蝶门户」→ 首次点击会由插件自动拉起 `127.0.0.1:8985`。

6️⃣ **让用户自己在页面上登录**：填公司名称、金蝶服务器地址、账号、密码。
Agent 不代填、不记录、不写入任何配置文件。

> 💡 不装 DSH 也能单独跑：`python -X utf8 scripts/serve_local.py --port 8985 --no-warmup`。

---

## 🔄 升级

```bash
cd <目标目录>
git pull --ff-only
python -m pip install -r requirements.txt
```

然后重启 DSH。⚠️ `git pull` 前先确认工作区没有本地改动（`git status --short`）；
运行数据在 `.portal-state-dsh/`，**不在 git 跟踪范围**，升级不会覆盖。

---

## 🗑️ 卸载

```bash
dsh plugin --profile <profile> remove @local/kingdee-portal
# 再从 package.json 的 dsh.profile.bundles 里删掉 "@local/kingdee-portal"
```

数据清理见 [PRIVACY.md · 🧹 如何彻底删除我的数据](PRIVACY.md#-如何彻底删除我的数据)。
卸载插件**不会**自动删除运行库，需用户明确确认后再删。

---

## 🔒 隐私与数据

> 🛡️ **一句话**：门户只监听 `127.0.0.1`，账号密码只在内存里，没有遥测/埋点/崩溃上报。

| 🧩 数据 | 📍 位置 | 📤 外发 |
|---|---|---|
| 🔑 金蝶账号密码 | 🧠 进程内存，重启即清空 | ❌ 从不 |
| 🍪 会话令牌 | 🪟 `sessionStorage`，关页面失效 | ❌ 从不 |
| 💾 运行库 | 🏠 `.portal-state-dsh/` | ❌ 从不 |
| 📊 业务数据 | 🧠 内存 + 本机缓存 | ✅ 仅往返用户自己的金蝶服务器 |
| 🧾 发票云授权 | 🏠 本机、按公司隔离 | ⚠️ 仅配置并主动使用时 |

🚫 不收集 · 🚫 不追踪 · 🚫 不共享 · 🚫 不内置集成账号 · 🚫 写操作默认关闭

📄 完整版：[PRIVACY.md](PRIVACY.md) · 在线版：<https://github.com/LittleBeaverStudio/kingdee-portal/blob/main/PRIVACY.md>
🛠️ 安全边界：[SECURITY.md](SECURITY.md)

---

## 🩺 故障排查

| 🚨 现象 | 🔍 原因 | 🛠️ 处理 |
|---|---|---|
| ⬜ 侧栏白屏 | 嵌入策略 / 服务未起 | 确认 DSH 本地模式生效；浏览器直接打开 `http://127.0.0.1:8985` 看是否正常 |
| 🔌 端口被占 | 8985 被别的程序用了 | 三处同步改端口：`dsh-plugin/client.js`、`cordis.patch.yml`、`index.js`，并在 profile patch 行设 `port` |
| 🔐 要求选账套 | 多账套环境 | 在「连接设置」里选择或填写账套；单账套自动识别 |
| 🧱 左侧没有入口 | bundle 未启用 | 检查 `package.json` 的 `dsh.profile.bundles` 是否含 `@local/kingdee-portal`，改完重启 DSH |
| 🐍 启动失败 | 缺依赖 / Python 不对 | `python -m pip install -r requirements.txt`，确认 Python 3.10+ |

---

## ⚠️ 安全须知

- ✅ 只从 `https://github.com/LittleBeaverStudio/kingdee-portal` 获取代码；clone 后可用 `git log -1` 核对提交。
- ❌ 不要向用户索要金蝶账号密码，不要写进任何文件、命令或对话记录。
- ❌ 不要启用公网模式、管理员控制台或 `enable_write_ops`，除非用户明确要求并理解后果。
- 🧱 桌面协议是「按协议放行」，不构成对同机不可信程序的隔离；建议在受信任的单机环境使用。
- 📋 可见数据完全由登录者本人的金蝶权限决定，门户不做权限判断。
