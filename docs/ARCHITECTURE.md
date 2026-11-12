# 架构与数据流

本文说明金蝶经营门户 DSH 插件的组成、请求链路，以及数据停留在哪一侧。

## 组件

```
┌─────────────────────────────────────────────────────────────┐
│ DSH 客户端 (DeepSeek Harness, 桌面版)                        │
│                                                             │
│  侧栏 (sidebar iframe)  ──►  本机 Flask 门户 127.0.0.1:8985  │
│       ▲                        │                           │
│       │  cordis.patch.yml      │  HTTP (回环)                │
│       │  注入导航/面板          ▼                           │
│  dsh-plugin/client.js     app.py / store.py / metrics_*.py   │
│  dsh-plugin/index.js (Host 进程管理, 拉起 Python 服务)        │
└─────────────────────────────────────────────────────────────┘
                                          │
                                          │ HTTPS (你的金蝶服务器)
                                          ▼
                                  金蝶云星空 API
```

- **`dsh-plugin/`** — DSH 插件本体。`index.js` 在 Host 进程里拉起本机 Python 服务；
  `client.js` 在侧栏渲染门户入口与面板；`cordis.patch.yml` 声明注入点与 `8985` 端口。
- **本地 Flask 门户**（`app.py` 等）— 只监听 `127.0.0.1`，提供看板页面与取数接口。
- **金蝶云星空 API** — 用户自己配置的服务器；门户以登录者本人凭据直连。

## 数据流

1. 用户在侧栏点击「金蝶门户」→ 插件首次自动启动本机 Python 服务（约 1–3 秒）。
2. 侧栏 iframe 加载 `http://127.0.0.1:8985`。
3. 用户在门户内填写金蝶服务器、账号、密码 → 凭据只存于 Flask 进程**内存**。
4. 看板取数：Flask → 金蝶 API（HTTPS）→ 结果回显并做本机缓存；**不落作者服务器**。
5. 会话结束 / 服务重启 → 内存凭据清空；退出登录 → 该会话后台任务被回收。

## 数据边界

| 数据 | 落点 | 是否出本机 |
|---|---|---|
| 金蝶账号密码 | 仅 Flask 进程内存 | 否 |
| 会话令牌 | 浏览器 `sessionStorage` | 否 |
| 看板业务数据 | 进程内存 + 本机缓存 | 仅与你自己的金蝶服务器通信 |
| 运行库 `state.db` / `state.key` | `.portal-state-dsh/` 或 `~/.kingdee-portal/` | 否 |
| 发票云授权 | 本机、按公司隔离 | 仅调用你配置的发票云接口时 |

详见 [PRIVACY.md](PRIVACY.md) 与 [SECURITY.md](SECURITY.md)。

## 配置加载

`app.py` 优先读取仓库根 `portal_config.json`；不存在时回退到
`portal_config.example.json`。**clone 即可运行**，无需预先配置。
