# 常见问题（FAQ）

## 数据真的不会上传吗？
是。门户只监听 `127.0.0.1`，不提供公网服务，没有遥测/埋点/崩溃上报。看板数据只来自你自己的金蝶服务器，
凭据只在 Flask 进程内存中。详见 [PRIVACY.md](PRIVACY.md)。

## `dsh plugin add` 报缺依赖怎么办？
侧栏注入依赖 `@deepseek-ai/dsh-client-ui-sidebar-right` 与 `@deepseek-ai/dsh-client-locale`。
旧版 DSH 可能不含这两个内置包，请升级 DSH 后再装。见 [README · DSH 版本兼容](README.md)。

## 怎么换端口？
端口 `8985` 写在 `dsh-plugin/client.js`、`dsh-plugin/cordis.patch.yml`、`dsh-plugin/index.js` 三处，需同步修改，
并在 profile 的 patch 行设置 `port`。不建议修改，回环端口冲突时才需要。

## 不装 DSH 能用吗？
能。直接运行 `python -X utf8 scripts/serve_local.py --port 8985 --no-warmup`，浏览器打开
<http://127.0.0.1:8985> 即可。

## 发票云是什么？要开吗？
可选功能。开启后用于报销办理时调用你配置的发票云接口；不配置就不产生相关请求，授权凭据按公司隔离、存于本机。
真实发票云上传链路尚未端到端验证，请谨慎使用。

## 写操作（报销办理）默认开启吗？
默认关闭（`enable_write_ops: false`）。开启后写操作绑定当前登录用户的金蝶凭据，不再回退到其他账号。

## 怎么彻底删除本地数据？
见 [PRIVACY.md · 如何彻底删除](PRIVACY.md)，删除 `.portal-state-dsh/` 与 `~/.kingdee-portal/` 并清除浏览器站点数据即可。
