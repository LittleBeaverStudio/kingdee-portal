# 金蝶经营门户 · DSH 本地插件

[![License](https://img.shields.io/badge/License-No--Resale%201.0-yellow.svg)](LICENSE)
[![DSH Plugin](https://img.shields.io/badge/DSH-plugin%20v1.0.2-blue.svg)](dsh-plugin/)
[![Local First](https://img.shields.io/badge/Local--First-green.svg)](PRIVACY.md)
[![Python](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://www.python.org/)

---

在 DSH（DeepSeek Harness）左侧打开完整的金蝶经营门户，
**用你自己的金蝶云星空账号登录**。门户只监听本机，取数直连你自己的金蝶服务器；
不需要管理员开通公司、不需要公网服务、不内置任何集成账号。

- 插件 Host 在本机拉起 `127.0.0.1:8985` 的 Python 服务；侧栏用 iframe 嵌入完整页面。
- 能取到什么数据完全由金蝶的权限体系决定，门户不换账号、不做兜底。
- 四个看板：费用报销 / 财务健康与风险 / 销售与回款表现 / 现金流；含组织筛选、下钻、趋势与 CSV 导出。
- 报销办理与发票云配置为可选功能，**写操作默认关闭**。

> 本项目由原公网版经营门户改造而来。公网版的管理员控制台、租户开通、公司申请接口在 DSH 本地模式下禁用。

## 安装

前置：Python 3.10+、Node.js + pnpm、已能启动的 DSH 客户端。已在 Windows + DSH 桌面版验证。

```bash
git clone https://github.com/LittleBeaverStudio/kingdee-portal.git
cd kingdee-portal
python -m pip install -r requirements.txt
```

配置（可选）：仓库只带 `portal_config.example.json`。不复制也能跑（程序会自动回退到示例配置）；
需要改端口、会话时长等时再复制：

```bash
cp portal_config.example.json portal_config.json
```

把插件挂到 DSH profile（以 `desktop` 为例，路径换成你自己的 clone 目录）：

```bash
dsh plugin --profile desktop add "link:D:/kingdee-portal/dsh-plugin"
```

等价的手工写法 —— 编辑 `~/.dsh/profiles/desktop/package.json`：

```json
{
  "dependencies": {
    "@littlebeaverstudio/kingdee-portal": "link:D:/kingdee-portal/dsh-plugin"
  },
  "dsh": {
    "profile": {
      "bundles": ["@deepseek-ai/dsh-base", "@littlebeaverstudio/kingdee-portal"]
    }
  }
}
```

`bundles` 数组保留原有条目，只需把 `@littlebeaverstudio/kingdee-portal` 追加进去。然后重启 DSH，
左侧导航出现「金蝶门户」；首次点击会由插件自动启动本地服务（约 1–3 秒）。
端口 8985 同时写在 `dsh-plugin/client.js`、`dsh-plugin/cordis.patch.yml`、`dsh-plugin/index.js`，
改端口需三处同步，并在 profile 的 patch 行里设置 `port`。

**实验性**：仓库根 `package.json` 也支持 `dsh plugin --profile desktop add github:LittleBeaverStudio/kingdee-portal`。
该方式尚未在干净机器上验证；若加载异常，请改用上面的本地 link 方式。

## 不装 DSH 也能用

```bash
python -X utf8 scripts/serve_local.py --port 8985 --no-warmup
```

浏览器打开 <http://127.0.0.1:8985>。填公司名称、金蝶服务器地址、账号、密码即可；
多账套环境在「连接设置」里选择或填写账套，单账套自动识别。

## 配置说明

| 项 | 默认值 | 说明 |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8985` | 只监听回环 |
| `allow_http` / `block_private_network` | `false` / `true` | 服务器地址默认要求 HTTPS，并拒绝私网地址 |
| `allow_local_embedding` | `false` | 仅 DSH 本地模式按嵌入策略放行，非本地模式不继承 |
| `enable_write_ops` | `false` | 报销办理写操作总开关 |
| `warmup_auto` / `debug` | `false` / `false` | 关闭后台预热与调试 |
| `expense_skill_dir` | 空 | 可选；也可用环境变量 `PORTAL_EXPENSE_SKILL_DIR` |

不要把自己的 `portal_config.json`（含服务器地址与账套）、账号密码写进仓库。示例配置里
`server_url`、`acct_name`、`scheme_id` 均为空。

## 🔒 隐私与数据

> 🛡️ **一句话版**：门户只监听 `127.0.0.1`，账号密码只在内存里，不向作者或任何第三方上传经营数据。

| 🧩 数据 | 📍 存在哪里 | ⏳ 保留多久 | 📡 会外发吗 |
|---|---|---|---|
| 🔑 金蝶账号密码 | 🧠 仅 Flask 进程内存 | 服务重启即清空 | ❌ 从不 |
| 🍪 会话令牌 | 🪟 浏览器 `sessionStorage` | 关闭页面会话即失效 | ❌ 从不 |
| 💾 运行库 `state.db` / `state.key` | 🏠 `.portal-state-dsh/` 或 `~/.kingdee-portal/` | 直到你手动删除 | ❌ 从不 |
| 🧾 发票云授权 | 🏠 本机、按公司隔离 | 直到你手动删除 | ⚠️ 仅调用你配置的发票云接口时 |
| 📊 看板业务数据 | 🧠 进程内存 + 本机缓存 | 会话结束 / 缓存到期 | ✅ 只与你自己的金蝶服务器通信 |

**✅ 我们做的**：🔐 凭据只在内存 · 🏠 运行数据留在本机 · 🧹 退出登录清理后台任务 · 🚪 写操作默认关闭
**🚫 我们不做的**：❌ 无遥测 / 埋点 / 崩溃上报 · ❌ 不上传账号密码或业务数据 · ❌ 无第三方统计 SDK · ❌ 不内置集成账号

📄 完整版见 [PRIVACY.md](PRIVACY.md) · 🛠️ 安全边界见 [SECURITY.md](SECURITY.md)

## 发布前检查与测试

```bash
python -X utf8 scripts/check_public.py
python -m unittest discover -s tests -p 'test_*.py' -v
```

`check_public.py` 扫描真实公司名、密钥、运行库、审计日志与开发机绝对路径；
发布白名单之外的内容不应进入公开仓库。浏览器回归脚本在 `tests/ui_*.py`，使用合成数据，
不连接真实金蝶。真实金蝶登录链路已在本机跑通；**他机首次安装、真实发票云上传、全岗位权限尚未验证**。

## 目录

| 路径 | 作用 |
|---|---|
| `dsh-plugin/` | DSH 插件（Host 进程管理 + 侧栏客户端 + patch + 多语言） |
| `app.py` / `store.py` | Flask 服务与本地会话、任务、配置存储 |
| `metrics_*.py` | 四个看板的取数口径 |
| `portal_actions.py` / `vendor/helpers/` | 报销办理、发票云（部分来自 kingdee-data-exporter） |
| `static/` `templates/` | 前端 |
| `scripts/` | 本地服务入口与发布检查 |

## 许可证

[小河狸工作室非转售许可 1.0](LICENSE)。`vendor/helpers/` 中部分实现来自
kingdee-data-exporter，见 [vendor/kingdee-data-exporter-LICENSE.md](vendor/kingdee-data-exporter-LICENSE.md)。
