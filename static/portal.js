/* 金蝶经营门户 · 试验版前端（无内联脚本，配合 CSP） */
(function () {
  "use strict";

  var CFG = (function () {
    var el = document.getElementById("cfg");
    return {
      loggedIn: el.dataset.loggedIn === "1",
      localMode: el.dataset.localMode === "1",
      maxBills: el.dataset.maxBills || "0",
      loginName: el.dataset.loginName || "",
      defaultServer: el.dataset.defaultServer || "",
      defaultAcct: el.dataset.defaultAcct || ""
    };
  })();

  var S = { orgs: [], selected: [], trace: [], notes: [], view: "expense",
            loaded: { expense: false, mgmt: false, sales: false, cash: false },
            lastData: {}, expenseTab: 0, pick: "" };   // pick = 图表选中的维度（如 "sal-m:2026-09"）

  /* ★ DSH 内嵌模式（iframe）初始化：cookie 被浏览器当第三方丢弃时，
     用 /api/bootstrap 拿当前 sid + csrf 存进门户自己域的 localStorage，
     之后每个 fetch 经 _portalHeaders() 带上 X-Portal-Sid 头（见 csrf()）。 */
  function refreshEmbeddedBoot() {
    fetch("/api/bootstrap", { credentials: "same-origin" }).then(function (r) {
      return r.ok && r.headers.get("Content-Type").indexOf("application/json") >= 0
        ? r.json() : null;
    }).then(function (j) {
      if (j && j.ok && j.sid && j.csrf) {
        try {
          window.localStorage.setItem("portal_boot", JSON.stringify({ sid: j.sid, csrf: j.csrf }));
          _embeddedBoot = { sid: j.sid, csrf: j.csrf };
        } catch (e) { /* 隐私模式等场景忽略 */ }
      }
    }).catch(function () { /* 网络异常忽略，继续走 cookie 通路 */ });
  }
  if (!CFG.localMode) refreshEmbeddedBoot();

  /* ---------------- 基础工具 ---------------- */
  function $(id) { return document.getElementById(id); }

  // 读取主题变量：图表颜色必须跟着当前配色走，否则浅色下会看不清
  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /* ---------------- 配色主题 ---------------- */
  function paintThemeButtons() {
    var light = themeCurrent() === "light";
    Array.prototype.forEach.call(document.querySelectorAll("[data-theme-toggle]"), function (btn) {
      var use = btn.querySelector("use");
      if (use) use.setAttribute("href", light ? "#i-moon" : "#i-sun");
      btn.setAttribute("aria-label", light ? "切换到深色配色" : "切换到淡色配色");
      btn.setAttribute("title", light ? "切换到深色配色" : "切换到淡色配色");
    });
  }

  function themeCurrent() {
    if (window.portalTheme && window.portalTheme.current) return window.portalTheme.current();
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }

  function applyTheme(value, persist) {
    document.documentElement.setAttribute("data-theme", value);
    if (persist && window.portalTheme) window.portalTheme.save(value);
    paintThemeButtons();
    // 已加载过的看板按新配色重画图表，不重新取数
    rerenderView("expense");
    rerenderView("mgmt");
    rerenderView("sales");
    rerenderView("cash");
  }

  function initTheme() {
    paintThemeButtons();
    Array.prototype.forEach.call(document.querySelectorAll("[data-theme-toggle]"), function (btn) {
      btn.addEventListener("click", function () {
        applyTheme(themeCurrent() === "light" ? "dark" : "light", true);
      });
    });
  }

  /* ★ DSH 内嵌模式（iframe）：跨站 iframe 里第三方 cookie（Lax）会被浏览器丢弃，
     读不到 portal_csrf / portal_sid。此时改用门户自己域下的 localStorage 里
     的凭证（首次由 X-Portal-Bootstrap 响应头带进来）+ 请求头 X-Portal-Sid。
     直接访问（同站）时 cookie 可用，走原逻辑，行为完全不变。 */
  var _embeddedBoot = null;   // 来自 X-Portal-Bootstrap 的 {sid, csrf}
  function _embeddedCreds() {
    if (_embeddedBoot) return _embeddedBoot;
    try {
      var raw = CFG.localMode ? window.sessionStorage.getItem("portal_boot_local") : window.localStorage.getItem("portal_boot");
      if (raw) { _embeddedBoot = JSON.parse(raw); return _embeddedBoot; }
    } catch (e) { /* ignore */ }
    return null;
  }

  function csrf() {
    if (CFG.localMode) {
      var local = _embeddedCreds();
      return local && local.csrf || "";
    }
    var m = document.cookie.match(/(?:^|;\s*)portal_csrf=([^;]+)/);
    if (m) return decodeURIComponent(m[1]);
    var c = _embeddedCreds();
    return c && c.csrf ? c.csrf : "";
  }

  /** 附加会话头（cookie 可用时后端以 cookie 为准；不可用时靠这个头） */
  function _portalHeaders(extra) {
    var h = extra || {};
    var c = _embeddedCreds();
    if (c && c.sid) h["X-Portal-Sid"] = c.sid;
    return h;
  }

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function api(path, body, method) {
    var headers = _portalHeaders({ "Content-Type": "application/json", "X-CSRF-Token": csrf() });
    return fetch(path, {
      method: method || "POST",
      headers: headers,
      body: method === "GET" ? undefined : JSON.stringify(body || {}),
      credentials: "same-origin"
    }).then(function (r) {
      return r.text().then(function (txt) {
        var j;
        try {
          j = JSON.parse(txt);
        } catch (e) {
          return { ok: false, error: {
            stage: "请求", message: "服务返回 HTTP " + r.status + "（非 JSON 响应）",
            hint: (txt || "").replace(/<[^>]+>/g, " ").slice(0, 200) } };
        }
        /* ★ 会话失效自动恢复：cookie/头都失效时重新取 bootstrap 重试一次。
           跨站 iframe 里第三方 cookie 被丢弃，刷新 / 页面即可重新拿凭证。 */
        if (!CFG.localMode && j && j.ok === false && j.error &&
            (j.error.stage === "会话" || j.error.stage === "登录")) {
          refreshEmbeddedBoot();
        }
        return j;
      });
    });
  }

  function num(v, d) {
    if (v === null || v === undefined || v === "") return "—";
    var n = Number(v);
    if (!isFinite(n)) return "—";
    return n.toLocaleString("zh-CN", { minimumFractionDigits: d === undefined ? 2 : d,
      maximumFractionDigits: d === undefined ? 2 : d });
  }
  function money(v) { return v === null || v === undefined || v === "" ? "—" : (isFinite(Number(v)) ? "¥" + num(v) : "—"); }
  function wan(v) {
    if (v === null || v === undefined || v === "") return "—";
    var n = Number(v);
    if (!isFinite(n)) return "—";
    return "¥" + num(n / 10000, 2) + " 万";
  }
  function pct(v) { var n = Number(v); return v !== null && v !== undefined && v !== "" && isFinite(n) ? (n * 100).toFixed(1) + "%" : "—"; }

  /* ---------------- 进度 / 错误 ---------------- */
  var PROG_T0 = 0;
  function prog(show, txt, p) {
    $("prog").hidden = !show;
    if (show) { if (!PROG_T0) PROG_T0 = Date.now(); } else { PROG_T0 = 0; }
    if (txt) $("prog-txt").textContent = txt;
    if (p !== undefined && p !== null) $("prog-bar").style.width = Math.max(2, Math.min(100, p)) + "%";
  }
  /* 取数可能几十秒（要连金蝶），把"已等待多久"显式打出来，避免看着像卡死 */
  function progTick(label) {
    if (!$("prog") || $("prog").hidden) return;
    var sec = Math.max(1, Math.round((Date.now() - PROG_T0) / 1000));
    $("prog-txt").textContent = (label || "正在取数，请耐心等待…") + "（已 " + sec + " 秒）";
  }
  function showErr(err) {
    if (!err) { $("err-box").hidden = true; return; }
    $("err-box").hidden = false;
    $("err-box").innerHTML = errLine(err);   // 精简成一行
    // 会话类错误给一个**明确动作**：重新登录（否则用户只能干看着）
    if (err.stage === "会话" || err.stage === "登录") {
      $("err-box").innerHTML += '<div class="row-actions" style="margin-top:8px">' +
        '<button class="btn pri" id="err-relogin">重新登录</button></div>';
      var rb = $("err-relogin");
      // ⚠️ 不能跳 "/" —— 根地址现在是**管理员控制台**，会把员工踢错地方。
      //    刷新当前地址才能保住 ?c= 公司码。
      if (rb) rb.addEventListener("click", function () { window.location.reload(); });
    }
    // ⚠️ 报错时必须**清掉骨架屏和"正在读取…"**，否则界面上同时出现
    //    "取数失败" 和 "正在读取你的金蝶数据…"，自相矛盾（用户截图就是这样）。
    try {
      Array.prototype.forEach.call(document.querySelectorAll(".view .skeleton-card"), function (e) {
        var g = e.closest ? e.closest(".grid") : null;
        if (g) g.parentNode.removeChild(g); else if (e.parentNode) e.parentNode.removeChild(e);
      });
      Array.prototype.forEach.call(document.querySelectorAll(".view .page-intro p"), function (p) {
        if (p.textContent.indexOf("正在读取") >= 0) p.textContent = "取数失败，见上方提示。";
      });
    } catch (e) { /* 忽略 */ }
  }

  function poll(jobId, onDone) {
    return api("/api/job/" + jobId, {}, "GET").then(function (res) {
      if (!res.ok) { prog(false); showErr(res.error); return; }
      var j = res.job;
      if (j.status === "done") { prog(false); showErr(null); onDone(j.result || {}, true); return; }
      if (j.status === "error") { prog(false); showErr(j.error); return; }
      prog(true, null, j.pct);
      progTick(j.step || "正在取数，请耐心等待…");
      setTimeout(function () { poll(jobId, onDone); }, 1200);
    }).catch(function () {
      prog(false); showErr({ message: "进度读取失败，请检查网络后刷新数据。" });
    });
  }

  /* ---------------- 登录 ---------------- */
  function initLogin() {
    // ★ 路由：**带 ?c= → 公司员工登录页；不带 → 管理员控制台登录页**。
    //   🔴 踩过的坑：原来这两条路包在**同一个 try** 里，管理员控制台一抛异常就被 catch 吞掉，
    //   代码继续往下跑 → 页面被渲染成"单个公司的界面"（用户看到的就是这个）。
    //   现在：分开 try，且管理员控制台失败时**显示可见报错**，绝不悄悄换成另一个页面。
    var tc = "";
    try { tc = (new URLSearchParams(window.location.search)).get("c") || ""; } catch (e) { tc = ""; }
    if (CFG.localMode) {
      // 本地直连永远不解释公网公司码，避免旧链接切回租户登录流程。
      tc = "";
      window.__localLogin = true;
    }
    if (!tc && !CFG.localMode) {
      try {
        enterAdminLogin();
      } catch (e) {
        $("screen-login").hidden = true;
        $("screen-admin-login").hidden = true;
        $("screen-app").hidden = false;
        var bad = $("v-adm");
        if (bad) {
          bad.innerHTML = '<div class="banner err">管理员控制台加载失败：' +
            esc(String((e && e.message) || e).slice(0, 300)) + "</div>";
        }
      }
      return;
    }
    $("screen-login").hidden = false;
    window.__tenantCode = tc.trim().toLowerCase();
    try {
      // 连接设置（服务器 + 账套）整块不显示：服务器来自本公司配置、账套由服务端自动解析。
      // ⚠️ 「连接设置」外层是 <details>，要连 <summary> 一起收起。
      var sf = $("srv-fields");
      var det = (sf && sf.closest) ? sf.closest("details") : null;
      if (!window.__localLogin && window.__tenantCode) {
        if (det) det.hidden = true; else if (sf) sf.hidden = true;
      }
      if (window.__localLogin) {
        $("local-company-field").hidden = false;
        $("local-server-field").hidden = false;
        $("f-company").required = true;
        $("f-local-server").required = true;
        if (det) { det.hidden = true; det.open = false; }
        var legacyServer = $("f-server");
        if (legacyServer && legacyServer.closest(".field")) legacyServer.closest(".field").querySelector("label").hidden = true;
        if (legacyServer) { legacyServer.hidden = true; legacyServer.removeAttribute("type"); }
      }
      var sub = $("login-sub");
      if (sub) sub.hidden = true;
    } catch (e) { /* 忽略 */ }

    // 只记连接信息；完整配置默认收起，未配置时自动展开。
    try {
      if (!$("f-server").value) $("f-server").value = window.localStorage.getItem("portal_server") || "";
      if (CFG.localMode) {
        $("f-company").value = window.localStorage.getItem("portal_company") || "";
        $("f-local-server").value = window.localStorage.getItem("portal_server") || "";
      }
      if (!CFG.localMode && !$("f-acct").value) $("f-acct").value = window.localStorage.getItem("portal_acct") || "";
      if (!$("f-user").value) $("f-user").value = window.localStorage.getItem("portal_user") || "";
    } catch (e) { /* 隐私模式下忽略 */ }
    // ★ 2026-10-19：服务器/账套不再由配置文件预置（否则真实账套会渲染进公网 HTML），
    //   改为按 ?c=公司码 从租户记录带出——知道链接的员工自动填好，陌生人打开是空的。
    if (!CFG.localMode) applyTenantInfo();
    var settings = $("connection-settings"), summary = $("srv-summary"), toggle = $("btn-password");
    function connectionStatus() {
      var ready = !!((window.__localLogin ? $("f-local-server").value.trim() : $("f-server").value.trim()) && $("f-acct").value.trim());
      if (summary) {
        summary.hidden = false;
        summary.textContent = ready ? "已配置" : "待配置";
        summary.style.color = ready ? "var(--green)" : "var(--amber)";
      }
      if (ready && $("login-msg").textContent === "请先设置服务器地址与账套。") $("login-msg").hidden = true;
      return ready;
    }
    // 页面版本不一致时（例如服务端缓存了旧页面），仍保证登录按钮可用
    if (settings) {
      settings.open = !CFG.localMode && !connectionStatus();
      ["f-server", "f-acct"].forEach(function (id) { $(id).addEventListener("input", connectionStatus); });
    }
    if (window.__localLogin) {
      $("f-local-server").addEventListener("input", function () {
        $("f-acct").value = "";
        $("acct-hint").textContent = "";
        connectionStatus();
      });
    }
    if (toggle) {
      toggle.addEventListener("click", function () {
        var visible = $("f-pass").type === "password";
        $("f-pass").type = visible ? "text" : "password";
        this.setAttribute("aria-pressed", String(visible));
        this.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
      });
    }

    $("btn-acct").addEventListener("click", function () {
      var b = this; b.disabled = true; $("acct-hint").textContent = "正在读取账套列表…";
      var requestedServer = window.__localLogin ? $("f-local-server").value.trim() : $("f-server").value.trim();
      api("/api/datacenters", { server_url: requestedServer }).then(function (r) {
        b.disabled = false;
        if (!r.ok) { $("acct-hint").innerHTML = '<span style="color:var(--red)">' +
          esc(r.error.message) + (r.error.hint ? " · " + esc(r.error.hint) : "") + "</span>"; return; }
        if (window.__localLogin && $("f-local-server").value.trim() !== requestedServer) {
          $("acct-hint").textContent = "服务器地址已改变，请重新获取账套。"; return;
        }
        var list = r.datacenters || [];
        if (!list.length) { $("acct-hint").textContent = "该服务器未返回账套列表"; return; }
        $("acct-hint").innerHTML = "账套：" + list.map(function (c) {
          return '<a href="#" data-acct="' + esc(c.name) + '" style="color:var(--brand)">' + esc(c.name) + "</a>";
        }).join("、");
        Array.prototype.forEach.call($("acct-hint").querySelectorAll("a[data-acct]"), function (a) {
          a.addEventListener("click", function (e) {
            e.preventDefault(); $("f-acct").value = a.dataset.acct;
            $("acct-hint").textContent = "已选择：" + a.dataset.acct;
            connectionStatus();
          });
        });
      }).catch(function () {
        b.disabled = false; $("acct-hint").textContent = "账套读取失败，请检查网络后重试。";
      });
    });

    $("login-form").addEventListener("submit", function (e) {
      e.preventDefault();
      var btn = $("btn-login"), m = $("login-msg"); m.hidden = true;
      // ★ 2026-10-19：从公司专属链接进来时，账套可以留空 —— 服务端会按
      //   「服务器只有一个账套就自动用它」来解析（实测多数客户就是单账套）。
      //   只有不带公司码的独立使用时，才要求手填服务器 + 账套。
      var fromTenant = !!window.__tenantCode;
       if (window.__localLogin) {
         var company = $("f-company").value.trim();
         var localServer = $("f-local-server").value.trim();
         if (!company) { m.hidden = false; m.textContent = "请填写公司名称。"; $("f-company").focus(); return; }
         if (!localServer) { m.hidden = false; m.textContent = "请填写服务器地址。"; $("f-local-server").focus(); return; }
         $("f-server").value = localServer;
       }
      if (!window.__localLogin && !fromTenant && settings && !connectionStatus()) {
        settings.open = true;
        m.hidden = false; m.textContent = "请先设置服务器地址与账套。";
        (!$("f-server").value.trim() ? $("f-server") : $("f-acct")).focus();
        return;
      }
      // ⚠️ 有公司码时不校验 server：后端 `api_login` 会**用租户记录里的服务器覆盖**，
      //   前端这个隐藏字段只是异步填的兜底——在这里拦会误报"未配置"（点快了就中招）。
      btn.disabled = true; btn.textContent = "正在登录…";
      api("/api/login", {
        code: window.__tenantCode || "",
         company: window.__localLogin ? $("f-company").value.trim() : "",
        server_url: $("f-server").value, acct: $("f-acct").value,
        username: $("f-user").value, password: $("f-pass").value
      }).then(function (r) {
        $("f-pass").value = "";
        btn.disabled = false; btn.textContent = "登录工作台";
        $("f-pass").type = "password";
        if (toggle) {
          toggle.setAttribute("aria-pressed", "false");
          toggle.setAttribute("aria-label", "显示密码");
        }
        if (!r.ok) {
          // 单账套直接登录；只有服务器确实需要指定账套时才显示选择入口。
          if (CFG.localMode && r.error && r.error.stage === "账套" && settings) {
            settings.hidden = false;
            settings.open = true;
          }
          m.hidden = false;
          m.innerHTML = "<b>" + esc(r.error.message) + "</b>" +
            (r.error.hint ? "<br>" + esc(r.error.hint) : "");
          return;
        }
        if (CFG.localMode && r.sid) {
          _embeddedBoot.sid = r.sid;
          try { sessionStorage.setItem("portal_boot_local", JSON.stringify(_embeddedBoot)); } catch (e) {}
        }
        $("screen-login").hidden = true;
        try {
          if (CFG.localMode) window.localStorage.setItem("portal_company", $("f-company").value.trim());
          window.localStorage.setItem("portal_server", $("f-server").value.trim());
          window.localStorage.setItem("portal_acct", $("f-acct").value.trim());
          window.localStorage.setItem("portal_user", $("f-user").value.trim());
        } catch (e) { /* 隐私模式下忽略 */ }
        bootApp(r.login, r.org_job);
      }).catch(function () {
        $("f-pass").value = "";
        btn.disabled = false; btn.textContent = "登录工作台";
        m.hidden = false; m.textContent = "连接失败，请检查网络后重试。";
      });
    });
  }

  /* ---------------- 管理员：登录屏（根地址） ----------------
     ★ 直接复用公司登录页的整套模板（login-wrap / login-layout / login-card 同一套类），
       只换文案和表单字段 —— 用户 2026-10-14：「新的管理员登录页，并没有复制单个公司的界面设计」。
       认证通过后才进控制台；控制台有自己的侧栏/顶栏/分区，不复用公司工作台。 */
  function enterAdminLogin() {
    $("screen-login").hidden = true;
    var scr = $("screen-admin-login");
    scr.hidden = false;
    var form = $("admin-login-form"), pw = $("f-admin-pw"),
        msg = $("admin-login-msg"), btn = $("btn-admin-login"),
        toggle = $("btn-admin-password");
    if (toggle) {
      toggle.addEventListener("click", function () {
        var visible = pw.type === "password";
        pw.type = visible ? "text" : "password";
        this.setAttribute("aria-pressed", String(visible));
        this.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
      });
    }
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var v = pw.value;
      msg.hidden = true;
      if (!v) { msg.hidden = false; msg.textContent = "请输入管理员口令。"; pw.focus(); return; }
      btn.disabled = true; btn.textContent = "正在验证…";
      T.pw = v;
      fetch("/api/admin/tenants", { headers: { "X-Admin-Token": v } })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          btn.disabled = false; btn.textContent = "进入控制台";
          pw.value = ""; pw.type = "password";
          if (toggle) {
            toggle.setAttribute("aria-pressed", "false");
            toggle.setAttribute("aria-label", "显示密码");
          }
          if (!j.ok) {
            msg.hidden = false;
            msg.innerHTML = "<b>" + esc((j.error && j.error.message) || "口令不对") + "</b>" +
              ((j.error && j.error.hint) ? "<br>" + esc(j.error.hint) : "");
            return;
          }
          T.tenants = j.tenants || [];
          enterAdminConsole();
        })
        .catch(function () {
          btn.disabled = false; btn.textContent = "进入控制台";
          msg.hidden = false; msg.textContent = "连接失败，请检查网络后重试。";
        });
    });
    try { pw.focus(); } catch (e) { /* 忽略 */ }
    // ★ 2026-10-19：口令不再预置在配置文件里 —— 全新部署时先「首次设置」。
    bindAdminSetup(form, $("admin-setup-form"));
  }

  function bindAdminSetup(loginForm, setupForm) {
    if (!setupForm) return;
    var msg = $("admin-setup-msg"), btn = $("btn-admin-setup");
    fetch("/api/admin/need-setup").then(function (r) { return r.json(); }).then(function (j) {
      if (j && j.need_setup) {
        loginForm.hidden = true;
        setupForm.hidden = false;
        try { $("f-setup-pw").focus(); } catch (e) { /* 忽略 */ }
      }
    }).catch(function () { /* 查询失败就按正常登录处理 */ });
    setupForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var a = ($("f-setup-pw").value || ""), b2 = ($("f-setup-pw2").value || "");
      msg.hidden = true;
      if (a.length < 8) { msg.hidden = false; msg.textContent = "口令至少 8 位。"; return; }
      if (a !== b2) { msg.hidden = false; msg.textContent = "两次输入不一致。"; return; }
      btn.disabled = true; btn.textContent = "正在设置…";
      fetch("/api/admin/setup", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: a })
      }).then(function (r) { return r.json(); }).then(function (j) {
        btn.disabled = false; btn.textContent = "设置口令并进入";
        if (!j.ok) {
          msg.hidden = false;
          msg.textContent = (j.error && (j.error.message || j.error)) || "设置失败";
          return;
        }
        // 设置完直接用这个口令进控制台
        setupForm.hidden = true;
        loginForm.hidden = false;
        $("f-admin-pw").value = a;
        loginForm.dispatchEvent(new Event("submit", { cancelable: true }));
      }).catch(function () {
        btn.disabled = false; btn.textContent = "设置口令并进入";
        msg.hidden = false; msg.textContent = "连接失败，请重试。";
      });
    });
  }

  /* ---------------- 自助申请开通（/?apply=1） ---------------- */
  var AP = { code: "", result: null };

  function showApply() {
    ["screen-login", "screen-admin-login", "screen-app"].forEach(function (id) {
      var e = $(id); if (e) e.hidden = true;
    });
    $("screen-apply").hidden = false;
    var form = $("apply-form"), msg = $("apply-msg"), btn = $("btn-apply");
    if (!form) return;
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      msg.hidden = true;
      var body = {
        company: $("ap-company").value.trim(),
        server: $("ap-server").value.trim(),
        phone: $("ap-phone").value.trim(),
        wechat: $("ap-wechat").value.trim(),
        note: $("ap-note").value.trim()
      };
      if (!body.company) { msg.hidden = false; msg.textContent = "请填公司名称"; return; }
      if (!body.server) { msg.hidden = false; msg.textContent = "请填金蝶服务器地址"; return; }
      if (!body.wechat) { msg.hidden = false; msg.textContent = "请填微信号（开通通知发到这里）"; return; }
      if (body.phone && !/^1[3-9]\d{9}$/.test(body.phone)) {
        msg.hidden = false; msg.textContent = "手机号格式不对（不填也可以）"; return;
      }
      btn.disabled = true; btn.textContent = "正在验证服务器…";
      api("/api/apply", body).then(function (r) {
        btn.disabled = false; btn.textContent = "提交申请";
        if (!r.ok) {
          msg.hidden = false;
          msg.innerHTML = "<b>" + esc((r.error && r.error.message) || "提交失败") + "</b>" +
            ((r.error && r.error.hint) ? "<br>" + esc(r.error.hint) : "");
          return;
        }
        r.wechat = body.wechat;
        apShowResult(r);
      }).catch(function () {
        btn.disabled = false; btn.textContent = "提交申请";
        msg.hidden = false; msg.textContent = "连接失败，请稍后重试。";
      });
    });
  }

  function apShowResult(r) {
    AP.wechat = r.wechat || "";
    $("apply-form").hidden = true;
    var box = $("apply-result");
    box.hidden = false;
    box.innerHTML =
      '<div class="banner info">' + (r.dup ? "这个微信号之前提交过，不用重复提交。" : "✅ 申请已提交。") +
      "</div>" +
      '<div class="ap-k">公司</div><b>' + esc(r.company || "") + "</b>" +
      '<div class="ap-k">接下来</div>' +
      '<div style="font-size:15px;line-height:2;margin-top:4px">' +
      "审核通过后，专属链接会发到你的微信。</div>" +
      '<div class="row-actions" style="margin-top:14px">' +
      '<button class="btn" id="ap-check">查看审核进度</button></div>' +
      '<div id="ap-state" class="hint" style="margin-top:10px"></div>';
    var ck = $("ap-check");
    if (ck) ck.addEventListener("click", function () {
      api("/api/apply/status?wechat=" + encodeURIComponent(AP.wechat), null, "GET").then(function (j) {
        var el = $("ap-state");
        if (!el) return;
        if (!j.ok) { el.textContent = "没查到这条申请，可能填的微信号与提交时不一致。"; return; }
        var map = { pending: "待审核", approved: "已开通（链接已发你微信）", rejected: "未通过" };
        el.innerHTML = "当前状态：<b>" + esc(map[j.status] || j.status) + "</b>" +
          (j.status === "rejected" && j.reason ? "<br>原因：" + esc(j.reason) : "");
      });
    });
  }

  /* 按公司码带出登录页的服务器与账套（不再从配置文件预置真实账套） */
  function applyTenantInfo() {
    var tc = "";
    try { tc = (new URLSearchParams(window.location.search)).get("c") || ""; } catch (e) { tc = ""; }
    if (!tc) return;
    fetch("/api/tenant-info?c=" + encodeURIComponent(tc)).then(function (r) { return r.json(); })
      .then(function (j) {
        var t = (j || {}).tenant;
        if (!t) return;
        // 员工页的连接字段是隐藏的，这里填值只是兜底；
        // 真正生效的是服务端：登录时**以租户记录为准**，账套为空会自动解析。
        if (t.server && !$("f-server").value) $("f-server").value = t.server;
        if (t.acct && !$("f-acct").value) $("f-acct").value = t.acct;
      }).catch(function () { /* 取不到就让用户自己填 */ });
  }

  /* ---------------- 管理员：控制台（认证后） ---------------- */
  function enterAdminConsole() {
    $("screen-admin-login").hidden = true;
    $("screen-app").hidden = false;
    // 侧栏：品牌换成"平台运营后台"，导航换成三个分区 + 返回登录
    var brand = document.querySelector(".side .brand");
    if (brand) {
      brand.innerHTML = '<span class="brand-mark"><svg class="icon"><use href="#i-grid"/></svg></span>' +
        '<div class="brand-copy">平台运营后台<small>多公司 SaaS 管理</small></div>';
      brand.style.display = "";
    }
    Array.prototype.forEach.call(
      document.querySelectorAll(".side .grp, .side .side-status"),
      function (e) { e.style.display = "none"; });
    $("nav").innerHTML =
      '<a href="#adm-companies" data-adm="companies" class="on"><svg class="icon"><use href="#i-org"/></svg><span class="nav-label">公司管理</span><svg class="icon nav-arrow"><use href="#i-arrow"/></svg></a>' +
      '<a href="#adm-stats" data-adm="stats"><svg class="icon"><use href="#i-chart"/></svg><span class="nav-label">运营统计</span><svg class="icon nav-arrow"><use href="#i-arrow"/></svg></a>' +
      '<a href="#adm-security" data-adm="security"><svg class="icon"><use href="#i-lock"/></svg><span class="nav-label">安全与凭据</span><svg class="icon nav-arrow"><use href="#i-arrow"/></svg></a>';
    Array.prototype.forEach.call($("nav").querySelectorAll("a[data-adm]"), function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        if (a.dataset.adm === "back") { window.location.href = "/"; return; }
        admSection(a.dataset.adm);
      });
    });
    // 顶栏：只留标题；组织/期间/刷新对后台是噪音
    var bar = document.querySelector(".bar");
    if (bar) bar.hidden = false;
    ["org-label", "btn-orgs", "period-label", "sel-year", "sel-month", "btn-refresh", "btn-reset-view"]
      .forEach(function (id) { var el = $(id); if (el) el.hidden = true; });
    ["expense", "mgmt", "apply", "bills", "pzcfg", "orgs", "source", "adm"].forEach(function (k) {
      var el = $("v-" + k); if (el) el.classList.toggle("on", k === "adm");
    });
    $("ttl").textContent = "管理员控制台";
    if ($("breadcrumb")) $("breadcrumb").textContent = "平台运营";
    var me = $("me-user"); if (me) me.textContent = "管理员";
    var meta = $("me-meta"); if (meta) meta.textContent = "平台运营后台";
    var av = $("user-avatar"); if (av) av.textContent = "管";
    admSection("companies");
  }

  /* ---------------- 组织 ---------------- */
  function orgLabel() {
    if (!S.selected.length) return "组织：未选择";
    if (S.orgs.length && S.selected.length === S.orgs.length) return "组织：全部（" + S.orgs.length + " 个）";
    return "组织：已选 " + S.selected.length + " / " + S.orgs.length;
  }

  function renderOrgChips() {
    if (!S.orgs.length) {
      $("org-chips").innerHTML = '<span class="muted">未发现组织</span>';
      return;
    }
    $("org-chips").innerHTML =
      '<button type="button" class="chip' + (S.selected.length === S.orgs.length ? " on" : "") +
      '" aria-pressed="' + String(S.selected.length === S.orgs.length) + '" id="chip-all">全选</button>' +
      S.orgs.map(function (o) {
        var on = S.selected.indexOf(o.number) >= 0;
        return '<button type="button" class="chip' + (on ? " on" : "") + '" aria-pressed="' + String(on) + '" data-no="' + esc(o.number) + '">' +
          esc(o.number) + " " + esc(o.name || "（无名称）") + "</button>";
      }).join("");
    $("chip-all").addEventListener("click", function () {
      S.selected = S.selected.length === S.orgs.length ? [] : S.orgs.map(function (o) { return o.number; });
      afterOrgChange();
    });
    Array.prototype.forEach.call($("org-chips").querySelectorAll("[data-no]"), function (c) {
      c.addEventListener("click", function () {
        var no = c.dataset.no, i = S.selected.indexOf(no);
        if (i >= 0) S.selected.splice(i, 1); else S.selected.push(no);
        afterOrgChange();
      });
    });
    $("org-label").textContent = orgLabel();
  }

  function afterOrgChange(reset) {
    if (reset !== false) window.PortalAnalytics.reset();
    renderOrgChips();
    ["expense", "mgmt", "sales", "cash"].forEach(rerenderView);
    $("org-banner").hidden = true;
  }
  function orgDoneNotice() { $("org-banner").hidden = true; }

  function setUser(login) {
    var name = (login && login.username) || "已登录";
    $("me-user").textContent = name;
    if ($("user-avatar")) $("user-avatar").textContent = name.slice(0, 1).toUpperCase();
    $("me-meta").textContent = (login && login.acct_display) || "金蝶云星空";
    $("me-meta").title = ((login && login.acct_display) || "") + " · " + ((login && login.login_at) || "");
  }

  function syncOrgSummary() { $("org-banner").hidden = true; }

  // 写操作页的标签（不能再用"只读"——那两页是真的会写金蝶数据）
  var TAG_WRITE = '<span class="read-only-tag" style="color:#C0531F;background:#FFF1E0;border-color:#F0C9A8">' +
    '<svg class="icon"><use href="#i-send"/></svg>可写金蝶数据</span>';

  function intro(title, description, tag) {
    // ⚠️ 顶部标题栏已经显示页面名（#ttl），这里**不再重复渲染 h2** ——
    //    否则每页标题出现两次（用户截图就是这个）。
    var t = (tag === undefined)
      ? '<span class="read-only-tag"><svg class="icon"><use href="#i-shield"/></svg>金蝶数据 · 只读</span>'
      : tag;
    return '<div class="page-intro"><div><p>' + esc(description) + '</p></div>' + t + '</div>';
  }

  /* 统一的"一行"错误行（详情区重试、顶部提示都用它） */
  function errLine(err) {
    err = err || {};
    var tip = ((err.stage === "表单" || err.stage === "登录") && err.hint) ? "｜" + esc(err.hint) : "";
    return '<div class="banner err">取数失败' + (err.stage ? "（" + esc(err.stage) + "）" : "") +
      "：" + esc(err.message || "") + tip + "</div>";
  }

  /* 写操作说明：**只在页面关掉时提示**，开着就不再刷屏（说明统一放在「数据源与口径」页） */
  function rbNoticeIfOff(st) {
    return (st && st.enabled) ? ""
      : '<div class="banner warn">写操作未开启：把 portal_config.json 里的 enable_write_ops ' +
        "设为 true 后重启即可。</div>";
  }

  function skeleton(view) {
    if ($("v-" + view).children.length) return;
    var title = view === "expense" ? "费用报销看板"
      : view === "sales" ? "销售出库看板"
      : view === "cash" ? "现金流预测看板"
      : "管理会计看板";
    $("v-" + view).innerHTML = intro(title, "正在读取你的金蝶数据…") +
      '<div class="grid g4" aria-label="数据加载中"><div class="skeleton-card"></div><div class="skeleton-card"></div>' +
      '<div class="skeleton-card"></div><div class="skeleton-card"></div></div>';
  }

  function dataStatus(d, detail, coverage) {
    return '<div class="data-status"><div class="status-main"><span class="live"></span><b>更新于 ' +
      esc(d.generated_at || "—") + '</b>' + (coverage ? '<span>· ' + esc(coverage) + '</span>' : '') +
      '</div><details class="status-detail"><summary>数据详情</summary><div class="hint">' + detail +
      apiUsage(d) + '</div></details></div>';
  }

  function bootApp(login, orgJob) {
    $("screen-app").hidden = false;
    setUser(login);
    skeleton("expense");
    bindApp();
    // 员工侧**不留管理入口**：租户管理只在管理员控制台（根地址）里出现
    try {
      var admA = $("nav").querySelector("a[data-v='adm']");
      if (admA) {
        var grp = admA.previousElementSibling;
        if (grp && grp.classList && grp.classList.contains("grp")) grp.style.display = "none";
        admA.style.display = "none";
      }
    } catch (e) { /* 忽略 */ }
    prog(true, "正在自动发现组织（组织名不内置，按账号权限提取）…", 5);
    poll(orgJob, function (res) {
      S.orgs = res.orgs || []; S.trace = res.trace || []; S.notes = res.notes || [];
      S.selected = S.orgs.map(function (o) { return o.number; });
      renderOrgChips(); renderOrgsView(); renderSourceView();
      syncOrgSummary();
      loadExpense(false);
    });
  }

  /* ---------------- 报销办理（写操作：下推 / 建收票单 / 调金额 / 体检 / 提交） ----------------
     ⚠️ 这是门户里唯一的写入口。与只读看板的区别：
        · 看板用「登录者本人的金蝶会话」取数
        · 本页走报销 skill 配置的账号（写收票信息通常需要收票池权限）
     所以默认关闭，开启方式见 portal_config.json 的 enable_write_ops。 */
  var R = { status: null, rows: null, bill: null, fid: "", travel: false,
            pendingFocus: "", upMsgs: [] };

  function rbBanner(st) {
    if (!st.enabled) {
      return '<div class="banner warn"><b>写操作未开启，本页暂不可用。</b>' +
        "开启方式：把 <span class=\"mono\">portal_config.json</span> 里的 " +
        '<span class="mono">"enable_write_ops"</span> 改为 <span class="mono">true</span>，重启门户即可。</div>';
    }
    return '<div class="banner info"><b>本页会写金蝶数据</b>（下推报销单 / 建收票单 / 提交审批三个动作）。' +
      "所有动作都记入 <span class=\"mono\">" + esc(st.audit_log || "portal_audit.log") + "</span>。" +
      "底层用报销 skill 配置的账号（写收票信息通常需要收票池权限），" +
      "与看板那种「用登录者本人账号只读取数」不是同一个身份。</div>";
  }

  /* ---- 页 1：申请单（挑一张已审核的申请单，一键下推） ---- */
  function loadApply() {
    var root = $("v-apply");
    root.innerHTML = skeleton("apply");
    prog(true, "正在取数，请耐心等待…", 8);
    api("/api/reimburse/status", null, "GET").then(function (st) {
      if (!st.ok) { showErr(st.error); return; }
      R.status = st;
      var h = [];
      h.push(rbNoticeIfOff(st));
      if (!st.enabled) { prog(false);
      root.innerHTML = h.join(""); return; }
      h.push('<div class="sec">可下推的申请单 </div>');
      h.push('<div id="rb-list"><div class="hint">加载中…</div></div>');
      prog(false);
      root.innerHTML = h.join("");
      rbLoadList();
    }).catch(function () {
      root.innerHTML = '<div class="banner warn">连接失败，请刷新重试。</div>';
    });
  }

  /* ---- 页 2：报销单（统计 + 列表 + 办理区） ---- */
  function loadBillsPage() {
    var root = $("v-bills");
    root.innerHTML = skeleton("bills");
    prog(true, "正在取数，请耐心等待…", 8);
    api("/api/reimburse/status", null, "GET").then(function (st) {
      if (!st.ok) { showErr(st.error); return; }
      R.status = st;
      var h = [];
      h.push(rbNoticeIfOff(st));
      if (!st.enabled) { prog(false);
      root.innerHTML = h.join(""); return; }
      h.push('<div id="rb-stats"></div>');
      h.push('<div class="sec">报销单列表 <span class="sub">点单号继续办理</span></div>');
      h.push('<div class="row-actions" style="margin-bottom:8px">' +
        '<button class="btn" id="rb-filter-all">全部</button>' +
        '<button class="btn" id="rb-filter-pending">只看审核中</button>' +
        '<button class="btn" id="rb-filter-draft">只看未提交</button></div>');
      h.push('<div id="rb-bills"><div class="hint">加载中…</div></div>');
      h.push('<div id="rb-detail" hidden></div>');
      prog(false);
      root.innerHTML = h.join("");
      $("rb-filter-all").addEventListener("click", function () { rbLoadBills("all"); });
      $("rb-filter-pending").addEventListener("click", function () { rbLoadBills("pending"); });
      $("rb-filter-draft").addEventListener("click", function () { rbLoadBills("draft"); });
      rbLoadBills("all");
      if (R.pendingFocus) {                       // 从申请单下推后跳过来，直接打开新单弹窗
        var f = R.pendingFocus;
        R.pendingFocus = "";
        rbBillDialog(f, R.travel);
      }
    }).catch(function () {
      root.innerHTML = '<div class="banner warn">连接失败，请刷新重试。</div>';
    });
  }

  function rbLoadList() {
    api("/api/reimburse/requests", null, "GET").then(function (r) {
      if (!r.ok) { showErr(r.error); $("rb-list").innerHTML = '<div class="hint">读取失败</div>'; return; }
      R.rows = r.rows || [];
      if (!R.rows.length) { $("rb-list").innerHTML = '<div class="hint">没有「已审核且未关闭」的申请单可下推。</div>'; return; }
      var rows = R.rows.map(function (x) {
        return "<tr><td class='mono'>" + esc(x.bill_no) + "</td><td>" + esc(x.date) + "</td>" +
          "<td>" + esc(x.kind) + (x.is_borrow ? " <span class='tag'>借款</span>" : "") + "</td>" +
          "<td>" + esc(x.org_no + " " + x.org_name) + "</td>" +
          "<td>" + esc(x.staff) + "</td><td class='num'>" + money(x.amount) + "</td>" +
          "<td class='row-acts'><button type='button' class='btn' data-view='" +
          esc(x.bill_no) + "' data-travel='" + (x.kind === "出差申请" ? "1" : "0") +
          "'>查看</button> " +
          "<button type='button' class='btn pri' data-push='" + esc(x.bill_no) +
          "' data-travel='" + (x.kind === "出差申请" ? "1" : "0") +
          "'>下推</button></td></tr>";
      });
      // 统计框（2026-10-20 用户要求，类似报销单页）
      var _sum = R.rows.reduce(function (a, x) { return a + (Number(x.amount) || 0); }, 0);
      var _staff = {}, _orgs = {};
      R.rows.forEach(function (x) { if (x.staff) _staff[x.staff] = 1; if (x.org_no) _orgs[x.org_no] = 1; });
      var _borrow = R.rows.filter(function (x) { return x.is_borrow; }).length;
      $("rb-list").innerHTML =
        '<div class="rb-stat">' +
        '<div><span>可下推</span><b>' + R.rows.length + ' <em>张</em></b></div>' +
        '<div><span>申请金额合计</span><b>' + money(_sum) + '</b></div>' +
        '<div><span>涉及申请人</span><b>' + Object.keys(_staff).length + ' <em>人</em></b></div>' +
        '<div><span>涉及组织</span><b>' + Object.keys(_orgs).length + ' <em>个</em></b></div>' +
        (_borrow ? '<div><span>含借款单</span><b>' + _borrow + ' <em>张</em></b></div>' : '') +
        '</div>' +
        table([{ t: "申请单号" }, { t: "日期" }, { t: "类型" }, { t: "组织" }, { t: "申请人" },
               { t: "申请金额", num: 1 }, { t: "操作" }], rows);
      Array.prototype.forEach.call($("rb-list").querySelectorAll("button[data-push]"), function (b) {
        b.addEventListener("click", function () {
          // ★ 2026-10-18：改成弹窗（生成草稿 → 上传发票 → 提交 → 列表看状态）
          var no = b.dataset.push, tv = b.dataset.travel === "1";
          var hit = (R.rows || []).filter(function (z) { return z.bill_no === no; })[0] ||
                    { bill_no: no, kind: tv ? "出差申请" : "费用申请" };
          rbPushDialog(hit, tv);
        });
      });
      Array.prototype.forEach.call($("rb-list").querySelectorAll("button[data-view]"), function (b) {
        b.addEventListener("click", function () {
          var no = b.dataset.view, tv = b.dataset.travel === "1";
          var hit = (R.rows || []).filter(function (z) { return z.bill_no === no; })[0] ||
                    { bill_no: no, kind: tv ? "出差申请" : "费用申请" };
          rbViewRequest(hit);
        });
      });
    });
  }

  function rbPush(billNo, travel) {
    var box = $("rb-detail");
    box.hidden = false;
    box.innerHTML = '<div class="sec">已下推 ' + esc(billNo) + '</div><div class="hint">正在下推…</div>';
    api("/api/reimburse/push", { bill_no: billNo, is_travel: !!travel }).then(function (r) {
      if (!r.ok) { showErr(r.error); box.querySelector(".hint").textContent = "下推失败（见上方错误）"; return; }
      R.travel = !!travel;
      // 下推成功 → 直接跳到「报销单」页并打开这张新单（用户要的"切换过去"快捷动作）
      R.pendingFocus = r.result.fid;
      gotoView("bills");
    });
  }

  function gotoView(v) {
    var a = $("nav").querySelector("a[data-v='" + v + "']");
    if (a) a.click();
  }

  function rbOpen(fid, scroll) {
    // ⚠️ 2026-10-18 起详情统一走弹窗 rbBillDialog；这个旧实现只作兜底转发，
    //    页面下方的 #rb-detail 区域不再使用。
    rbBillDialog(fid, R.travel);
    return;
    // 换了一张单 → 上一张的上传结果提示要清掉（同一张则保留，否则刷新会把提示冲掉）
    if (String(fid) !== String(R.fid)) R.upMsgs = [];
    R.fid = fid;
    var box = $("rb-detail");
    box.hidden = false;
    box.innerHTML = '<div class="sec">报销单详情</div><div class="hint">正在取数，请耐心等待…</div>';
    if (scroll) { try { box.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (e) { /* 忽略 */ } }
    prog(true, "正在取数，请耐心等待…", 10);
    api("/api/reimburse/bill/" + fid + (R.travel ? "?travel=1" : ""), null, "GET").then(function (r) {
      prog(false);
      if (!r.ok) {
        // ⚠️ 取失败时**绝不能留空白**：早前只 showErr，详情区就一直停在"加载中…"，
        //    看起来像"报销信息空了、金额没数据"。这里就地给一行错 + 重试。
        box.innerHTML = '<div class="sec">报销单详情</div>' + errLine(r.error) +
          '<div class="row-actions"><button class="btn" id="rb-retry">重新取数</button></div>';
        var b = $("rb-retry");
        if (b) b.addEventListener("click", function () { rbOpen(fid, false); });
        return;
      }
      R.bill = r.bill; rbRender();
    }).catch(function () {
      prog(false);
      box.innerHTML = '<div class="sec">报销单详情</div>' +
        '<div class="banner err">取数失败：连接异常，请重试</div>' +
        '<div class="row-actions"><button class="btn" id="rb-retry">重新取数</button></div>';
      var b2 = $("rb-retry");
      if (b2) b2.addEventListener("click", function () { rbOpen(fid, false); });
    });
  }

  function rbRender() {
    var b = R.bill || {}, h = [];
    h.push('<div class="row-actions" style="margin-bottom:8px">' +
      '<button class="btn" id="rb-back">← 返回报销单列表</button>' +
      '<span class="hint" style="margin:0">下面这一片就是这张单的办理区：上传发票、改金额、体检、提交；' +
      "删单按钮在标题行右侧（仅暂存/已保存/重新审核可删）</span></div>");
    h.push('<div class="sec">报销单 <span class="sub">FID ' + esc(b.fid) +
      " · 下推后是草稿(Z)，提交前一直是可改状态</span></div>");
    var canDel = !!({ Z: 1, A: 1, D: 1 })[String(b.status || "").toUpperCase()];
    h.push('<div class="row-actions" style="margin-bottom:10px">' +
      (canDel ? '<button class="btn danger" id="rb-del">删除此单</button>' : "") +
      '<span class="hint" style="margin:0">' +
      (canDel ? "当前状态（" + esc(stText(b.status)) + "）可删除；重复下推的草稿用这里清掉"
              : "当前状态（" + esc(stText(b.status)) + "）不能删，请先在金蝶撤销/反审核") +
      "</span></div>");
    h.push('<div class="grid g4">' +
      kpi("单据编号", b.bill_no || "（草稿未编号）", "状态 " + esc(b.status), "blue") +
      kpi("报销金额", wan(b.amount), "明细 " + (b.entries || []).length + " 行", "green") +
      kpi("发票价税合计", wan(b.recv_total), "收票信息 " + (b.recv_rows || []).length + " 行", "amber") +
      kpi("差额", money((b.amount || 0) - (b.recv_total || 0)),
        (b.amount > b.recv_total ? "报销额大于发票合计" : "发票充足"), "gray") +
      "</div>");

    h.push('<div class="sec">发票（收票信息）</div>');
    if (!(b.recv_rows || []).length) {
      h.push('<div class="hint">还没有发票。请在下面上传。</div>');
    } else {
      // 有原件预览地址就出缩略图（发票云返回的 pdf_url），点开看大图；
      // 加载不出来时（需要鉴权等）链接文字"打开原件"仍然可用，不留破图。
      h.push(table([{ t: "收票单号" }, { t: "发票号" }, { t: "价税合计", num: 1 }, { t: "发票原件" }],
        b.recv_rows.map(function (x) {
          var cell = "—";
          if (x.pdf_url) {
            cell = '<a class="rb-orig" href="' + esc(x.pdf_url) + '" target="_blank" rel="noopener">' +
              '<img class="rb-mini" src="' + esc(x.pdf_url) + '" alt="发票原件"' +
              " onerror=\"this.style.display='none'\">打开原件</a>";
          }
          return "<tr><td class='mono'>" + esc(x.bill_no) + "</td><td class='mono'>" +
            esc(x.invoice_no || "") + "</td><td class='num'>" + money(x.amount) +
            "</td><td>" + cell + "</td></tr>";
        })));
    }

    h.push('<div class="sec">上传发票 <span class="sub">可多选；组织按本单自动带出，无需填写</span></div>');
    h.push('<div class="uploader">' +
      '<div class="rb-field"><label>发票文件 <span class="opt">可多选</span></label>' +
      '<input type="file" id="rb-file" multiple accept=".pdf,.xml,.ofd,.jpg,.jpeg,.png"></div>' +
      '<div id="rb-thumbs" class="rb-thumbs"></div>' +
      '<div class="rb-field"><label>发票明细项目名称 <span class="opt">可选</span></label>' +
      '<input id="rb-item" placeholder="留空即可（金蝶会沿用申请单的费用项目名称）" value="' +
      esc(rbLastItem()) + '">' +
      '<div class="hint">它是发票上「<b>货物或应税劳务、服务名称</b>」那一栏（如 ' +
      "<span class=\"mono\">*交通运输服务*客运服务费</span>），<b>不是组织名称</b>。" +
      "不合并生成费用明细时可以留空。填过一次会记住。</div></div>" +
      '<div class="row-actions"><button class="btn pri" id="rb-do-upload">上传并挂到本单</button>' +
      '<span class="hint" style="margin:0">本单组织：<b>' + esc(b.org_no || "—") + "</b></span></div>" +
      '<div class="hint">XML / PDF 的识别率高于拍照件。识别不出是发票的文件（行程单、说明、扫描件）会自动挂到本单的「附件」，不需要用户选择，所以可以一起拖进来。</div></div>');
    h.push('<div id="rb-msg">' + (R.upMsgs || []).join("") + "</div>");

    // 金额核对（2026-10-19 用户定）：先摆出两个金额让人对照，再让他自己定/改，不再一键写死
    h.push('<div class="sec">金额核对</div>');
    h.push('<div class="rb-amt">' +
      '<div><span>发票合计</span><b>' + money(b.recv_total) + "</b></div>" +
      '<div><span>当前报销</span><b>' + money(b.amount) + "</b></div>" +
      "</div>");
    h.push('<div class="row-actions">' +
      '<span class="hint" style="margin:0">报销金额</span>' +
      '<input id="rb-amount" value="' + esc(b.amount) + '" style="max-width:150px">' +
      '<button class="btn" id="rb-userecv">用发票合计</button>' +
      '<button class="btn pri" id="rb-set">保存</button></div>');

    h.push('<div class="sec">提交</div>');
    h.push('<div class="row-actions"><button class="btn" id="rb-check">提交前体检</button>' +
      '<button class="btn pri" id="rb-submit">提交审批</button></div>');
    h.push('<div id="rb-checkbox"></div>');
    h.push('<div class="hint">提交会让单据进入审批流（状态 → 审核中），请确认金额与发票已核对。' +
      "提交后可在下面「最近报销单」里查看进度。</div>");

    $("rb-detail").innerHTML = h.join("");
    $("rb-do-upload").addEventListener("click", function () { rbUpload(); });
    $("rb-file").addEventListener("change", rbPreview);
    var useRecv = $("rb-userecv");
    if (useRecv) useRecv.addEventListener("click", function () {
      $("rb-amount").value = Number(R.bill.recv_total || 0).toFixed(2);   // 只填入，不保存
    });
    $("rb-set").addEventListener("click", function () { rbSetAmount(); });
    $("rb-check").addEventListener("click", function () { rbCheck(); });
    $("rb-submit").addEventListener("click", function () { rbSubmit(); });
    if ($("rb-del")) $("rb-del").addEventListener("click", rbDeleteBill);
    if ($("rb-back")) $("rb-back").addEventListener("click", function () {
      var box2 = $("rb-bills");
      if (box2) { try { box2.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (e) { /* 忽略 */ } }
    });
    rbWatchMsg();
  }

  /* ★ 2026-10-19（用户要求）：详情页的提示区一有新内容，自动滚过去。
     保存/体检/上传的结果都在页面下部，以前每次都要手动往下滑。 */
  function rbWatchMsg() {
    ["rb-msg", "rb-checkbox"].forEach(function (id) {
      var el = $(id);
      if (!el || el.dataset.watched === "1") return;
      el.dataset.watched = "1";
      try {
        new MutationObserver(function () {
          if (!el.textContent.trim()) return;
          setTimeout(function () {
            try { el.scrollIntoView({ behavior: "smooth", block: "center" }); } catch (e) { /* 忽略 */ }
          }, 40);
        }).observe(el, { childList: true, subtree: true, characterData: true });
      } catch (e) { /* 老浏览器忽略 */ }
    });
  }

  function rbLastItem() {
    try { return window.localStorage.getItem("rb_item_name") || ""; } catch (e) { return ""; }
  }

  /* 选中文件后立刻本地预览缩略图（图片直接预览；PDF/XML 显示扩展名）。
     用 URL.createObjectURL，**不往服务器落文件**。 */
  function rbPreview() {
    var box = $("rb-thumbs");
    if (!box) return;
    var files = ($("rb-file") && $("rb-file").files) || [];
    box.innerHTML = "";
    Array.prototype.forEach.call(files, function (f) {
      var d = document.createElement("div");
      d.className = "rb-thumb";
      if (/^image\//.test(f.type)) {
        var a1 = document.createElement("a");
        a1.href = URL.createObjectURL(f);
        a1.target = "_blank";
        a1.rel = "noopener";
        a1.title = "点开看大图：" + f.name;
        var img = document.createElement("img");
        img.src = a1.href;
        img.alt = f.name;
        a1.appendChild(img);
        d.appendChild(a1);
      } else {
        // PDF / XML 等浏览器给不出缩略图：做成**可点开的卡片**，别只显示两个字母
        var a2 = document.createElement("a");
        a2.className = "rb-thumb-ext";
        a2.href = URL.createObjectURL(f);
        a2.target = "_blank";
        a2.rel = "noopener";
        a2.title = "点开查看原件：" + f.name;
        a2.textContent = (f.name.split(".").pop() || "?").toUpperCase().slice(0, 4) + " 打开";
        d.appendChild(a2);
      }
      var n = document.createElement("span");
      n.className = "rb-thumb-name";
      n.textContent = f.name;
      d.appendChild(n);
      box.appendChild(d);
    });
  }

  function rbRememberItem(name) {
    try { window.localStorage.setItem("rb_item_name", name); } catch (e) { /* 隐私模式忽略 */ }
  }

  /* 报销单列表 + 状态统计（mode: all | pending | draft；统计永远按全量算） */
  function rbLoadBills(mode) {
    var box = $("rb-bills");
    if (!box) return;
    mode = mode || "all";
    box.innerHTML = '<div class="hint">加载中…</div>';
    api("/api/reimburse/bills", null, "GET").then(function (r) {
      if (!r.ok) { box.innerHTML = '<div class="hint">读取失败</div>'; return; }
      var all = r.rows || [];
      R.rows = all;
      var tags = { "暂存": "gray", "已保存": "blue", "审核中": "amber", "已审核": "green", "作废": "gray" };
      var sum = function (f) { return all.filter(f).reduce(function (a, x) { return a + (x.amount || 0); }, 0); };
      var n = function (f) { return all.filter(f).length; };
      var isPending = function (x) { return x.status === "B"; };
      var isDraft = function (x) { return x.status === "Z" || x.status === "A"; };
      var sb = $("rb-stats");
      if (sb) {
        sb.innerHTML = '<div class="rb-stat">' +
          '<div><span>报销单总数</span><b>' + all.length + ' <em>张</em></b></div>' +
          '<div><span>未提交</span><b>' + n(isDraft) + ' <em>张</em></b><em class="sub-amt">' + money(sum(isDraft)) + '</em></div>' +
          '<div><span>审核中</span><b>' + n(isPending) + ' <em>张</em></b><em class="sub-amt">' + money(sum(isPending)) + '</em></div>' +
          '<div><span>已审核</span><b>' + n(function (x) { return x.status === "C"; }) + ' <em>张</em></b><em class="sub-amt">' +
            money(sum(function (x) { return x.status === "C"; })) + '</em></div>' +
          "</div>";
      }
      var rows = all.filter(mode === "pending" ? isPending : mode === "draft" ? isDraft : function () { return true; });
      // ★ 2026-10-18（用户反馈"草稿没有打开的位置"）：原来**只有单号那一格能点**，
      //   点状态/金额等列毫无反应，草稿又显示成「(草稿) 101784」不像按钮 → 看着像打不开。
      //   现在：整行可点 + 行尾给明确按钮（可删的写「办理/删除」，其余写「查看详情」）。
      var body = rows.map(function (x) {
        var del = !!(x.status && ({ "暂存": 1, "已保存": 1, "重新审核": 1 })[x.status_cn]);
        var attrs = " data-open='" + esc(x.fid) + "' data-kind='" + esc(x.kind) + "'";
        return "<tr class='rb-row'" + attrs + ">" +
          "<td class='mono'><a href='#'" + attrs + ">" +
          esc(x.bill_no || ("(草稿 " + x.fid + ")")) + "</a></td>" +
          "<td>" + esc(x.kind) + "</td>" +
          "<td><span class='badge badge-" + esc(tags[x.status_cn] || "gray") + "'>" +
          esc(x.status_cn) + "</span></td>" +
          "<td class='num'>" + money(x.amount) + "</td>" +
          "<td>" + esc(x.proposer) + "</td>" +
          "<td>" + esc(x.org_no + " " + x.org_name) + "</td>" +
          "<td class='mono'>" + esc(x.created) + "</td>" +
          "<td class='row-acts'><a href='#' class='btn'" + attrs + ">查看</a>" +
          (del ? " <a href='#' class='btn danger' data-del='" + esc(x.fid) + "' data-no='" +
                 esc(x.bill_no || "") + "' data-kind='" + esc(x.kind) + "'>删除</a>" : "") +
          "</td></tr>";
      });
      box.innerHTML = rows.length
        ? table([{ t: "单据编号" }, { t: "类型" }, { t: "状态" }, { t: "金额", num: 1 },
                 { t: "申请人" }, { t: "组织" }, { t: "创建时间" }, { t: "操作" }], body)
        : '<div class="hint">没有符合条件的单据。</div>';
      var openFrom = function (el) {
        // ★ 2026-10-18：改为弹窗查看（用户要求）
        rbBillDialog(el.dataset.open, el.dataset.kind === "差旅报销");
      };
      Array.prototype.forEach.call(box.querySelectorAll("a[data-open]"), function (a) {
        a.addEventListener("click", function (e) {
          e.preventDefault();
          e.stopPropagation();
          openFrom(a);
        });
      });
      Array.prototype.forEach.call(box.querySelectorAll("tr.rb-row"), function (tr) {
        tr.addEventListener("click", function (e) {
          if (e.target.closest && e.target.closest("a[data-del]")) return;    // 删除按钮自己处理
          if (e.target.closest && e.target.closest("a[data-open]")) return;   // 查看链接自己处理
          e.preventDefault();
          openFrom(tr);
        });
      });
      Array.prototype.forEach.call(box.querySelectorAll("a[data-del]"), function (a) {
        a.addEventListener("click", function (e) {
          e.preventDefault();
          e.stopPropagation();
          rbDeleteByFid(a.dataset.del, a.dataset.no, a.dataset.kind === "差旅报销");
        });
      });
    });
  }

  function rbUpload() {
    var inp = $("rb-file"), item = ($("rb-item").value || "").trim();
    var msg = $("rb-msg");
    var files = (inp && inp.files) ? Array.prototype.slice.call(inp.files) : [];
    if (!files.length) { msg.innerHTML = '<div class="banner warn">请先选择发票文件。</div>'; return; }
    var out = [];
    msg.innerHTML = '<div class="hint">上传中（共 ' + files.length + ' 个文件，发票云识别 + 查验通常几秒）…</div>';
    (function next(i) {
      if (i >= files.length) {
        if (item) rbRememberItem(item);
        // ⚠️ 先把结果存起来再刷新：`rbOpen` 会重渲染详情、把 #rb-msg 清空 ——
        //    不存的话用户看到的就是"归零了、也看不到失败原因"。
        R.upMsgs = out;
        msg.innerHTML = out.join("");
        rbOpen(R.fid);
        rbLoadBills("all");
        return;
      }
      var file = files[i];
      var fd = new FormData();
      fd.append("file", file);
      fd.append("bill_fid", R.fid);
      fd.append("bill_no", (R.bill || {}).bill_no || "");
      fd.append("org_no", "");          // 留空 → 后端按报销单的组织
      fd.append("item_name", item);     // 可为空：不合并生成明细时金蝶沿用申请单名称
      if (R.travel) fd.append("is_travel", "1");
      fetch("/api/reimburse/upload", {
        method: "POST", body: fd, headers: _portalHeaders({ "X-CSRF-Token": csrf() })
      }).then(function (r) { return r.json(); }).then(function (j) {
        if (!j.ok) {
          var e = j.error || {};
          out.push('<div class="banner warn"><b>' + esc(file.name) + "：</b>" +
            esc(e.stage || "失败") + " — " + esc(e.message || "") +
            (e.hint ? "<br>" + esc(e.hint) : "") + "</div>");
        } else if ((j.result || {}).route === "attach") {
          out.push('<div class="banner info">📎 <b>' + esc(file.name) +
            "</b> 发票云未识别为发票 → 已作为<b>附件</b>挂到本单（文件ID " +
            esc((j.result || {}).file_id) + "）</div>");
        } else {
          var x = j.result || {};
          out.push('<div class="banner info">✅ <b>' + esc(file.name) + "</b> → 收票单 <b>" +
            esc(x.recvin_bill_no) + "</b>｜发票号 " + esc(x.invoice_no) + "｜价税合计 " +
            money(x.amount) + "｜销方 " + esc(x.seller) +
            (x.serial_ok ? "｜流水号已回读核对 ✅" : "｜<b>流水号没核对上，先别提交</b>") + "</div>");
        }
        msg.innerHTML = out.join("") + '<div class="hint">进度 ' + (i + 1) + "/" + files.length + "</div>";
        next(i + 1);
      }).catch(function () {
        out.push('<div class="banner warn"><b>' + esc(file.name) + "：</b>上传失败（连接异常）</div>");
        msg.innerHTML = out.join("");
        next(i + 1);
      });
    })(0);
  }

  function rbSetAmount(fixed) {
    var v = (fixed === undefined) ? parseFloat($("rb-amount").value) : Number(fixed);
    if (!isFinite(v)) { showErr({ message: "金额不是数字" }); return; }
    api("/api/reimburse/amount", { fid: R.fid, amount: v, is_travel: !!R.travel }).then(function (r) {
      if (!r.ok) { showErr(r.error); return; }
      rbOpen(R.fid);
    });
  }

  function rbCheck() {
    api("/api/reimburse/precheck/" + R.fid + (R.travel ? "?travel=1" : ""), null, "GET").then(function (r) {
      var c = r.check || {};
      if (!r.ok) { showErr(r.error); return; }
      $("rb-checkbox").innerHTML = c.ok
        ? '<div class="banner info">✅ 可以提交（报销 ' + money(c.amount) + " ≤ 发票合计 " +
          money(c.recv_total) + "，收票 " + c.recv_rows + " 行）</div>"
        : '<div class="banner warn"><b>还不能提交：</b><br>' +
          (c.issues || []).map(esc).join("<br>") + (c.gate_message ? "<br>" + esc(c.gate_message) : "") + "</div>";
    });
  }

  function rbSubmit() {
    if (!window.confirm("提交后单据会进入审批流（A → B），审批人将收到待办。\n确认提交吗？")) return;
    api("/api/reimburse/submit", { fid: R.fid, confirm: "SUBMIT", is_travel: !!R.travel }).then(function (r) {
      if (!r.ok) { showErr(r.error); return; }
      var x = r.result || {};
      $("rb-checkbox").innerHTML = '<div class="banner info">✅ 已提交：' + esc(x.before_status) +
        " → <b>" + esc(x.after_status) + "</b>｜单号 " + esc(x.bill_no) + "</div>";
      rbOpen(R.fid);
    });
  }


  /* ================================================================
     弹窗交互（2026-10-18 用户要求）：
       ① 下推报销单改成弹窗：生成草稿 → 上传发票 → 提交，回到列表看状态
       ② 报销单可删除（仅 暂存/已保存/重新审核；审核中/已审核先撤销）
       ③ 申请单只读查看（审批依据不做编辑）
     ================================================================ */
  function stText(code) {
    return ({ Z: "暂存", A: "已保存", B: "审核中", C: "已审核", D: "重新审核" })[code] || (code || "未知");
  }

  function openModal(title, bodyHtml, opts) {
    opts = opts || {};
    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML = '<div class="modal" role="dialog" aria-modal="true" aria-label="' +
      esc(title) + '"><div class="modal-head"><b>' + esc(title) + "</b>" +
      '<button type="button" class="modal-x" aria-label="关闭">×</button></div>' +
      '<div class="modal-body"></div></div>';
    document.body.appendChild(mask);
    var body = mask.querySelector(".modal-body");
    body.innerHTML = bodyHtml || "";
    var closed = false;
    function close() {
      if (closed) return;
      closed = true;
      mask.remove();
      if (opts.onClose) opts.onClose();
    }
    mask.querySelector(".modal-x").addEventListener("click", close);
    if (opts.dismissible !== false) {
      mask.addEventListener("click", function (e) { if (e.target === mask) close(); });
      var onKey = function (e) {
        if (e.key === "Escape") { document.removeEventListener("keydown", onKey); close(); }
      };
      document.addEventListener("keydown", onKey);
    }
    return { el: mask.querySelector(".modal"), body: body, close: close };
  }

  /* ---------------- ① 下推弹窗 ---------------- */
  var PD = null;

  function rbPushDialog(x, travel) {
    PD = { item: x, travel: !!travel, fid: "", bill: null, msgs: [], busy: false, m: null };
    PD.m = openModal("下推报销单 · " + (x.bill_no || ""),
      '<div class="hint">正在金蝶生成草稿报销单…</div>',
      { onClose: function () { PD = null; } });
    api("/api/reimburse/push", { bill_no: x.bill_no, is_travel: !!travel }).then(function (r) {
      if (!PD) return;
      if (!r.ok) {
        PD.m.body.innerHTML = errLine(r.error) +
          '<div class="row-actions"><button class="btn" id="pd-close">关闭</button></div>';
        var c = document.getElementById("pd-close");
        if (c) c.addEventListener("click", PD.m.close);
        return;
      }
      PD.fid = String((r.result || {}).fid || "");
      pdReload();
    }).catch(function () {
      if (PD) PD.m.body.innerHTML = '<div class="banner err">下推失败：连接异常</div>';
    });
  }

  function pdReload() {
    if (!PD) return;
    api("/api/reimburse/bill/" + PD.fid + (PD.travel ? "?travel=1" : ""), null, "GET")
      .then(function (r) {
        if (!PD) return;
        if (r.ok) PD.bill = r.bill;
        pdRender();
      });
  }

  function pdRender() {
    if (!PD || !PD.m) return;
    var it = PD.item, b = PD.bill || {}, h = [];
    h.push('<div class="pd-sum">' +
      "<div>申请单<b class='mono'>" + esc(it.bill_no) + "</b></div>" +
      "<div>申请人<b>" + esc(it.staff) + "</b></div>" +
      "<div>申请金额<b>" + money(it.amount) + "</b></div>" +
      "<div>组织<b>" + esc((it.org_no || "") + " " + (it.org_name || "")) + "</b></div>" +
      (it.reason ? '<div style="grid-column:1/-1">事由<b style="font-weight:400;font-size:13px">' +
        esc(it.reason) + "</b></div>" : "") + "</div>");
    h.push('<div class="pd-step">① 草稿报销单 <span>已自动生成，提交前一直可改</span></div>');
    h.push('<div class="banner info">已生成：<b>' + esc(b.bill_no || "（草稿未编号）") +
      "</b>（FID " + esc(PD.fid) + "）｜金额 " + money(b.amount) + "｜发票 " +
      (b.recv_rows || []).length + " 张，合计 " + money(b.recv_total) + "</div>");
    h.push('<div class="pd-step">② 上传发票 <span>可多选；识别不出是发票的会自动转附件</span></div>');
    h.push('<div class="rb-field"><input type="file" id="pd-file" multiple ' +
      'accept=".pdf,.xml,.ofd,.jpg,.jpeg,.png"></div>' +
      '<div class="row-actions"><button class="btn pri" id="pd-upload">上传发票</button>' +
      '<span class="hint" style="margin:0">上传完成后可点「按发票合计对齐」再提交</span></div>');
    h.push('<div id="pd-msgs">' + (PD.msgs || []).join("") + "</div>");
    h.push('<div class="pd-step">③ 提交或稍后办</div>');
    h.push('<div class="row-actions">' +
      '<button class="btn pri" id="pd-submit"' + (PD.busy ? " disabled" : "") +
      ">" + (PD.busy ? "处理中…" : "提交审批") + "</button>" +
      '<button class="btn" id="pd-align">按发票合计对齐</button>' +
      '<button class="btn" id="pd-later">先存草稿，去列表</button></div>');
    h.push('<div class="hint">提交前会自动体检（报销金额 ≤ 发票合计）。提交后状态变「审核中」，' +
      "能在报销单列表里直接看到。</div>");
    PD.m.body.innerHTML = h.join("");
    document.getElementById("pd-upload").addEventListener("click", pdUpload);
    document.getElementById("pd-submit").addEventListener("click", pdSubmit);
    document.getElementById("pd-align").addEventListener("click", function () {
      if (!PD || !PD.bill) return;
      PD.busy = true; pdRender();
      api("/api/reimburse/amount", { fid: PD.fid, amount: Number(PD.bill.recv_total || 0),
                                     is_travel: !!PD.travel }).then(function (r) {
        PD.busy = false;
        if (!r.ok) { showErr(r.error); pdRender(); return; }
        PD.msgs.push('<div class="banner info">金额已对齐到发票合计 ' +
          money((r.result || {}).amount) + "</div>");
        pdReload();
      });
    });
    document.getElementById("pd-later").addEventListener("click", function () {
      var fid = PD.fid, travel = PD.travel;
      R.travel = travel;
      PD.m.close();
      gotoView("bills");
      setTimeout(function () { rbBillDialog(fid, travel); }, 300);
    });
  }

  function pdUpload() {
    if (!PD) return;
    var inp = document.getElementById("pd-file");
    var files = (inp && inp.files) ? Array.prototype.slice.call(inp.files) : [];
    if (!files.length) {
      PD.msgs.push('<div class="banner warn">请先选择发票文件</div>');
      pdRender();
      return;
    }
    PD.busy = true;
    PD.msgs.push('<div class="hint">上传中（' + files.length + " 个文件，发票云识别通常几秒）…</div>");
    pdRender();
    (function next(i) {
      if (!PD) return;
      if (i >= files.length) {
        PD.busy = false;
        pdReload();                       // 刷新金额/发票合计
        return;
      }
      var f = files[i];
      var fd = new FormData();
      fd.append("file", f);
      fd.append("bill_fid", PD.fid);
      fd.append("bill_no", (PD.bill || {}).bill_no || "");
      fd.append("org_no", "");
      fd.append("item_name", "");
      if (PD.travel) fd.append("is_travel", "1");
      fetch("/api/reimburse/upload", {
        method: "POST", body: fd, headers: _portalHeaders({ "X-CSRF-Token": csrf() })
      }).then(function (r) { return r.json(); }).then(function (j) {
        if (!PD) return;
        if (!j.ok) {
          var e = j.error || {};
          PD.msgs.push('<div class="banner warn"><b>' + esc(f.name) + "：</b>" +
            esc(e.message || "失败") + (e.hint ? "<br>" + esc(e.hint) : "") + "</div>");
        } else if ((j.result || {}).route === "attach") {
          PD.msgs.push('<div class="banner info">📎 ' + esc(f.name) + " → 已作为附件挂到本单</div>");
        } else {
          var x = j.result || {};
          PD.msgs.push('<div class="banner info">✅ ' + esc(f.name) + " → 收票单 " +
            esc(x.recvin_bill_no) + "｜发票号 " + esc(x.invoice_no) + "｜" + money(x.amount) + "</div>");
        }
        pdRender();
        next(i + 1);
      }).catch(function () {
        if (!PD) return;
        PD.msgs.push('<div class="banner err">' + esc(f.name) + " 上传异常</div>");
        pdRender();
        next(i + 1);
      });
    })(0);
  }

  function pdSubmit() {
    if (!PD || PD.busy) return;
    var fid = PD.fid, travel = PD.travel;
    PD.busy = true;
    pdRender();
    api("/api/reimburse/precheck/" + fid + (travel ? "?travel=1" : ""), null, "GET").then(function (c) {
      if (!PD) return;
      var ck = c.check || {};
      if (!c.ok) { PD.busy = false; showErr(c.error); pdRender(); return; }
      if (!ck.ok) {
        PD.busy = false;
        PD.msgs.push('<div class="banner warn"><b>还不能提交：</b><br>' +
          (ck.issues || []).map(esc).join("<br>") + "</div>");
        pdRender();
        return;
      }
      api("/api/reimburse/submit", { fid: fid, confirm: "SUBMIT", is_travel: !!travel })
        .then(function (r) {
          if (!PD) return;
          PD.busy = false;
          if (!r.ok) { showErr(r.error); pdRender(); return; }
          var x = r.result || {};
          PD.m.close();
          gotoView("bills");
          setTimeout(function () {
            rbLoadBills("all");
            var b = document.createElement("div");
            b.className = "banner info";
            b.innerHTML = "✅ 已提交：" + esc(x.before_status) + " → <b>" +
              esc(x.after_status) + "</b>｜单号 " + esc(x.bill_no) +
              "（在下方「报销单列表」能看到状态）";
            var host = $("v-bills");
            if (host) host.insertBefore(b, host.firstChild);
          }, 300);
        });
    });
  }

  /* ---------------- ② 删除报销单 ---------------- */
  function rbDeleteBill() {
    var b = R.bill || {};
    if (!b.fid) return;
    var m = openModal("删除报销单",
      '<div class="banner warn">确定删除 <b>' + esc(b.bill_no || ("FID " + b.fid)) +
      "</b> 吗？<br>删除不可恢复（金蝶也没有回收站）。<br>" +
      "<span class='hint'>典型场景：一张申请单下推了多张报销单，把重复的那几张删掉。</span></div>" +
      '<div class="row-actions"><button class="btn danger" id="dl-ok">确认删除</button>' +
      '<button class="btn" id="dl-no">取消</button></div>');
    document.getElementById("dl-no").addEventListener("click", m.close);
    document.getElementById("dl-ok").addEventListener("click", function () {
      var btn = this;
      btn.disabled = true;
      btn.textContent = "删除中…";
      api("/api/reimburse/delete", { bill_fid: b.fid, is_travel: !!R.travel }).then(function (r) {
        m.close();
        if (!r.ok) { showErr(r.error); return; }
        R.bill = null;
        R.fid = "";
        var box = $("rb-detail");
        if (box) { box.hidden = true; box.innerHTML = ""; }
        rbLoadBills("all");
        var nb = document.createElement("div");
        nb.className = "banner info";
        nb.textContent = "已删除报销单 " + ((r.result || {}).bill_no || b.bill_no || "");
        $("v-bills").insertBefore(nb, $("v-bills").firstChild);
      });
    });
  }


  /* ================================================================
     报销单详情弹窗（2026-10-18 用户要求）
       · 列表「查看」直接开弹窗，不再用页面下方详情区
       · 只有暂存/已保存/重新审核（Z/A/D）才显示上传发票、改金额、提交；
         审核中(B)/已审核(C) 只读，明确告诉用户去金蝶撤销
       · 删除放在**列表行**（与查看并列），弹窗里不放删除
     ================================================================ */
  var BD = null;

  function rbBillDialog(fid, travel) {
    if (!fid) return;
    BD = { fid: String(fid), travel: !!travel, bill: null, msgs: [], busy: false, m: null };
    BD.m = openModal("报销单详情", '<div class="hint">正在取数，请耐心等待…</div>',
                     { onClose: function () { BD = null; } });
    bdReload();
  }

  function bdReload() {
    if (!BD) return;
    api("/api/reimburse/bill/" + BD.fid + (BD.travel ? "?travel=1" : ""), null, "GET")
      .then(function (r) {
        if (!BD) return;
        if (!r.ok) {
          BD.m.body.innerHTML = errLine(r.error) +
            '<div class="row-actions"><button class="btn" id="bd-retry">重新取数</button></div>';
          var rt = document.getElementById("bd-retry");
          if (rt) rt.addEventListener("click", bdReload);
          return;
        }
        BD.bill = r.bill;
        bdRender();
      })
      .catch(function () {
        if (!BD) return;
        BD.m.body.innerHTML = '<div class="banner err">取数失败：连接异常（网络繁忙），可重试。</div>' +
          '<div class="row-actions"><button class="btn" id="bd-retry">重新取数</button></div>';
        var rt = document.getElementById("bd-retry");
        if (rt) rt.addEventListener("click", bdReload);
      });
  }

  function bdRender() {
    if (!BD || !BD.m || !BD.bill) return;
    var b = BD.bill, h = [];
    var st = String(b.status || "").toUpperCase();
    var editable = ({ Z: 1, A: 1, D: 1 })[st];
    // 抬头：状态 + 三个关键数
    h.push('<div class="pd-sum">' +
      "<div>单据编号<b class='mono'>" + esc(b.bill_no || ("草稿 " + b.fid)) + "</b></div>" +
      "<div>状态<b>" + esc(stText(st)) + "</b></div>" +
      "<div>报销金额<b>" + money(b.amount) + "</b></div>" +
      "<div>发票价税合计<b>" + money(b.recv_total) + "</b></div>" +
      "<div>差额<b>" + money((Number(b.amount) || 0) - (Number(b.recv_total) || 0)) + "</b></div>" +
      "<div>组织<b>" + esc(b.org_no || "—") + "</b></div>" +
      "</div>");
    if (!editable) {
      h.push('<div class="banner info"><b>这张单当前是「' + esc(stText(st)) +
        "」，门户不提供修改。</b><br>要调整金额或发票，请先在金蝶里撤销提交/反审核，" +
        "回到草稿状态后再来办理。</div>");
    }
    // 发票
    h.push('<div class="sec" style="margin-top:12px">发票（收票信息）· ' +
      (b.recv_rows || []).length + " 张</div>");
    if (!(b.recv_rows || []).length) {
      h.push('<div class="hint">还没有发票。</div>');
    } else {
      h.push(table([{ t: "收票单号" }, { t: "发票号" }, { t: "价税合计", num: 1 }, { t: "原件" }],
        b.recv_rows.map(function (x) {
          var cell = "—";
          if (x.pdf_url) {
            cell = '<a class="rb-orig" href="' + esc(x.pdf_url) + '" target="_blank" ' +
              'rel="noopener"><img class="rb-mini" src="' + esc(x.pdf_url) + '" alt="发票原件" ' +
              "onerror=\"this.style.display='none'\">打开</a>";
          }
          return "<tr><td class='mono'>" + esc(x.bill_no) + "</td><td class='mono'>" +
            esc(x.invoice_no || "") + "</td><td class='num'>" + money(x.amount) +
            "</td><td>" + cell + "</td></tr>";
        })));
    }
    // 费用明细
    h.push('<div class="sec">费用明细 · ' + (b.entries || []).length + " 行</div>");
    if ((b.entries || []).length) {
      h.push(table([{ t: "#" }, { t: "费用项目" }, { t: "金额", num: 1 }],
        b.entries.map(function (e) {
          return "<tr><td>" + esc(e.seq) + "</td><td>" + esc(e.exp_item) +
            "</td><td class='num'>" + money(e.amount) + "</td></tr>";
        })));
    } else {
      h.push('<div class="hint">这张单没有明细行。</div>');
    }
    // 可编辑区
    if (editable) {
      h.push('<div class="sec">上传发票</div>');
      h.push('<div class="rb-field"><input type="file" id="bd-file" multiple ' +
        'accept=".pdf,.xml,.ofd,.jpg,.jpeg,.png"></div>' +
        '<div class="row-actions"><button class="btn pri" id="bd-upload">上传并挂到本单</button></div>');
      h.push('<div id="bd-msgs">' + (BD.loading ? '<div class="hint">' + esc(BD.loading) + "</div>" : "") +
        (BD.msgs || []).join("") + "</div>");
      h.push('<div class="sec">金额核对</div>');
      h.push('<div class="rb-amt">' +
        '<div><span>发票合计</span><b>' + money(b.recv_total) + "</b></div>" +
        '<div><span>当前报销</span><b>' + money(b.amount) + "</b></div></div>");
      h.push('<div class="row-actions">' +
        '<span class="hint" style="margin:0">报销金额</span>' +
        '<input id="bd-amount" value="' + esc(b.amount) + '" style="max-width:150px">' +
        '<button class="btn" id="bd-userecv">用发票合计</button>' +
        '<button class="btn pri" id="bd-set">保存</button></div>');
      h.push('<div class="sec">提交</div>');
      h.push('<div class="row-actions"><button class="btn" id="bd-check">提交前体检</button>' +
        '<button class="btn pri" id="bd-submit">提交审批</button></div>');
      h.push('<div id="bd-checkbox"></div>');
    }
    // 底部不再放「关闭」——右上角的 × 就是关闭（用户 2026-10-18 要求）
    BD.m.body.innerHTML = h.join("");
    if (editable) {
      document.getElementById("bd-upload").addEventListener("click", bdUpload);
      document.getElementById("bd-userecv").addEventListener("click", function () {
        document.getElementById("bd-amount").value =
          Number(BD.bill.recv_total || 0).toFixed(2);   // 只填入，点「保存」才写回
      });
      document.getElementById("bd-set").addEventListener("click", function () { bdSetAmount(); });
      document.getElementById("bd-check").addEventListener("click", bdCheck);
      document.getElementById("bd-submit").addEventListener("click", bdSubmit);
    }
  }

  function bdSetAmount(fixed) {
    if (!BD) return;
    var v = (fixed === undefined) ? parseFloat(document.getElementById("bd-amount").value) : Number(fixed);
    if (!isFinite(v)) { showErr({ message: "金额不是数字" }); return; }
    BD.busy = true;
    api("/api/reimburse/amount", { fid: BD.fid, amount: v, is_travel: !!BD.travel })
      .then(function (r) {
        BD.busy = false;
        if (!r.ok) { showErr(r.error); return; }
        BD.msgs.push('<div class="banner info">金额已保存为 ' + money((r.result || {}).amount) + "</div>");
        bdReload();
      });
  }

  function bdUpload() {
    if (!BD) return;
    var inp = document.getElementById("bd-file");
    var files = (inp && inp.files) ? Array.prototype.slice.call(inp.files) : [];
    if (!files.length) {
      BD.msgs.push('<div class="banner warn">请先选择发票文件。</div>');
      bdRender();
      return;
    }
    BD.busy = true;
    BD.loading = "上传中（" + files.length + " 个文件）…";   // 临时状态，不再塞进 msgs（否则永远留着）
    bdRender();
    (function next(i) {
      if (!BD) return;
      if (i >= files.length) { BD.busy = false; BD.loading = ""; bdReload(); return; }
      var f = files[i];
      var fd = new FormData();
      fd.append("file", f);
      fd.append("bill_fid", BD.fid);
      fd.append("bill_no", (BD.bill || {}).bill_no || "");
      fd.append("org_no", "");
      fd.append("item_name", "");
      if (BD.travel) fd.append("is_travel", "1");
      fetch("/api/reimburse/upload", {
        method: "POST", body: fd, headers: _portalHeaders({ "X-CSRF-Token": csrf() })
      }).then(function (r) { return r.json(); }).then(function (j) {
        if (!BD) return;
        if (!j.ok) {
          var e = j.error || {};
          BD.msgs.push('<div class="banner warn"><b>' + esc(f.name) + "：</b>" +
            esc(e.message || "失败") + (e.hint ? "<br>" + esc(e.hint) : "") + "</div>");
        } else if ((j.result || {}).route === "attach") {
          BD.msgs.push('<div class="banner info">📎 ' + esc(f.name) + " → 已作为附件挂到本单</div>");
        } else {
          var x = j.result || {};
          BD.msgs.push('<div class="banner info">✅ ' + esc(f.name) + " → 收票单 " +
            esc(x.recvin_bill_no) + "｜" + money(x.amount) + "</div>");
        }
        bdRender();
        next(i + 1);
      }).catch(function () {
        if (!BD) return;
        BD.msgs.push('<div class="banner err">' + esc(f.name) +
          "：请求超时或连接中断（发票云处理较慢时会这样），请单独重试这一个文件。</div>");
        bdRender();
        next(i + 1);
      });
    })(0);
  }

  function bdCheck() {
    if (!BD) return;
    api("/api/reimburse/precheck/" + BD.fid + (BD.travel ? "?travel=1" : ""), null, "GET")
      .then(function (r) {
        if (!BD) return;
        var c = r.check || {};
        var box = document.getElementById("bd-checkbox");
        if (!box) return;
        if (!r.ok) { box.innerHTML = errLine(r.error); return; }
        box.innerHTML = c.ok
          ? '<div class="banner info">✅ 可以提交（报销 ' + money(c.amount) + " ≤ 发票合计 " +
            money(c.recv_total) + "，收票 " + c.recv_rows + " 行）</div>"
          : '<div class="banner warn"><b>还不能提交：</b><br>' +
            (c.issues || []).map(esc).join("<br>") + "</div>";
      });
  }

  function bdSubmit() {
    if (!BD || BD.busy) return;
    if (!window.confirm("提交后单据会进入审批流，审批人将收到待办。\n确认提交吗？")) return;
    BD.busy = true;
    var fid = BD.fid, travel = BD.travel;
    api("/api/reimburse/submit", { fid: fid, confirm: "SUBMIT", is_travel: !!travel })
      .then(function (r) {
        if (!BD) return;
        BD.busy = false;
        if (!r.ok) { showErr(r.error); return; }
        var x = r.result || {};
        BD.m.close();
        rbLoadBills("all");
        var nb = document.createElement("div");
        nb.className = "banner info";
        nb.innerHTML = "✅ 已提交：" + esc(x.before_status) + " → <b>" + esc(x.after_status) +
          "</b>｜单号 " + esc(x.bill_no);
        var host = $("v-bills");
        if (host) host.insertBefore(nb, host.firstChild);
      });
  }

  /* 列表里直接删（不用先打开详情） */
  function rbDeleteByFid(fid, billNo, travel) {
    var m = openModal("删除报销单",
      '<div class="banner warn">确定删除 <b>' + esc(billNo || ("FID " + fid)) +
      "</b> 吗？<br>删除不可恢复（金蝶没有回收站）。</div>" +
      '<div class="hint">典型场景：一张申请单下推了多张报销单，把重复的那几张删掉。</div>' +
      '<div class="row-actions"><button class="btn danger" id="dl2-ok">确认删除</button>' +
      '<button class="btn" id="dl2-no">取消</button></div>');
    document.getElementById("dl2-no").addEventListener("click", m.close);
    document.getElementById("dl2-ok").addEventListener("click", function () {
      var btn = this;
      btn.disabled = true;
      btn.textContent = "删除中…";
      api("/api/reimburse/delete", { bill_fid: fid, is_travel: !!travel }).then(function (r) {
        m.close();
        if (!r.ok) { showErr(r.error); return; }
        if (BD && String(BD.fid) === String(fid)) BD.m.close();
        rbLoadBills("all");
        var nb = document.createElement("div");
        nb.className = "banner info";
        nb.textContent = "已删除报销单 " + ((r.result || {}).bill_no || billNo || "");
        var host = $("v-bills");
        if (host) host.insertBefore(nb, host.firstChild);
      });
    });
  }

  /* ---------------- ③ 申请单只读查看 ---------------- */
  function rbViewRequest(it) {
    var travel = it.kind === "出差申请";
    var m = openModal("申请单 " + it.bill_no, '<div class="hint">正在读取申请单内容…</div>');
    api("/api/reimburse/request/" + encodeURIComponent(it.fid || it.bill_no) +
        (travel ? "?travel=1" : ""), null, "GET").then(function (r) {
      if (!r.ok) { m.body.innerHTML = errLine(r.error); return; }
      var q = r.request || {};
      var h = ['<div class="pd-sum">' +
        "<div>申请人<b>" + esc(q.staff || it.staff) + "</b></div>" +
        "<div>日期<b>" + esc(q.date || it.date) + "</b></div>" +
        "<div>组织<b>" + esc((q.org_no || it.org_no) + " " + (q.org_name || it.org_name)) + "</b></div>" +
        "<div>状态<b>" + esc(stText(q.status)) + "</b></div>" +
        "<div>申请金额<b>" + money(q.amount || it.amount) + "</b></div>" +
        "<div>是否借款<b>" + (q.is_borrow || it.is_borrow ? "是" : "否") + "</b></div>" +
        ((q.reason || it.reason) ? '<div style="grid-column:1/-1">事由<b style="font-weight:400">' +
          esc(q.reason || it.reason) + "</b></div>" : "") + "</div>"];
      h.push('<div class="sec">费用明细 <span class="sub">' +
        (q.entries || []).length + " 行</span></div>");
      h.push(table([{ t: "#" }, { t: "费用项目" }, { t: "明细金额", num: 1 },
                    { t: "已下推", num: 1 }, { t: "本次还可下推", num: 1 }],
        (q.entries || []).map(function (e) {
          var left = (Number(e.amount) || 0) - (Number(e.pushed) || 0);
          return "<tr><td>" + esc(e.seq) + "</td><td>" + esc(e.item) +
            "</td><td class='num'>" + money(e.amount) + "</td><td class='num'>" +
            money(e.pushed) + "</td><td class='num'><b>" + money(left) + "</b></td></tr>";
        })));
      h.push('<div class="hint">「已下推」是这个费用项目已经推成报销单的金额。' +
        "已全额下推的申请单再推会生成重复报销单——需要的话在报销单列表里删掉即可。</div>");
      h.push('<div class="hint">申请单已通过审批，是报销的审批依据，门户只提供查看、不提供编辑。' +
        "要改内容请在金蝶里操作（作废后重提），避免审批依据与报销单不一致。</div>");
      h.push('<div class="row-actions"><button class="btn pri" id="vr-push">下推</button></div>');
      m.body.innerHTML = h.join("");
      document.getElementById("vr-push").addEventListener("click", function () {
        m.close();
        rbPushDialog(it, travel);
      });
    });
  }

  /* ---- 页 3：发票云配置（公司级 API Key；按租户隔离；支持多家开票主体） ---------------- */
  function loadPzCfg() {
    var root = $("v-pzcfg");
    root.innerHTML = skeleton("pzcfg");
    prog(true, "正在取数，请耐心等待…", 8);
    api("/api/reimburse/status", null, "GET").then(function (st) {
      R.status = st;
      var h = [];
      h.push(rbNoticeIfOff(st));
      if (!st.enabled) { prog(false);
      root.innerHTML = h.join(""); return; }
      h.push('<div id="pz-body"><div class="hint">加载中…</div></div>');
      prog(false);
      root.innerHTML = h.join("");
      api("/api/reimburse/piazzone-config?orgs=" + encodeURIComponent(
        JSON.stringify((S.orgs || []).map(function (o) {
          return { number: o.number, name: o.name };
        }))), null, "GET").then(function (r) {
        if (!r.ok) { showErr(r.error); $("pz-body").innerHTML = '<div class="hint">读取失败</div>'; return; }
        pzRender(r.config || {});
      });
    }).catch(function () {
      root.innerHTML = '<div class="banner warn">连接失败，请刷新重试。</div>';
    });
  }

  var PZC = { code: "", name: "", can_edit: false, admins: [], companies: [],
              draft: [], lastCfg: null };

  function pzRender(c, keepDraft) {
    PZC.lastCfg = c || PZC.lastCfg || {};
    if (!keepDraft) PZC.draft = [];
    c = PZC.lastCfg;
    PZC = { code: c.tenant || "", name: c.tenant_name || "", can_edit: !!c.can_edit,
            admins: c.admins || [], companies: c.companies || [],
            draft: PZC.draft, lastCfg: c };
    var list = PZC.companies.concat(PZC.draft);
    var h = [];
    if (!PZC.code) {
      h.push('<div class="banner warn">当前登录没有绑定公司 —— 请用本公司的专属链接（带 ?c= 的那条）登录后再配置发票云。</div>');
      $("pz-body").innerHTML = h.join("");
      return;
    }
    if (!c.can_edit) h.push('<div class="banner warn">只读 · 请联系公司管理员维护配置</div>');
    // ★ 2026-10-19：改成**以组织为主键**的表格（用户要求：组织是自动读出来的，
    //   不该让人记 101 这种编码；每行填「这个组织用谁开票」即可）
    var orgs = (c.orgs || []);
    var cosAll = PZC.companies || [];
    function splitOrgs(v) {
      return String(v || "").replace(/，/g, ",").split(",").map(function (x) { return x.trim(); }).filter(Boolean);
    }
    function coForOrg(no) {
      // 只按已登记的组织回填；**不再按"只有一家主体"把值填满所有行**
      // （用户 2026-10-19：默认应该是空的，填满反而看不清哪行真配过）
      return cosAll.filter(function (x) { return splitOrgs(x.kingdee_orgs).indexOf(no) >= 0; })[0] || null;
    }
    var unassigned = cosAll.filter(function (x) { return !String(x.kingdee_orgs || "").trim(); });
    if (!orgs.length) {
      h.push('<div class="banner warn">还没有读到组织列表。请先在门户里登录一次（会自动发现组织），' +
        "再回来配置。</div>");
    } else {
      h.push('<div class="sec" style="margin:4px 0 10px">按组织配置开票主体 ' +
        "</div>");
      var rowsHtml = orgs.map(function (o) {
        var co = coForOrg(o.no) || {};
        var secPh = co.has_secret ? ("已保存（尾 " + (co.secret_tail || "") + "）· 留空不改") : "发票云给的 secret";
        return '<tr data-id="' + esc(co.id || "") + '" data-org="' + esc(o.no) + '">' +
          '<td class="mono">' + esc(o.no) + "</td>" +
          '<td class="pz-orgname" title="' + esc(o.name || "") + '">' + esc(o.name || "—") + "</td>" +
          '<td><input data-f="ghf_mc" value="' + esc(co.ghf_mc || "") + '" placeholder="购方企业全称"' +
          ' autocomplete="off" name="pz-ghf-' + esc(o.no) + '-x"></td>' +
          '<td><input data-f="tax_no" value="' + esc(co.tax_no || "") + '" placeholder="购方税号"' +
          ' autocomplete="off" name="pz-tax-' + esc(o.no) + '-x"></td>' +
          '<td><input data-f="client_id" value="' + esc(co.client_id || "") + '" placeholder="发票云 client_id"' +
          ' autocomplete="off" name="pz-cid-' + esc(o.no) + '-x"></td>' +
          '<td><input data-f="client_secret" type="password" autocomplete="new-password"' +
          ' name="pz-sec-' + esc(o.no) + '-x" placeholder="' + esc(secPh) + '"></td>' +

          "</tr>";
      }).join("");
      h.push('<div class="pz-table-wrap"><table class="pz-table"><thead><tr>' +
        "<th>组织</th><th>组织名称</th><th>购方企业全称</th><th>购方税号</th>" +
        "<th>client_id</th><th>client_secret</th></tr></thead><tbody>" + rowsHtml +
        "</tbody></table></div>");
      if (PZC.can_edit) {
        h.push('<div class="row-actions" style="margin:10px 0 18px">' +
          '<button class="btn pri" id="pz-save-orgs">保存配置</button>' +
          '<button class="btn" id="pz-test">测试连接</button>' +
          "</div>");
      }
    }
    if (unassigned.length) {
      h.push('<div class="sec">已保存的其他开票主体</div>');
      unassigned.forEach(function (co) { h.push(pzCoCard(co, PZC.companies.indexOf(co))); });
    }
    h.push('<div id="pz-msg"></div>');
    // 公网模式的管理员和凭据设置不适用于各自电脑独立运行的本地版。
    if (!CFG.localMode) {
    h.push('<div class="sec">凭据模式</div>');
    h.push('<div class="pz-card"><div class="hint" style="margin:0 0 8px">当前：<b>' +
      (c.cred_mode === "memory" ? "内存模式" : "加密落盘") + "</b>" +
      (c.cred_mode_own === "inherit" ? "（跟随全局默认）" : "（本公司自设）") + "<br>" +
      "<b>加密落盘</b>：服务重启也能免登录；防的是文件被误拷/误传/写进日志，不防已经拿到整台机器的人。<br>" +
      "<b>内存模式</b>：登录凭据完全不落盘，重启后需重新登录。</div>" +
      '<div class="row-actions">' +
      '<button class="btn' + (c.cred_mode_own === "memory" ? " pri" : "") + '" data-pzm="memory">内存模式</button>' +
      '<button class="btn' + (c.cred_mode_own === "disk" ? " pri" : "") + '" data-pzm="disk">加密落盘</button>' +
      '<button class="btn' + (c.cred_mode_own === "inherit" ? " pri" : "") + '" data-pzm="inherit">跟随全局</button>' +
      "</div></div>");
    // 移交公司管理员（只有当前管理员看得到）
    if (PZC.can_edit) {
      h.push('<div class="sec">公司管理员 <span class="sub">可移交</span></div>');
      h.push('<div class="pz-card"><div class="rb-field"><label>移交给（金蝶账号）</label>' +
        '<input id="pz-newadmin" placeholder="输入接手人的金蝶登录账号"></div>' +
        '<div class="row-actions"><button class="btn" id="pz-transfer">移交管理员</button></div>' +
        '</div>');
    }
    } else {
      h.push('<div class="hint">发票云授权保存在本机，不会自动同步到其他电脑。金蝶账号密码仅在本次运行的内存中使用。</div>');
    }
    $("pz-body").innerHTML = h.join("");
    if (!PZC.can_edit) $("pz-body").querySelectorAll("input,button[data-pzm]").forEach(function (el) { el.disabled = true; });
    var testBtn = $("pz-test");
    if (testBtn) testBtn.addEventListener("click", function () {
      var b4 = this; b4.disabled = true; b4.textContent = "测试中…";
      api("/api/reimburse/piazzone-test", {}).then(function (r) {
        b4.disabled = false; b4.textContent = "测试连接";
        var box3 = $("pz-msg");
        if (!box3) return;
        if (!r.ok) { box3.innerHTML = errLine(r.error); return; }
        var cp = r.company || {};
        var html = (r.passed
          ? '<div class="banner info">✅ 发票云连通正常</div>'
          : '<div class="banner warn"><b>测试未通过</b>，原因见下：</div>') +
          '<div class="hint" style="margin:0 0 10px">本次用的主体：<b>' + esc(cp.ghf_mc || "—") +
          "</b> · 税号 <b>" + esc(cp.tax_no || "—") + "</b><br>" +
          "client_id：<b style=\"word-break:break-all\">" + esc(cp.cid_full || cp.client_id || "—") +
          "</b>（" + (cp.cid_len || 0) + " 位）· secret " + (cp.sec_len || 0) + " 位" +
          ((cp.cid_len || 0) > 64 || ((cp.sec_len || 0) > 0 && (cp.cid_len || 0) > (cp.sec_len || 0) * 1.5)
            ? '<br><b style="color:var(--red)">⚠️ client_id 长度异常，很可能把 secret 填到了 client_id 一栏</b>'
            : "") + "</div>";
        (r.steps || []).forEach(function (st) {
          html += '<div class="pz-stepres ' + (st.ok ? "ok" : "bad") + '">' +
            '<b>' + (st.ok ? "✓" : "✗") + " " + esc(st.step) + "</b>" +
            '<div class="hint" style="margin:4px 0 0;white-space:pre-wrap">' +
            esc(st.detail || "") + "</div>" +
            (st.raw ? '<details style="margin-top:6px"><summary style="cursor:pointer;' +
              'font-size:11.5px;color:var(--t3)">原始返回（发给发票云客服看）</summary>' +
              '<pre style="margin:6px 0 0;padding:8px;background:#f7f8fc;border-radius:8px;' +
              'font-size:11px;white-space:pre-wrap;word-break:break-all;color:var(--t2)">' +
              esc(st.raw) + "</pre></details>" : "") + "</div>";
        });
        box3.innerHTML = html;
      }).catch(function () { b4.disabled = false; b4.textContent = "测试连接"; });
    });
    var saveOrgs = $("pz-save-orgs");
    if (saveOrgs) saveOrgs.addEventListener("click", function () {
      var rows = [];
      Array.prototype.forEach.call($("pz-body").querySelectorAll(".pz-table tbody tr"), function (tr) {
        var row = { org_no: tr.dataset.org, id: tr.dataset.id };
        Array.prototype.forEach.call(tr.querySelectorAll("input[data-f]"), function (inp) {
          row[inp.dataset.f] = inp.value.trim();
        });
        rows.push(row);
      });
      saveOrgs.disabled = true; saveOrgs.textContent = "保存中…";
      api("/api/reimburse/piazzone-config", {
        action: "save_orgs", rows: rows, revision: PZC.lastCfg.revision,
        orgs: (S.orgs || []).map(function (o) { return { number: o.number, name: o.name }; })
      }).then(function (r) {
        saveOrgs.disabled = false; saveOrgs.textContent = "保存配置";
        var box2 = $("pz-msg");
        if (!r.ok) { if (box2) box2.innerHTML = errLine(r.error); return; }
        PZC.lastCfg = r.config;
        pzRender(r.config, false);
        var host = $("pz-body");
        if (r.warn) {                                  // 后端发现有空 secret
          var wb = document.createElement("div");
          wb.className = "banner warn";
          wb.textContent = "⚠️ " + r.warn;
          if (host) host.insertBefore(wb, host.firstChild);
        }
        var nb = document.createElement("div");
        nb.className = "banner info";
        nb.textContent = "✅ 已保存。上传发票时会按单据组织选对应主体；没配的组织用默认主体并提示。";
        if (host) host.insertBefore(nb, host.firstChild);
      }).catch(function () {
        saveOrgs.disabled = false; saveOrgs.textContent = "保存配置";
      });
    });
    Array.prototype.forEach.call($("pz-body").querySelectorAll("button[data-pzsave]"), function (b) {
      b.addEventListener("click", function () { pzSaveCo(b); });
    });
    Array.prototype.forEach.call($("pz-body").querySelectorAll("button[data-pzdel]"), function (b) {
      b.addEventListener("click", function () {
        var nm = b.dataset.pzname || "这家";
        if (!window.confirm("删除「" + nm + "」的发票云授权？删完不能恢复。")) return;
        api("/api/reimburse/piazzone-config", { action: "delete", id: b.dataset.pzdel, revision: PZC.lastCfg.revision }).then(function (r) {
          if (!r.ok) { $("pz-msg").innerHTML = errLine(r.error); return; }
          pzRender(r.config || {});
          $("pz-msg").innerHTML = '<div class="banner info">✅ 已删除。</div>';
        });
      });
    });
    Array.prototype.forEach.call($("pz-body").querySelectorAll("button[data-pzdraft]"), function (b) {
      b.addEventListener("click", function () {
        // 还没保存的空卡：本地移除即可，不调接口
        PZC.draft.splice(Number(b.dataset.pzdraft) - PZC.companies.length, 1);
        pzRender(null, true);
      });
    });
    Array.prototype.forEach.call($("pz-body").querySelectorAll("button[data-pzuse]"), function (b) {
      b.addEventListener("click", function () {
        api("/api/reimburse/piazzone-config", { action: "use", id: b.dataset.pzuse, revision: PZC.lastCfg.revision }).then(function (r) {
          if (!r.ok) { $("pz-msg").innerHTML = errLine(r.error); return; }
          pzRender(r.config || {});
          $("pz-msg").innerHTML = '<div class="banner info">✅ 已设为默认。</div>';
        });
      });
    });
    Array.prototype.forEach.call($("pz-body").querySelectorAll("button[data-pzm]"), function (b) {
      b.addEventListener("click", function () {
        api("/api/reimburse/piazzone-mode", { credential_mode: b.dataset.pzm }).then(function (r) {
          if (!r.ok) { $("pz-msg").innerHTML = errLine(r.error); return; }
          pzRender(r.config || {});
          $("pz-msg").innerHTML = '<div class="banner info">✅ 凭据模式已切换。</div>';
        });
      });
    });
    var tr = $("pz-transfer");
    if (tr) tr.addEventListener("click", function () {
      var u = $("pz-newadmin").value.trim();
      if (!u) { $("pz-msg").innerHTML = '<div class="hint">请填写接手人的账号</div>'; return; }
      if (!window.confirm("把本公司管理员移交给 " + u + "？你之后将无法再修改。")) return;
      api("/api/reimburse/piazzone-transfer", { new_admin: u }).then(function (r) {
        if (!r.ok) { $("pz-msg").innerHTML = errLine(r.error); return; }
        pzRender(r.config || {});
        $("pz-msg").innerHTML = '<div class="banner info">✅ 已移交给 ' + esc(u) + "。</div>";
      });
    });
  }

  function pzCoCard(co, idx, isNew) {
    var dis = PZC.can_edit ? "" : " disabled";
    var h = ['<div class="pz-co card' + (co.is_default ? " is-default" : "") +
             (isNew ? " is-new" : "") + '">'];
    h.push('<div class="pz-co-head"><div class="pz-co-title">' +
      (co.ghf_mc ? esc(co.ghf_mc) : (isNew ? "新增主体（未保存）" : "未命名主体 " + (idx + 1))) +
      (co.is_default ? '<span class="badge badge-blue">默认</span>' : "") +
      (isNew ? '<span class="badge badge-amber">未保存</span>' : "") + "</div>" +
      (PZC.can_edit ?
        (isNew ? '<button class="btn danger-ghost" data-pzdraft="' + idx + '">移除</button>'
               : (PZC.companies.length > 1 ?
                 '<button class="btn danger-ghost" data-pzdel="' + esc(co.id) + '" data-pzname="' +
                 esc(co.ghf_mc || co.client_id || "这家") + '">删除</button>' : "")) : "") + "</div>");
    h.push('<div class="rb-field"><label>购方企业全称</label><input data-pz="ghf_mc" value="' +
      esc(co.ghf_mc || "") + '"' + dis + "></div>");
    h.push('<div class="rb-field"><label>购方税号</label><input data-pz="tax_no" value="' +
      esc(co.tax_no || "") + '"' + dis + "></div>");
    h.push('<div class="rb-field"><label>client_id</label><input data-pz="client_id" value="' +
      esc(co.client_id || "") + '"' + dis + "></div>");
    h.push('<div class="rb-field"><label>client_secret</label><input data-pz="client_secret" type="password" placeholder="' +
      (co.has_secret ? "已保存（尾 " + esc(co.secret_tail) + "）· 留空 = 不修改" : "发票云给的 client_secret") +
      '"' + dis + "></div>");
    var multi = (PZC.companies || []).length > 1;
    h.push('<div class="rb-field"><label>对应金蝶组织 ' +
      (multi ? '<span class="req">多家主体时必填</span>' : '<span class="opt">单家可留空</span>') +
      "</label>" +
      '<input data-pz="kingdee_orgs" value="' + esc(co.kingdee_orgs || "") +
      '" placeholder="如 101,102"' + dis + ">" +
      (multi
        ? '<div class="hint"><b>这一项决定"哪个组织的发票用这家主体查验"。</b>' +
          "金蝶里没有这个对应关系，只能在这里配一次；两家都留空时就无法判断该用哪家，上传会报错。</div>"
        : '<div class="hint">只有一家主体时可以留空——所有组织都用它。</div>') +
      "</div>");
    // 组织点选：直接点组织编码，避免手敲
    var orgs = (S.orgs || []).slice(0, 20);
    if (orgs.length) {
      h.push('<div class="rb-field"><label>点选组织 <span class="opt">点一下追加到上面</span></label>' +
        '<div class="row-actions" style="margin:0;flex-wrap:wrap;gap:6px">' +
        orgs.map(function (o) {
          return '<button type="button" class="btn" data-org-pick="' + esc(o.number) + '"' +
            ' style="padding:4px 10px;font-size:12px">' + esc(o.number) +
            (o.name ? " " + esc(String(o.name).slice(0, 8)) : "") + "</button>";
        }).join("") + "</div></div>");
    }
    if (PZC.can_edit) {
      h.push('<div class="row-actions"><button class="btn pri" data-pzsave="' + esc(co.id) +
        '" data-pznew="' + (isNew ? "1" : "0") + '">保存这家</button>' +
        (co.is_default || isNew ? "" : '<button class="btn" data-pzuse="' + esc(co.id) + '">设为默认</button>') +
        "</div>");
    }
    h.push("</div>");
    return h.join("");
  }

  function pzSaveCo(btn) {
    var card = btn.closest(".pz-co");
    if (!card) return;
    var body = { action: "save", id: btn.dataset.pzsave || "",
                 "new": btn.dataset.pznew === "1", revision: PZC.lastCfg.revision };
    Array.prototype.forEach.call(card.querySelectorAll("input[data-pz]"), function (inp) {
      body[inp.dataset.pz] = inp.value.trim();
    });
    btn.disabled = true; btn.textContent = "保存中…";
    api("/api/reimburse/piazzone-config", body).then(function (r) {
      btn.disabled = false; btn.textContent = "保存这家";
      if (!r.ok) { $("pz-msg").innerHTML = errLine(r.error); return; }
      pzRender(r.config || {});
      $("pz-msg").innerHTML = '<div class="banner info">✅ 已保存' +
        (r.claimed ? "，你已成为本公司管理员" : "") + "。全公司都能用了。</div>";
    });
  }

  /* ================================================================
     看板 v2：卡片导航 + 子页面（用户 2026-10-16：卡片点击后是新页面那种）
     - 每个看板 = 主页（指标卡片网格）+ 若干子页（点击卡片进入）
     - 数据都在 S.lastData[view] 里，子页直接渲染，不重复请求
     - SUB[view] 记录当前子页 id；空 = 主页
     ================================================================ */
  var SUB = { expense: "", mgmt: "", sales: "", cash: "" };
  var VIEW_NAMES = { expense: "费用报销", mgmt: "管理会计", sales: "销售出库", cash: "现金流预测" };

  function subHead(view, title, desc) {
    return '<div class="sub-head">' +
      '<button type="button" class="btn sub-back" data-sub-back="1">' +
      '<svg class="icon"><use href="#i-arrow"/></svg>返回' + esc(VIEW_NAMES[view] || "看板") + '主页</button>' +
      '<div class="sub-title"><h2>' + esc(title) + "</h2>" +
      (desc ? '<p class="sub-desc">' + esc(desc) + "</p>" : "") + "</div></div>";
  }
  function wireSub(view, root) {
    Array.prototype.forEach.call(root.querySelectorAll("button[data-sub]"), function (b) {
      b.addEventListener("click", function () {
        SUB[view] = b.dataset.sub;
        rerenderView(view);
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
    });
    var back = root.querySelector("button[data-sub-back]");
    if (back) back.addEventListener("click", function () {
      SUB[view] = "";
      rerenderView(view);
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  /* ---------------- 费用报销看板 v2（补全：组织/账龄/状态 + 卡片导航） ---------------- */
  /* 费用报销：点费用类型 → 看这一类的金额与笔数（前端按单汇总，不额外请求） */
  function expensePickPanel(d, pick) {
    if (!pick || pick.indexOf("exp-t:") !== 0) return "";
    var k = pick.slice(6);
    var pie = d.exp_type_pie || [];
    var hit = pie.filter(function (x) { return String(x[0]) === k; })[0];
    if (!hit) return "";
    var total = pie.reduce(function (a, x) { return a + (Number(x[1]) || 0); }, 0);
    var share = total ? (Number(hit[1]) / total * 100) : 0;
    return '<div class="pick-panel"><div class="pick-head"><b>' + esc(k) + "</b>" +
      '<button type="button" class="btn btn-ghost" id="pick-clear">取消筛选</button></div>' +
      '<div class="pick-grid">' +
      '<div><span>金额</span><b>' + money(hit[1]) + "</b></div>" +
      '<div><span>占比</span><b>' + num(share, 1) + "%</b></div>" +
      '<div><span>全部费用类型</span><b>' + pie.length + " 类</b></div>" +
      "</div></div>";
  }

  function renderExpense2(d, cacheAt, refreshing) {
    if (!d) return;
    S.lastData.expense = d;
    d = window.PortalScope.select("expense", d, S.selected, S.orgs);
    updateRefreshStatus();
    var root = $("v-expense");
    var h = [];
    if (d._emptyScope) { root.innerHTML = '<div class="empty">请在右上角选择组织</div>'; return; }
    var cov = d.coverage || {};
    if (cov.truncated > 0) {
      h.push('<div class="banner warn">覆盖度说明（安全阀触发）：组织过滤后 ' +
        num(cov.bills_after_org_filter, 0) + " 张单，本次仅分析前 " +
        num(cov.max_bills, 0) + " 张，未覆盖 " + num(cov.truncated, 0) +
        " 张。可在「数据源与口径」调整。</div>");
    }
    for (var e of (cov.header_errors || [])) {
      h.push('<div class="banner err">' + esc(e) + "</div>");
    }
    var m = d.metrics || {};
    paintWorkbook("expense", d, root, h);
  }

  /* ---------------- 管理会计全貌看板（卡片导航 + 12 源分页） ---------------- */
  var MGMT_TITLES = {
    bank: "头寸与科目余额核对", subject: "科目余额波动", est: "应付 vs 暂估差异",
    interdim: "往来科目核算维度", arhx: "应收核销对账", aphx: "应付核销对账",
    goods: "有货未回票", expense: "非货未回票", dash: "带横杠单号",
    recv: "收票单汇总", anom: "存货异常", invoice: "未审核发票",
  };
  var CAT_NAMES_FE = { "1122": "应收账款", "1123": "预付账款", "1221": "其他应收款",
                       "2203": "预收账款", "2202ZG": "应付暂估", "2202MX": "应付明细",
                       "2241": "其他应付款" };

  function renderMgmtFull(d, cacheAt, refreshing) {
    if (!d) return;
    S.lastData.mgmt = d;
    d = window.PortalScope.select("mgmt", d, S.selected, S.orgs);
    updateRefreshStatus();
    var root = $("v-mgmt");
    var h = [];
    if (d._emptyScope) { root.innerHTML = '<div class="empty">请在右上角选择组织</div>'; return; }
    for (var e of (d.errors || [])) {
      h.push('<div class="banner warn">采集失败：' + esc(e) + "</div>");
    }
    if (SUB.mgmt) {
      h.push(mgmtSubPage(d, SUB.mgmt));
      root.innerHTML = h.join("");
      wireSub("mgmt", root);
      return;
    }
    paintWorkbook("mgmt", d, root, h);
  }

  function mgmtSubPage(d, id) {
    var p = (d.pages || {})[id];
    var h = [subHead("mgmt", MGMT_TITLES[id] || id, (p || {}).note || "")];
    if (!p) {
      h.push('<div class="banner warn">该数据源本次没有采集到数据（失败原因见主页提示）。</div>');
      return h.join("");
    }
    if (id === "bank") {
      // 用户要求（2026-10-17）：这一页**只保留差异统计**；头寸余额在「现金流预测」看板看
      var fund = p.fund || {};
      var cross = p.cross_check || {};
      var both = cross.both || [];
      var diffRows = both.filter(function (r) { return Math.abs(Number(r.delta) || 0) > 0.5; });
      var diffSum = diffRows.reduce(function (a, r) { return a + Math.abs(Number(r.delta) || 0); }, 0);
      h.push('<div class="grid g4">' +
        kpi("差异组织", num(diffRows.length, 0) + " / " + num(both.length, 0),
          "头寸期末 vs 货币资金科目余额，差额 >0.5 元计差异", "red") +
        kpi("差额合计", money(diffSum), "差异绝对值之和（不做正负抵消）", "amber") +
        kpi("覆盖账户", num(fund.account_count, 0), num(fund.org_count, 0) + " 个组织", "gray") +
        kpi("口径", "头寸 ≠ 科目余额", "两者取数范围不同，只作交叉验证", "blue") + "</div>");
      h.push('<div class="hint">' + esc(cross.why || "") +
        "头寸余额与每日收支请看「现金流预测」看板；本页只看两套口径的差异。</div>");
      if (both.length) {
        h.push('<div class="sec">按组织的差异统计</div>');
        h.push(divergingBars(both.map(function (r) {
          return { label: r.org_name || r.org_number,
                   v: Number(r.delta) || 0,
                   note: "头寸 " + money(r.fund_end) + " · 科目 " + money(r.subject) };
        }), { unit: "元" }));
        h.push(table([{ t: "组织" }, { t: "头寸期末", num: 1 }, { t: "科目余额", num: 1 },
                      { t: "差额", num: 1 }, { t: "结论" }],
          both.map(function (r) {
            var bad = Math.abs(Number(r.delta) || 0) > 0.5;
            return "<tr><td>" + esc(r.org_name || r.org_number) + "</td><td class='num'>" +
              money(r.fund_end) + "</td><td class='num'>" + money(r.subject) +
              "</td><td class='num'" + (bad ? " style='color:var(--red)'" : "") + ">" +
              money(r.delta) + "</td><td>" +
              (bad ? "<span class='badge badge-amber'>有差异</span>"
                   : "<span class='badge badge-gray'>一致</span>") + "</td></tr>";
          })));
      } else {
        h.push('<div class="banner warn">没有取得可对照的组织数据，暂时无法判断是否一致。</div>');
      }
      if ((cross.only_in_fund || []).length || (cross.only_in_subject || []).length) {
        h.push('<div class="sec">单边组织（只有一侧有数据）</div>');
        h.push(table([{ t: "组织" }, { t: "侧" }, { t: "金额", num: 1 }],
          (cross.only_in_fund || []).map(function (r) {
            return "<tr><td>" + esc(r.org_name || r.org_number) + "</td><td>仅头寸</td><td class='num'>" +
              money(r.fund_end) + "</td></tr>";
          }).concat((cross.only_in_subject || []).map(function (r) {
            return "<tr><td>" + esc(r.org_name || r.org_number) + "</td><td>仅科目余额</td><td class='num'>" +
              money(r.fund_balance) + "</td></tr>";
          }))));
      }
    } else if (id === "subject") {
      var swings = (p.swings || []);
      // 按科目类型统计（用户口径：资产/负债/收入/费用/权益，正=增加、负=减少）
      var typeAgg = {};
      swings.forEach(function (r) {
        var t = r.type || "其他";
        typeAgg[t] = (typeAgg[t] || 0) + (Number(r.delta) || 0);
      });
      var typeRows = Object.keys(typeAgg).map(function (t) {
        return { label: t + "类", v: typeAgg[t] };
      }).sort(function (a, b) { return Math.abs(b.v) - Math.abs(a.v); });
      h.push('<div class="grid g4">' +
        kpi("波动科目", num(swings.length, 0) + " 个", "本月末余额 vs 上月末，|变动|≥5000", "blue") +
        kpi("增长最多类型", typeRows.length ? typeRows[0].label : "—",
          typeRows.length ? money(typeRows[0].v) : "", "red") +
        kpi("涉及组织", num((function () {
          var seen = {}, n = 0;
          swings.forEach(function (r) {
            var k = r.org_number || r.org_name || "?";
            if (!seen[k]) { seen[k] = 1; n += 1; }
          });
          return n;
        })(), 0), "按账簿所属组织", "gray") +
        kpi("口径", "按科目类型", "资产/成本/费用=借−贷；负债/权益/收入=贷−借", "green") + "</div>");
      if (typeRows.length) {
        h.push('<div class="sec">按科目类型的余额变动（正=余额增加，负=减少）</div>');
        h.push(divergingBars(typeRows));
      }
      h.push('<div class="sec">波动明细（按 |变动| 降序，只列前 40 条）</div>');
      h.push(table([{ t: "类型" }, { t: "科目" }, { t: "名称" }, { t: "组织" },
                    { t: "本月末余额", num: 1 }, { t: "上月末余额", num: 1 }, { t: "增减", num: 1 }],
        swings.map(function (r) {
          var up = Number(r.delta) > 0;
          return "<tr><td><span class='badge badge-gray'>" + esc(r.type || "其他") +
            "</span></td><td class='mono'>" + esc(r.code) + "</td><td>" + esc(r.name) +
            "</td><td>" + esc(r.org_name || r.org_number) + "</td><td class='num'>" +
            money(r.cur) + "</td><td class='num'>" + money(r.prev) +
            "</td><td class='num' style='color:" + (up ? "var(--red)" : "var(--green)") +
            "'>" + (up ? "▲ 增加 " : "▼ 减少 ") + money(Math.abs(r.delta)) + "</td></tr>";
        })));
    } else if (id === "est") {
      h.push('<div class="sec">按组织汇总</div>');
      h.push(table([{ t: "组织" }, { t: "供应商", num: 1 }, { t: "不含税（应付单）", num: 1 },
                    { t: "暂估（科目）", num: 1 }, { t: "差额", num: 1 }],
        (p.org_summary || []).map(function (r) {
          return "<tr><td>" + esc(r.org) + "</td><td class='num'>" + num(r.suppliers, 0) +
            "</td><td class='num'>" + money(r.notax) + "</td><td class='num'>" +
            money(r.est) + "</td><td class='num'><b>" + money(r.diff) + "</b></td></tr>";
        })));
      h.push('<div class="sec">按供应商明细（差额≠0，按累计差额降序）</div>');
      h.push(table([{ t: "组织" }, { t: "供应商" }, { t: "累计不含税", num: 1 },
                    { t: "累计暂估", num: 1 }, { t: "累计差额", num: 1 }, { t: "当期差额", num: 1 }],
        (p.detail || []).map(function (r) {
          return "<tr><td>" + esc(r.org) + "</td><td>" + esc(r.supplier) +
            "</td><td class='num'>" + money(r.notax) + "</td><td class='num'>" +
            money(r.est_cum) + "</td><td class='num'><b>" + money(r.diff_cum) +
            "</b></td><td class='num'>" + money(r.diff_cur) + "</td></tr>";
        })));
    } else if (id === "interdim") {
      (p.orgs || []).forEach(function (o) {
        var sum = (o.summary || []);
        h.push('<div class="sec">组织 ' + esc(o.org) + " · 往来科目净额（借−贷）</div>");
        if (sum.length) {
          h.push(divergingBars(sum.map(function (r) {
            return { label: r.name + "（" + r.cat + "）", v: Number(r.net) || 0 };
          })));
        } else {
          h.push('<div class="hint">该组织往来科目无余额。</div>');
        }
        h.push('<div class="sec">跨科目核算维度（同一往来方在 ≥2 个科目有余额）· ' +
          num((o.rows || []).length, 0) + " 个</div>");
        if (!(o.rows || []).length) {
          h.push('<div class="hint">没有跨科目挂账的往来方。</div>');
          return;
        }
        h.push('<div class="dim-grid">' + (o.rows || []).map(function (r) {
          var cats = Object.keys(r.cats || {});
          return '<div class="dim-card">' +
            '<div class="dim-head"><span class="dim-name">' + esc(r.dim) + "</span>" +
            '<span class="dim-total ' + ((Number(r.net) || 0) >= 0 ? "pos" : "neg") + '">' +
            money(r.net) + "</span></div>" +
            '<div class="dim-cats">' + cats.map(function (c) {
              return '<span class="dim-chip">' + esc(CAT_NAMES_FE[c] || c) +
                '<b>' + money(r.cats[c]) + "</b></span>";
            }).join("") + "</div></div>";
        }).join("") + "</div>");
      });
    } else if (id === "arhx" || id === "aphx") {
      h.push('<div class="sec">未结算 vs 未核销（按往来单位，差异≠0）</div>');
      h.push(table([{ t: "组织" }, { t: (id === "arhx" ? "客户" : "供应商") },
                    { t: "单据未结算", num: 1 }, { t: "收/付款未核销", num: 1 },
                    { t: "差异", num: 1 }],
        (p.recon || []).map(function (r) {
          var bad = Math.abs(Number(r.diff) || 0) > 0.5;
          return "<tr><td>" + esc(r.org) + "</td><td>" + esc(r.party) +
            "</td><td class='num'>" + money(r.bill) + "</td><td class='num'>" +
            money(r.pay) + "</td><td class='num'" +
            (bad ? " style='color:var(--red)'" : "") + ">" + money(r.diff) + "</td></tr>";
        })));
      h.push('<div class="sec">正负并存（同一单位同时有正负金额）</div>');
      h.push(table([{ t: "组织" }, { t: (id === "arhx" ? "客户" : "供应商") },
                    { t: "正金额", num: 1 }, { t: "负金额", num: 1 }],
        (p.posneg || []).map(function (r) {
          return "<tr><td>" + esc(r.org) + "</td><td>" + esc(r.party) +
            "</td><td class='num'>" + money(r.pos) + "</td><td class='num'>" +
            money(r.neg) + "</td></tr>";
        })));
    } else if (id === "goods") {
      if ((p.by_supplier || []).length) {
        h.push('<div class="sec">按供应商的差额分布（已结算−开票，Top15）</div>');
        h.push(divergingBars((p.by_supplier || []).slice(0, 15).map(function (r) {
          return { label: r.supplier, v: Number(r.diff) || 0, note: num(r.n, 0) + " 行" };
        })));
      }
      h.push('<div class="sec">按供应商汇总</div>');
      h.push(table([{ t: "供应商" }, { t: "订单行", num: 1 }, { t: "差额（已结算−开票）", num: 1 }],
        (p.by_supplier || []).map(function (r) {
          return "<tr><td>" + esc(r.supplier) + "</td><td class='num'>" + num(r.n, 0) +
            "</td><td class='num'><b>" + money(r.diff) + "</b></td></tr>";
        })));
      h.push('<div class="sec">订单明细（差额≠0，按差额降序）</div>');
      h.push(table([{ t: "订单编号" }, { t: "日期" }, { t: "供应商" }, { t: "物料" },
                    { t: "入库金额", num: 1 }, { t: "已结算", num: 1 }, { t: "开票", num: 1 },
                    { t: "差额", num: 1 }],
        (p.detail || []).map(function (r) {
          return "<tr><td class='mono'>" + esc(r.bill_no) + "</td><td class='mono'>" +
            esc(r.date) + "</td><td>" + esc(r.supplier) + "</td><td>" + esc(r.material) +
            "</td><td class='num'>" + money(r.receive) + "</td><td class='num'>" +
            money(r.settled) + "</td><td class='num'>" + money(r.invoice) +
            "</td><td class='num'><b>" + money(r.diff) + "</b></td></tr>";
        })));
    } else if (id === "expense") {
      h.push('<div class="sec">未开票核销明细（按金额降序）</div>');
      h.push(table([{ t: "单据编号" }, { t: "日期" }, { t: "供应商" }, { t: "类型" },
                    { t: "价税合计", num: 1 }, { t: "未开票核销", num: 1 }],
        (p.detail || []).map(function (r) {
          return "<tr><td class='mono'>" + esc(r.bill_no) + "</td><td class='mono'>" +
            esc(r.date) + "</td><td>" + esc(r.supplier) + "</td><td>" + esc(r.type) +
            "</td><td class='num'>" + money(r.total) + "</td><td class='num'><b>" +
            money(r.noinv) + "</b></td></tr>";
        })));
    } else if (id === "dash") {
      var agg = {};
      (p.detail || []).forEach(function (r) {
        var k = r.side + " · " + (r.org || "未知");
        agg[k] = (agg[k] || 0) + (Number(r.amount) || 0);
      });
      var aggRows = Object.keys(agg).map(function (k) {
        return { label: k, v: agg[k] };
      }).sort(function (a, b) { return Math.abs(b.v) - Math.abs(a.v); }).slice(0, 12);
      if (aggRows.length) {
        h.push('<div class="sec">按 侧/组织 的金额分布</div>');
        h.push(divergingBars(aggRows));
      }
      h.push('<div class="sec">带横杠单号明细（已审核）</div>');
      h.push(table([{ t: "单号" }, { t: "侧" }, { t: "组织" }, { t: "往来单位" },
                    { t: "价税合计", num: 1 }],
        (p.detail || []).map(function (r) {
          return "<tr><td class='mono'>" + esc(r.bill_no) + "</td><td>" + esc(r.side) +
            "</td><td>" + esc(r.org) + "</td><td>" + esc(r.party) +
            "</td><td class='num'>" + money(r.amount) + "</td></tr>";
        })));
    } else if (id === "recv") {
      var rtype = {};
      (p.detail || []).forEach(function (r) {
        rtype[r.link_type || "未关联"] = (rtype[r.link_type || "未关联"] || 0) + (Number(r.total) || 0);
      });
      var rtypeRows = Object.keys(rtype).map(function (k) {
        return { label: k, v: rtype[k] };
      }).sort(function (a, b) { return Math.abs(b.v) - Math.abs(a.v); });
      if (rtypeRows.length) {
        h.push('<div class="sec">按关联单据类型的价税合计</div>');
        h.push(divergingBars(rtypeRows));
      }
      h.push('<div class="sec">按 组织/关联类型/状态 汇总</div>');
      h.push(table([{ t: "组织" }, { t: "关联单据" }, { t: "状态" }, { t: "笔数", num: 1 },
                    { t: "不含税", num: 1 }, { t: "价税合计", num: 1 }],
        (p.detail || []).map(function (r) {
          return "<tr><td>" + esc(r.org) + "</td><td>" + esc(r.link_type) + "</td><td>" +
            esc(r.status) + "</td><td class='num'>" + num(r.n, 0) +
            "</td><td class='num'>" + money(r.amount) + "</td><td class='num'>" +
            money(r.total) + "</td></tr>";
        })));
    } else if (id === "anom") {
      var an = p.anom || {};
      var st = p.stats || {};
      h.push('<div class="hint">扫描 ' + num(st.rows, 0) + " 行收发存（" +
        num(st.materials, 0) + " 个物料）</div>");
      [["anom1", "零数量有金额"], ["anom2", "有数量负金额"], ["anom3", "异常价格"]]
        .forEach(function (pair) {
          var rows = an[pair[0]] || [];
          h.push('<div class="sec">' + esc(pair[1]) + " · " + num(rows.length, 0) + " 项</div>");
          if (!rows.length) {
            h.push('<div class="hint">未发现。</div>');
            return;
          }
          h.push(table([{ t: "物料编码" }, { t: "物料名称" }, { t: "仓库" },
                        { t: "期末数量", num: 1 }, { t: "期末金额", num: 1 }, { t: "原因" }],
            rows.map(function (r) {
              return "<tr><td class='mono'>" + esc(r.code) + "</td><td>" + esc(r.name) +
                "</td><td>" + esc(r.stock) + "</td><td class='num'>" + num(r.end_qty) +
                "</td><td class='num'>" + money(r.end_amount) + "</td><td>" +
                esc(r.reason) + "</td></tr>";
            })));
        });
    } else if (id === "invoice") {
      var byForm = {};
      (p.detail || []).forEach(function (r) {
        byForm[r.form || "未知表单"] = (byForm[r.form || "未知表单"] || 0) + (Number(r.n) || 0);
      });
      var formRows = Object.keys(byForm).map(function (k) {
        return { label: k, v: byForm[k] };
      }).sort(function (a, b) { return Math.abs(b.v) - Math.abs(a.v); });
      if (formRows.length) {
        h.push('<div class="sec">按发票表单的未审核笔数</div>');
        h.push(divergingBars(formRows, { unit: "笔" }));
      }
      h.push('<div class="sec">未审核发票（按 表单/供应商/状态）</div>');
      h.push(table([{ t: "表单" }, { t: "供应商" }, { t: "状态" }, { t: "笔数", num: 1 }],
        (p.detail || []).map(function (r) {
          return "<tr><td>" + esc(r.form) + "</td><td>" + esc(r.supplier) + "</td><td>" +
            esc(r.status) + "</td><td class='num'>" + num(r.n, 0) + "</td></tr>";
        })));
    } else {
      h.push('<div class="banner warn">未知分页：' + esc(id) + "</div>");
    }
    return h.join("");
  }

  /* ---------------- 双向条形图（正负分色，用于科目净额/差异） ---------------- */
  function divergingBars(items, opts) {
    // items: [{label, v, note?}]  正=红（中文涨），负=绿
    opts = opts || {};
    if (!items.length) return '<div class="empty">当前范围暂无数据</div>';
    var max = Math.max.apply(null, items.map(function (x) { return Math.abs(Number(x.v) || 0); })) || 1;
    var unit = opts.unit || "元";
    return '<div class="dv-bars">' + items.map(function (x) {
      var v = Number(x.v) || 0;
      var w = Math.abs(v) / max * 50;
      var pos = v >= 0;
      return '<div class="dv-row">' +
        '<div class="dv-label" title="' + esc(x.label) + '">' + esc(x.label) +
        (x.note ? '<span>' + esc(x.note) + "</span>" : "") + "</div>" +
        '<div class="dv-track">' +
        '<span class="dv-zero"></span>' +
        '<span class="dv-bar ' + (pos ? "pos" : "neg") + '" style="' +
        (pos ? "left:50%;width:" + w.toFixed(2) + "%" : "right:50%;width:" + w.toFixed(2) + "%") +
        '"></span></div>' +
        '<div class="dv-val ' + (pos ? "pos" : "neg") + '">' + money(v) + "</div></div>";
    }).join("") + "</div>";
  }

  /* Dashboard snapshots live only in current-page memory. */
  var CACHE_PREFIX = "portal_cache:";

  function cacheKeyOf(view, extra) {
    return CACHE_PREFIX + view + ":" + (S.selected || []).join(",") + ":" + (extra || "");
  }

  function readCache() { return null; }
  function writeCache() {}
  try { Object.keys(localStorage).forEach(function (k) { if (k.indexOf(CACHE_PREFIX) === 0) localStorage.removeItem(k); }); } catch (e) {}

  function hhmm(at) {
    var d = new Date(at);
    function p(n) { return n < 10 ? "0" + n : "" + n; }
    return p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function cacheBar(view, at, leftover) {
    if (!at) return "";
    return '<div class="cache-bar" id="cache-bar-' + esc(view) + '">' +
      '<span class="cache-dot"></span>显示 <b>' + hhmm(at) + "</b> 的缓存结果，" +
      '<span class="cache-state">' + (leftover ? "后台刷新中…" : "数据未变化") + "</span>" +
      "</div>";
  }

  /* 缓存收尾：新数据与缓存是同一份（generated_at 相同）→ 保留"数据未变化"提示 */
  function settleCache(render, k, hit, res) {
    writeCache(k, res);
    if (hit && hit.data && res && res.generated_at && res.generated_at === hit.data.generated_at) {
      render(res, hit.at, false);
    } else {
      render(res);
    }
  }

  /* ---------------- 12 周曲线图（现金流预测用） ---------------- */
  function curveChart(weekLabels, series) {
    // series: [{name, values:[...], color, dash?, width?}]
    if (!weekLabels.length || !series.length) {
      return '<div class="empty">暂无曲线数据</div>';
    }
    var all = [];
    series.forEach(function (s) { all = all.concat(s.values); });
    var hi = Math.max.apply(null, all.concat([0])), lo = Math.min.apply(null, all.concat([0]));
    if (hi === lo) { hi = lo + 1; }
    var pad = (hi - lo) * 0.08;
    hi += pad; lo -= pad;
    var W = 640, H = 260, left = 64, top = 16, right = 96, bottom = 34;
    var range = hi - lo, plotH = H - top - bottom, plotW = W - left - right;
    function x(i) { return left + plotW * i / (weekLabels.length - 1 || 1); }
    function y(v) { return top + (hi - v) / range * plotH; }
    var grid = token("--chart-grid"), axis = token("--chart-axis");
    var out = ['<svg class="trend-chart" viewBox="0 0 ' + W + " " + H +
               '" role="img" aria-label="未来 12 周现金头寸预测">'];
    for (var n = 0; n <= 3; n++) {
      var v = lo + range * n / 3, yy = y(v);
      out.push('<line x1="' + left + '" y1="' + yy + '" x2="' + (W - right) + '" y2="' + yy +
        '" stroke="' + grid + '" stroke-opacity="0.55" stroke-width="0.8"' +
        ' stroke-dasharray="2 6"/><text x="' + (left - 10) +
        '" y="' + (yy + 4) + '" text-anchor="end" fill="' + axis + '" font-size="11.5">' +
        num(v / 10000, 1) + "</text>");
    }
    if (lo < 0 && hi > 0) {
      out.push('<line x1="' + left + '" y1="' + y(0) + '" x2="' + (W - right) +
        '" y2="' + y(0) + '" stroke="' + token("--red") +
        '" stroke-width="1.2" stroke-dasharray="6 4"/>');
    }
    series.forEach(function (s) {
      var pts = s.values.map(function (v, i) {
        return x(i).toFixed(1) + "," + y(v).toFixed(1);
      }).join(" ");
      out.push('<polyline points="' + pts + '" fill="none" stroke="' + s.color +
        '" stroke-width="' + (s.width || 1.8) + '" stroke-opacity="' + (s.dash ? 0.55 : 1) + '"' +
        (s.dash ? ' stroke-dasharray="4 5"' : "") + "/>");
      s.values.forEach(function (v, i) {
        out.push('<circle cx="' + x(i).toFixed(1) + '" cy="' + y(v).toFixed(1) +
          '" r="2.6" fill="' + s.color + '"><title>' + esc(s.name) + " " +
          esc(weekLabels[i]) + "：" + money(v) + "</title></circle>");
      });
      var lv = s.values[s.values.length - 1];
      out.push('<text x="' + (W - right + 6) + '" y="' + (y(lv) + 4) +
        '" fill="' + s.color + '" font-size="11">' + esc(s.name) + "</text>");
    });
    weekLabels.forEach(function (lab, i) {
      if (weekLabels.length <= 6 || i % 2 === 0 || i === weekLabels.length - 1) {
        out.push('<text x="' + x(i).toFixed(1) + '" y="' + (H - 12) +
          '" text-anchor="middle" fill="' + axis + '" font-size="11">' + esc(lab) + "</text>");
      }
    });
    out.push("</svg>");
    return out.join("");
  }

  /* ---------------- 销售出库看板 ---------------- */
  /* ---------------- 销售出库：CEO 视角（本月 / 累计） ---------------- */
  function deltaChip(pct, label) {
    if (pct == null || isNaN(pct)) return "";
    var up = Number(pct) >= 0;
    return '<span class="delta-chip ' + (up ? "up" : "down") + '">' +
      (up ? "▲ " : "▼ ") + num(Math.abs(pct), 1) + "% " + esc(label) + "</span>";
  }

  function salesHero(title, sub, cur, base, growth, growthLabel) {
    var maxv = Math.max(Number(cur.amount) || 0, Number(base.amount) || 0) || 1;
    var wCur = (Number(cur.amount) || 0) / maxv * 100;
    var wBase = (Number(base.amount) || 0) / maxv * 100;
    return '<div class="big-card">' +
      '<div class="big-head"><span class="big-title">' + esc(title) + "</span>" +
      '<span class="big-sub">' + esc(sub) + "</span></div>" +
      '<div class="big-val">' + money(cur.amount) + deltaChip(growth, growthLabel) + "</div>" +
      '<div class="compare">' +
      '<div class="cmp-row"><span>本期</span><i style="width:' + wCur.toFixed(1) +
      '%"></i><b>' + money(cur.amount) + "</b></div>" +
      '<div class="cmp-row base"><span>对比</span><i style="width:' + wBase.toFixed(1) +
      '%"></i><b>' + money(base.amount) + "</b></div></div>" +
      '<div class="big-foot">' + num(cur.bill_count, 0) + " 笔 · " +
      num(cur.customer_count, 0) + " 家客户 · 开票率 " + num(cur.invoice_rate, 1) +
      "% · 结算率 " + num(cur.settlement_rate, 1) + "%</div></div>";
  }

  function monthBars(items, curLabel, opt) {
    if (!items.length) return '<div class="empty">暂无月度数据</div>';
    opt = opt || {};
    var max = Math.max.apply(null, items.map(function (x) { return Number(x.v) || 0; })) || 1;
    return '<div class="m-bars">' + items.map(function (x) {
      var hgt = Math.max(3, (Number(x.v) || 0) / max * 140);
      var cur = String(x.label).indexOf(curLabel) >= 0;
      var key = opt.prefix ? opt.prefix + ":" + x.label : "";
      var on = key && opt.picked === key;
      return '<div class="m-col' + (cur ? " cur" : "") + (on ? " picked" : "") +
        (key ? " pickable" : "") + '"' + (key ? ' data-k="' + esc(key) + '"' : "") +
        ' title="' + esc(x.label) + "：" + money(x.v) + '"><i style="height:' +
        hgt.toFixed(0) + 'px"></i><span>' + esc(String(x.label).slice(2)) + "</span></div>";
    }).join("") + "</div>";
  }

  /* 图表点击后的详情面板（2026-10-20 交互第一步：点月份/客户 → 看这一项的构成） */
  function salesPickPanel(d, pick) {
    if (!pick || pick.indexOf("sal-") !== 0) return "";
    var t = pick.slice(4, 5), k = pick.slice(6);
    var rows = [], title = "";
    if (t === "m") {
      var hit = (d.monthly_trend || []).filter(function (x) { return String(x.month) === k; })[0];
      if (!hit) return "";
      title = k + " 月度明细";
      var rate = function (a, b) { return b ? (Number(a) / Number(b) * 100) : 0; };
      rows = [
        ["出库金额", money(hit.amount)],
        ["应收金额", money(hit.receivable_amount)],
        ["已开票", money(hit.invoice_amount) + "（" + num(rate(hit.invoice_amount, hit.amount), 1) + "%）"],
        ["已收款", money(hit.receipt_settlement_amount) + "（" + num(rate(hit.receipt_settlement_amount, hit.amount), 1) + "%）"],
        ["未结算", money(hit.unsettled_amount)],
        ["数量", num(hit.qty, 0) + " 件"]
      ];
    } else if (t === "c") {
      var c = (d.customer_summary || []).filter(function (x) { return String(x.name) === k; })[0];
      if (!c) return "";
      title = k;
      var r2 = function (a, b) { return b ? (Number(a) / Number(b) * 100) : 0; };
      rows = [
        ["出库金额", money(c.amount)],
        ["未结算", money(c.unsettled_amount)],
        ["开票率", num(r2(c.invoice_amount, c.amount), 1) + "%"],
        ["结算率", num(r2(c.receipt_settlement_amount, c.amount), 1) + "%"],
        ["单据数", num(c.bill_count, 0) + " 张"]
      ];
    }
    if (!rows.length) return "";
    return '<div class="pick-panel"><div class="pick-head"><b>' + esc(title) + "</b>" +
      '<button type="button" class="btn btn-ghost" id="pick-clear">取消筛选</button></div>' +
      '<div class="pick-grid">' + rows.map(function (r) {
        return '<div><span>' + esc(r[0]) + "</span><b>" + esc(r[1]) + "</b></div>";
      }).join("") + "</div></div>";
  }

  function renderSales2(d, cacheAt, refreshing) {
    if (!d) return;
    S.lastData.sales = d;
    d = window.PortalScope.select("sales", d, S.selected, S.orgs);
    updateRefreshStatus();
    var root = $("v-sales");
    var h = [];
    if (d._emptyScope) { root.innerHTML = '<div class="empty">请在右上角选择组织</div>'; return; }
    var errs = (d.coverage || {}).errors || [];
    for (var e of errs) {
      h.push('<div class="banner warn">采集失败：' + esc(e) + "</div>");
    }
    var s = d.summary || {};
    paintWorkbook("sales", d, root, h);
  }

  /* ---------------- 现金流预测看板 ---------------- */
  function renderCash2(d, cacheAt, refreshing) {
    if (!d) return;
    S.lastData.cash = d;
    d = window.PortalScope.select("cash", d, S.selected, S.orgs);
    updateRefreshStatus();
    var root = $("v-cash");
    var h = [];
    if (d._emptyScope) { root.innerHTML = '<div class="empty">请在右上角选择组织</div>'; return; }
    var k = d.kpi || {};
    for (var fe of (d.fund_errors || [])) {
      h.push('<div class="banner warn">头寸取数失败：' + esc(fe) + "</div>");
    }
    paintWorkbook("cash", d, root, h);
  }
  /* ---------------- 管理员控制台（公司管理 / 运营统计 / 安全与凭据） ----------------
     认证在登录屏完成（enterAdminLogin → /api/admin/tenants）；这里只渲染三个分区。
     用户 2026-10-14 反馈：登录后找不到改口令的地方、排版乱 —— 所以：
       · 三个分区用顶部 Tab 切换，互不堆在一页；
       · 「安全与凭据」里**修改口令放在第一张卡**，一眼能看到；
       · 公司列表用卡片（链接一键复制），不再堆一张大表。 */
  var T = { tenants: [], pw: "" };
  var ADM_SEC = "companies";

  function admTabs() {
    // 2026-10-20 用户要求：顶部页签删掉（侧栏已有「公司管理 / 运营统计 / 安全与凭据」）
    return "";
  }

  /* 控制台 hero 的 KPI：脚注**内联可见**（看板那个 kpi() 是折叠"口径说明"，不适合这里） */
  function admKpi(label, val, foot, tone) {
    var dot = tone === "green" ? "var(--green)" : tone === "blue" ? "var(--brand)" : "var(--t3)";
    return '<div class="card kpi tone-' + tone + '"><div class="lab"><i class="dot" style="background:' +
      dot + '"></i>' + esc(label) + '</div><div class="val">' + val + '</div>' +
      '<div class="foot-inline">' + foot + "</div></div>";
  }

  function admSection(name) {
    ADM_SEC = name || "companies";
    try {
      Array.prototype.forEach.call(document.querySelectorAll("#nav a[data-adm]"), function (a) {
        a.classList.toggle("on", a.dataset.adm === ADM_SEC);
      });
    } catch (e) { /* 忽略 */ }
    var root = $("v-adm");
    if (!root) return;
    root.innerHTML = ['<div class="adm-wrap">', admTabs(),
                      '<div id="adm-panel"><div class="hint">正在取数，请耐心等待…</div></div>',
                      "</div>"].join("");
    Array.prototype.forEach.call(root.querySelectorAll(".adm-tab"), function (b) {
      b.addEventListener("click", function () { admSection(b.dataset.sec); });
    });
    if (ADM_SEC === "stats") { admStats(); return; }
    if (ADM_SEC === "security") { admMode(); return; }
    admCompanies();
  }

  /* ---- 分区 1：公司管理（开通 + 列表） ---- */
  function admCompanies() {
    var box = $("adm-panel");
    if (!box) return;
    // 先把开通申请（手机/微信/提交时间）拉回来，卡片上要用
    if (!T.appsLoaded) {
      fetch("/api/admin/applications?status=all", { headers: { "X-Admin-Token": T.pw } })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          T.apps = (j && j.ok && j.apps) || [];
          T.appsLoaded = true;
          admCompanies();
        }).catch(function () { T.appsLoaded = true; admCompanies(); });
      return;
    }
    var active = 0, configured = 0;
    T.tenants.forEach(function (t) {
      if ((t.status || "active") === "active") active += 1;
      if (t.has_piazzone) configured += 1;
    });
    var h = [];
    h.push('<div class="adm-hero card">' +
      '<div class="adm-hero-copy"><div class="eyebrow">PLATFORM CONSOLE</div>' +
      "<h2>平台运营后台</h2></div>" +
      '<div class="adm-hero-kpis">' +
      admKpi("开通公司", num(T.tenants.length, 0), "使用中 " + num(active, 0) + " 家", "blue") +
      admKpi("发票云已配", num(configured, 0), "公司级密钥按租户隔离", "green") +
      "</div></div>");
    h.push('<div class="sec">开通新公司 <span class="sub">只填两项，唯一码系统自动生成</span></div>');
    h.push('<div class="card adm-create">' +
      '<div class="adm-create-grid">' +
      '<div class="rb-field"><label>公司名称 <span class="req">必填</span></label>' +
      '<input id="a-name" placeholder="如 示例科技有限公司甲"></div>' +
      '<div class="rb-field"><label>金蝶服务器地址 <span class="req">必填</span></label>' +
      '<input id="a-server" placeholder="https://xxx.ik3cloud.com/"></div>' +
      '<div class="rb-field"><label>账套 <span class="opt">可留空（单账套自动识别）</span></label>' +
      '<div class="row2"><input id="a-acct" placeholder="账套名称或 ID">' +
      '<button type="button" class="btn" id="a-dcs">获取账套</button></div>' +
      '<div class="hint" id="a-dcs-hint"></div></div>' +
      "</div>" +
      '<div class="row-actions"><button class="btn pri" id="a-save">开通并验证</button>' +
      '<button class="btn" id="a-probe">仅验证地址</button>' +
      '<label class="adm-check"><input type="checkbox" id="a-force"> 验证不通过也保存</label></div>' +
      '<div id="a-probe-msg"></div>' +
      '<div class="hint">开通后系统自动生成唯一码和专属链接；服务器地址会先探活，不通会拦下来。' +
      "员工从专属链接用他自己的金蝶账号登录。**单账套服务器账套可以留空**（自动识别）；" +"只有一台服务器上挂了多个账套（多个数据中心）时才需要点「获取账套」选一个。</div></div>");
    h.push('<div class="card" style="padding:16px 18px;margin:0 0 16px">' +
      '<div class="sec" style="margin:0 0 6px">申请通知 <span class="sub">' +
      "有人提交开通申请时，直接推到你指定的群</span></div>" +
      '<div class="row-actions" style="margin:0;align-items:center;gap:8px">' +
      '<input id="aw-url" placeholder="企业微信群机器人 webhook（https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…）" ' +
      'style="flex:1;min-width:240px;padding:8px 11px;border:1px solid var(--line);border-radius:9px;font-size:12.5px">' +
      '<button class="btn pri" id="aw-save">保存</button>' +
      '<button class="btn" id="aw-test">发测试</button></div>' +
      '<div class="hint" id="aw-msg">状态：未配置。留空保存 = 关闭通知。</div></div>');
    h.push('<div class="sec">已开通的公司 <span class="sub">共 ' + num(T.tenants.length, 0) + ' 家</span></div>');
    if (!T.tenants.length) {
      h.push('<div class="hint">还没有公司。填上面两个字段就能开通第一家。</div>');
    } else {
      h.push('<div class="adm-co-grid">' + T.tenants.map(admCoCard).join("") + "</div>");
    }
    h.push('<div id="adm-msg"></div>');
    box.innerHTML = h.join("");
    wireAdmCompanies(box);
  }

  function admCoCard(t) {
    var link = window.location.origin + "/?c=" + encodeURIComponent(t.code);
    var on = (t.status || "active") === "active";
    var h = ['<div class="adm-co card' + (on ? "" : " off") + '">'];
    var stNow = String(t.status || "active").toLowerCase();
    var badge = stNow === "active" ? ["green", "使用中"]
              : stNow === "pending" ? ["amber", "待审核"] : ["gray", "已暂停"];
    var appRow = (T.apps || []).filter(function (a) { return a.code === t.code; })[0] || null;
    h.push('<div class="adm-co-head"><div class="adm-co-name">' + esc(t.name) + "</div>" +
      '<span class="badge badge-' + badge[0] + '">' + badge[1] + "</span></div>");
    if (appRow) {
      h.push('<div class="adm-co-meta">' +
        '<div class="adm-co-row"><span>手机号</span><b class="mono">' + esc(appRow.phone || "—") + "</b></div>" +
        (appRow.wechat ? '<div class="adm-co-row"><span>微信</span><b>' + esc(appRow.wechat) + "</b></div>" : "") +
        (appRow.note ? '<div class="adm-co-row"><span>备注</span><b style="font-weight:400">' + esc(appRow.note) + "</b></div>" : "") +
        "</div>");
    }
    h.push('<div class="adm-co-meta">' +
      '<div class="adm-co-row"><span>公司码</span><b class="mono">' + esc(t.code) + "</b></div>" +
      '<div class="adm-co-row"><span>金蝶服务器</span><b class="mono">' + esc(t.server || "—") + "</b></div>" +
      '<div class="adm-co-row"><span>账套</span><b class="mono">' + esc(t.acct || "未指定（单账套自动识别）") + "</b></div>" +
      '<div class="adm-co-row"><span>发票云</span><b>' + (t.has_piazzone ? "已配置" : "未配置") + "</b></div>" +
      '<div class="adm-co-row"><span>公司管理员</span><b>' +
      ((t.admins && t.admins.length) ? esc(t.admins.join("、")) : "未认领") + "</b></div>" +
      "</div>");
    h.push('<div class="adm-co-link"><span>专属链接</span>' +
      '<input class="mono" readonly value="' + esc(link) + '">' +
      '<button class="btn" data-copy="' + esc(t.code) + '">复制</button></div>');
    h.push('<div class="row-actions adm-co-acts">' +
      (stNow === "pending"
        ? '<button class="btn pri" data-app-ok="' + esc(t.code) + '">通过并开通</button>' +
          '<button class="btn danger" data-app-no="' + esc(t.code) + '">拒绝</button>'
        : "") +
      '<button class="btn" data-acct-fix="' + esc(t.code) + '">补账套</button>' +
      '<button class="btn" data-toggle="' + esc(t.code) + '" data-to="' + (on ? "suspended" : "active") + '">' +
      (on ? "暂停" : "启用") + "</button>" +
      '<button class="btn danger" data-del="' + esc(t.code) + '">删除</button></div>');
    h.push("</div>");
    return h.join("");
  }

  function wireAdmCompanies(box) {
    // 申请通知：载入状态 + 保存 + 测试
    (function () {
      var msgEl = $("aw-msg");
      fetch("/api/admin/apply-webhook", { headers: { "X-Admin-Token": T.pw } })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!msgEl) return;
          msgEl.innerHTML = j && j.configured
            ? "状态：<b style='color:var(--green)'>已配置</b>（" + esc(j.webhook || "") +
              "）。要更换就直接粘贴新地址后保存；留空保存 = 关闭。"
            : "状态：<b>未配置</b>。填企业微信群机器人 webhook 后保存，有人申请就会推消息。";
        }).catch(function () {});
      var saveBtn = $("aw-save");
      if (saveBtn) saveBtn.addEventListener("click", function () {
        var url = ($("aw-url").value || "").trim();
        api("/api/admin/apply-webhook", { webhook: url, admin_password: T.pw }).then(function (r) {
          if (!r.ok) { if (msgEl) msgEl.innerHTML = errLine(r.error); return; }
          if (msgEl) msgEl.innerHTML = r.configured
            ? "状态：<b style='color:var(--green)'>已配置</b>，下次申请会推消息。点「发测试」验证一下。"
            : "状态：<b>未配置</b>（通知已关闭）。";
          $("aw-url").value = "";
        });
      });
      var testBtn = $("aw-test");
      if (testBtn) testBtn.addEventListener("click", function () {
        var b3 = this; b3.disabled = true; b3.textContent = "发送中…";
        api("/api/admin/apply-webhook/test", { admin_password: T.pw }).then(function (r) {
          b3.disabled = false; b3.textContent = "发测试";
          if (msgEl) msgEl.innerHTML = r.ok
            ? '<div class="banner info">✅ 已发送，去群里看看</div>'
            : errLine(r.error);
        });
      });
    })();
    Array.prototype.forEach.call(box.querySelectorAll("button[data-app-ok]"), function (b) {
      b.addEventListener("click", function () {
        if (!window.confirm("通过后该公司员工即可登录，确定？")) return;
        admDecide(b.dataset.appOk, "approve");
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll("button[data-app-no]"), function (b) {
      b.addEventListener("click", function () {
        var reason = window.prompt("拒绝原因（会显示给申请人，可留空）：", "");
        if (reason === null) return;
        admDecide(b.dataset.appNo, "reject", reason);
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll("button[data-acct-fix]"), function (b) {
      b.addEventListener("click", function () {
        var code = b.dataset.acctFix;
        var t = (T.tenants || []).filter(function (x) { return x.code === code; })[0] || {};
        var m = openModal("补填账套 · " + (t.name || code),
          '<div class="hint">多账套环境必须指定账套（金蝶的账套 ID 或名称），' +
          "否则员工登录会提示「无法确定账套」。单账套环境可以留空。</div>" +
          '<div class="rb-field"><label>账套</label><input id="fx-acct" placeholder="如 1930568647881801728" value="' +
          esc(t.acct || "") + '"></div><div id="fx-msg"></div>' +
          '<div class="row-actions"><button class="btn pri" id="fx-save">保存</button></div>');
        document.getElementById("fx-save").addEventListener("click", function () {
          var acct = (document.getElementById("fx-acct").value || "").trim();
          var msg = document.getElementById("fx-msg");
          if (!acct) { msg.innerHTML = '<div class="banner warn">请填账套</div>'; return; }
          api("/api/admin/tenants", {
            code: code, name: t.name, server: t.server, acct: acct,
            allow_update: true, force: true, admin_password: T.pw
          }).then(function (r) {
            if (!r.ok) { msg.innerHTML = errLine(r.error); return; }
            T.tenants = r.tenants || T.tenants;
            m.close();
            admCompanies();
            var nb = document.createElement("div");
            nb.className = "banner info";
            nb.textContent = "已补填账套：" + acct;
            var host = $("v-adm");
            if (host) host.insertBefore(nb, host.firstChild);
          });
        });
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll("button[data-toggle]"), function (b) {
      b.addEventListener("click", function () {
        var to = b.dataset.to;
        if (to === "suspended" && !window.confirm("暂停后该公司员工将无法登录，确定？")) return;
        admPost("/api/admin/tenants/status", { code: b.dataset.toggle, status: to });
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll("button[data-del]"), function (b) {
      b.addEventListener("click", function () {
        if (!window.confirm("删除租户 " + b.dataset.del + "？该公司的员工将无法登录。")) return;
        admPost("/api/admin/tenants/delete", { code: b.dataset.del });
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll("button[data-copy]"), function (b) {
      b.addEventListener("click", function () {
        var link = window.location.origin + "/?c=" + encodeURIComponent(b.dataset.copy);
        var done = function () {
          b.textContent = "已复制";
          setTimeout(function () { b.textContent = "复制"; }, 1500);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(link).then(done, function () { window.prompt("复制这个链接：", link); });
        } else {
          window.prompt("复制这个链接：", link);
        }
      });
    });
    var sv = $("a-save"); if (sv) sv.addEventListener("click", admSave);
    admBindDcs();
    var pb = $("a-probe"); if (pb) pb.addEventListener("click", function () {
      var s = $("a-server").value.trim();
      if (!s) { $("a-probe-msg").innerHTML = '<div class="hint">先填服务器地址</div>'; return; }
      $("a-probe-msg").innerHTML = '<div class="hint">正在验证（连一下金蝶，几秒）…</div>';
      api("/api/admin/tenants/probe", { server: s, admin_password: T.pw }).then(function (r) {
        if (!r.ok) { $("a-probe-msg").innerHTML = errLine(r.error); return; }
        var p = r.probe || {};
        $("a-probe-msg").innerHTML = '<div class="banner ' + (p.reachable ? "info" : "warn") + '">' +
          (p.reachable ? "✅ 可用：" : "⚠️ 不可用：") + esc(p.note || "") + "</div>";
      });
    });
  }

  function admDecide(code, action, reason) {
    $("adm-msg").innerHTML = '<div class="hint">处理中…</div>';
    api("/api/admin/applications/decide",
        { code: code, action: action, reason: reason || "", admin_password: T.pw })
      .then(function (r) {
        if (!r.ok) { $("adm-msg").innerHTML = errLine(r.error); return; }
        // 重新拉租户与申请，刷新卡片
        return fetch("/api/admin/tenants", { headers: { "X-Admin-Token": T.pw } })
          .then(function (x) { return x.json(); })
          .then(function (j) {
            T.tenants = (j && j.tenants) || T.tenants;
            T.appsLoaded = false;
            admCompanies();
            // ⚠️ 不能写进 #adm-msg：admCompanies 会重渲染整块把它冲掉。
            //    放到 #v-adm 顶部（重渲染只覆盖 #adm-panel）。
            var nb = document.createElement("div");
            nb.className = "banner info";
            nb.textContent = "✅ 已" + (action === "approve" ? "通过并开通" : "拒绝") + "：" + code;
            var host = $("v-adm");
            if (host) host.insertBefore(nb, host.firstChild);
          });
      });
  }

  function admBindDcs() {
    var btn = $("a-dcs");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var server = ($("a-server").value || "").trim();
      var hint = $("a-dcs-hint");
      if (!server) { hint.innerHTML = "请先填服务器地址"; return; }
      btn.disabled = true;
      hint.textContent = "正在读取账套列表…";
      api("/api/datacenters", { server_url: server }).then(function (r) {
        btn.disabled = false;
        if (!r.ok) { hint.innerHTML = errLine(r.error); return; }
        var list = r.list || r.datacenters || [];
        if (!list.length) { hint.textContent = "该服务器没有返回账套"; return; }
        if (list.length === 1) {
          $("a-acct").value = list[0].name || list[0].id || "";
          hint.innerHTML = "只有 1 个账套：<b>" + esc(list[0].name || "") +
            "</b>（已自动填入，也可以留空）";
          return;
        }
        hint.innerHTML = "共 " + list.length + " 个账套，点选一个：" +
          list.map(function (c) {
            return '<a href="#" data-pick="' + esc(c.name || c.id || "") +
              '" style="color:var(--brand);margin-left:8px">' +
              esc(c.name || c.id || "") + "</a>";
          }).join("");
        Array.prototype.forEach.call(hint.querySelectorAll("a[data-pick]"), function (a) {
          a.addEventListener("click", function (e) {
            e.preventDefault();
            $("a-acct").value = a.dataset.pick;
            hint.innerHTML = "已选择：<b>" + esc(a.dataset.pick) + "</b>";
          });
        });
      }).catch(function () {
        btn.disabled = false;
        hint.textContent = "读取失败，请检查服务器地址";
      });
    });
  }

  function admSave() {
    var body = {
      name: $("a-name").value.trim(),
      server: $("a-server").value.trim(),
      acct: ($("a-acct") && $("a-acct").value.trim()) || "",
      force: $("a-force") && $("a-force").checked
    };
    body.admin_password = T.pw;
    $("adm-msg").innerHTML = '<div class="hint">正在开通并验证服务器（几秒）…</div>';
    api("/api/admin/tenants", body).then(function (r) {
      if (!r.ok) { $("adm-msg").innerHTML = errLine(r.error); return; }
      T.tenants = r.tenants || [];
      var code = r.code || "";
      admCompanies();
      var link = window.location.origin + "/?c=" + encodeURIComponent(code);
      $("adm-msg").innerHTML = '<div class="banner info">✅ 已开通。唯一码 <b>' + esc(code) +
        "</b>，把这行链接发给该公司的员工：<br><b style='font-size:15px'>" + esc(link) + "</b><br>" +
        "员工打开链接，用他自己的金蝶账号密码登录即可。</div>";
    });
  }

  function admPost(path, body) {
    body.admin_password = T.pw;
    $("adm-msg").innerHTML = '<div class="hint">处理中…</div>';
    api(path, body).then(function (r) {
      if (!r.ok) { $("adm-msg").innerHTML = errLine(r.error); return; }
      T.tenants = r.tenants || [];
      var created = r.created;
      admCompanies();
      $("adm-msg").innerHTML = '<div class="banner info">✅ 已' +
        (created === true ? "开通新公司" : created === false ? "更新" : "完成") + "</div>";
    });
  }

  /* ---- 分区 2：运营统计（只统计账号/公司码/时间/成败，不含任何密码与业务数据） ---- */
  function admStats() {
    var box = $("adm-panel");
    if (!box) return;
    box.innerHTML = '<div class="hint">正在读取统计…</div>';
    fetch("/api/admin/stats", { headers: { "X-Admin-Token": T.pw } })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) { box.innerHTML = errLine(j.error); return; }
        var s = j.stats || {};
        var tot = s.total || {}, td = s.today || {};
        var tenants = j.tenants || [];
        var h = [];
        h.push('<div class="sec" style="margin-top:0">运营统计 <span class="sub">近 ' +
          (s.days || 30) + ' 天 · 只统计登录，不含业务数据</span></div>');
        h.push('<div class="grid g4">' +
          kpi("开通公司", num(tenants.length, 0), "其中使用中：" +
            num(tenants.filter(function (t) { return t.status === "active"; }).length, 0), "blue") +
          kpi("今日登录人次", num(td.n || 0, 0), "成功 " + num(td.ok || 0, 0), "green") +
          kpi("近 " + (s.days || 30) + " 天登录人次", num(tot.n || 0, 0),
            "成功 " + num(tot.ok || 0, 0), "amber") +
          kpi("有登录的公司", num((s.by_tenant || []).length, 0), "按公司汇总去重人数", "gray") +
          "</div>");
        var bt = (s.by_tenant || []).map(function (x) {
          var t = tenants.filter(function (y) { return y.code === x.tenant; })[0] || {};
          return "<tr><td>" + esc(t.name || "（已删除）") + "</td><td class='mono'>" + esc(x.tenant) +
            "</td><td class='num'>" + num(x.users, 0) + "</td><td class='num'>" + num(x.n, 0) +
            "</td></tr>";
        });
        if (bt.length) {
          h.push('<div class="sec">各公司登录情况</div>');
          h.push(table([{ t: "公司" }, { t: "公司码" }, { t: "登录人数", num: 1 },
                        { t: "登录人次", num: 1 }], bt));
        }
        var rc = (s.recent || []).map(function (x) {
          return "<tr><td class='mono'>" + esc(x.at) + "</td><td>" + esc(x.tenant || "—") +
            "</td><td class='mono'>" + esc(x.username || "—") + "</td><td>" +
            (x.ok ? "<span class='badge badge-green'>成功</span>"
                  : "<span class='badge badge-amber'>失败</span>") +
            "</td><td>" + esc(x.note || "") + "</td></tr>";
        });
        h.push('<div class="sec">最近登录明细</div>');
        h.push(rc.length
          ? table([{ t: "时间" }, { t: "公司码" }, { t: "账号" }, { t: "结果" }, { t: "备注" }], rc)
          : '<div class="hint">还没有登录记录。</div>');
        box.innerHTML = h.join("");
      })
      .catch(function () { box.innerHTML = '<div class="banner err">取数失败：连接异常</div>'; });
  }

  /* ---- 分区 3：安全与凭据（修改口令放第一张卡，一眼能看到） ---- */
  function admMode() {
    var box = $("adm-panel");
    if (!box) return;
    fetch("/api/admin/settings", { headers: { "X-Admin-Token": T.pw } })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) { box.innerHTML = errLine(j.error); return; }
        var mode = (j.settings || {}).credential_mode || "disk";
        var h = [];
        h.push('<div class="sec" style="margin-top:0">管理员口令 <span class="sub">改完立即生效，旧口令失效</span></div>');
        h.push('<div class="card adm-pw">' +
          '<div class="rb-field"><label>新口令 <span class="opt">至少 8 位，必须含字母+数字</span></label>' +
          '<input id="adm-newpw" type="password" placeholder="输入新口令"></div>' +
          '<div class="rb-field"><label>再输一遍 <span class="opt">两次要一致</span></label>' +
          '<input id="adm-newpw2" type="password" placeholder="再输入一次"></div>' +
          '<div class="row-actions"><button class="btn pri" id="adm-savepw">保存新口令</button></div>' +

          '<div id="adm-pw-msg"></div></div>');
        h.push('<div class="sec">凭据模式 <span class="sub">全局默认；各公司可在自己的发票云配置页单独改</span></div>');
        h.push('<div class="card"><div class="rb-field"><label>当前：<b>' +
          (mode === "memory" ? "内存模式" : "加密落盘") + "</b></label>" +
          '<div class="hint">' +
          "<b>加密落盘（默认）</b>：会话凭据加密写盘 → 服务重启也能免登录；防的是「文件被误拷贝 / 误传 / 写进日志」，<b>不防</b>已经拿到整台机器的人（密钥与数据在同一台机器上）。<br>" +
          "<b>内存模式</b>：凭据<b>完全不落盘</b> → 拿到机器也解不出任何登录凭据；代价是服务重启后必须重新登录（发票云配置不受影响，它按公司存在租户表里）。</div>" +
          '<div class="row-actions" style="margin-top:12px">' +
          '<button class="btn' + (mode === "disk" ? " pri" : "") +
          '" data-mode="disk">加密落盘（便利）</button>' +
          '<button class="btn' + (mode === "memory" ? " pri" : "") +
          '" data-mode="memory">内存模式（更安全）</button></div></div>');
        h.push('<div class="sec">防爆破 <span class="sub">管理员地址是公司链接的上一层，一定会有人来试</span></div>');
        h.push('<div class="hint">连续输错 6 次锁定 10 分钟（全局计数：平台网关转发的 X-Forwarded-For 每次可能不同，' +
          "按 IP 计数拦不住，这是实测后的取舍）。锁定期间正确口令也会被暂时拒绝；IP 会记进审计日志便于追溯。</div>");
        box.innerHTML = h.join("");
        Array.prototype.forEach.call(box.querySelectorAll("button[data-mode]"), function (b) {
          b.addEventListener("click", function () {
            api("/api/admin/settings", { credential_mode: b.dataset.mode, admin_password: T.pw })
              .then(function (r) {
                if (!r.ok) { $("adm-pw-msg").innerHTML = errLine(r.error); return; }
                admMode();
                $("adm-pw-msg").innerHTML = '<div class="banner info">✅ 已切换全局默认为' +
                  (b.dataset.mode === "memory" ? "内存模式（重启需重登）" : "加密落盘") + "</div>";
              });
          });
        });
        var sp = $("adm-savepw");
        if (sp) sp.addEventListener("click", function () {
          var np = ($("adm-newpw").value || "").trim();
          var np2 = ($("adm-newpw2").value || "").trim();
          var m = $("adm-pw-msg");
          if (np.length < 8) { m.innerHTML = '<div class="banner warn">新口令至少 8 位。</div>'; return; }
          if (!/[A-Za-z]/.test(np) || !/\d/.test(np)) {
            m.innerHTML = '<div class="banner warn">新口令要同时包含字母和数字。</div>'; return;
          }
          if (np !== np2) { m.innerHTML = '<div class="banner warn">两次输入不一致。</div>'; return; }
          sp.disabled = true;
          api("/api/admin/password", { new_password: np, admin_password: T.pw }).then(function (r) {
            sp.disabled = false;
            if (!r.ok) { m.innerHTML = errLine(r.error); return; }
            T.pw = np;                      // 本页继续用新口令
            $("adm-newpw").value = ""; $("adm-newpw2").value = "";
            m.innerHTML = '<div class="banner info">✅ 管理员口令已更新（哈希入库，明文初始口令已失效）。</div>';
          });
        });
      })
      .catch(function () { box.innerHTML = '<div class="banner err">取数失败：连接异常</div>'; });
  }

  function loadAdm() {
    // 公司工作台里不再留「租户管理」入口；万一从旧链接/书签进来，引导去根地址。
    var root = $("v-adm");
    if (root) {
      root.innerHTML = '<div class="banner info">管理员控制台已独立：打开 <b>' +
        esc(window.location.origin) + "/</b> （不带任何参数）即可进入。</div>";
    }
  }

  /* ---------------- 视图切换 ---------------- */
  // 供界面回归脚本 _ui_check_reimburse.py 使用（只暴露渲染入口与切页，不含任何凭据）
  window.__portalDebug = { openBill: rbOpen, reload: loadBillsPage,
                           gotoApply: loadApply, gotoBills: loadBillsPage,
                           openApplyBill: function (fid) { R.pendingFocus = fid; gotoView("bills"); } };

  var TITLES = { expense: "费用报销看板", mgmt: "财务健康与风险", sales: "销售与回款表现",
                 cash: "现金流预测", apply: "申请单", bills: "报销单",
                 pzcfg: "发票云配置", adm: "租户管理", orgs: "组织与权限", source: "数据源与口径",
                 guide: "使用说明" };
  var DASH_VIEWS = ["expense", "mgmt", "sales", "cash"];

  /* 看板子页返回 / 主题切换后重画：用 S.lastData 重渲当前视图（不重新请求） */
  function paintWorkbook(view, d, root, h) {
    h.push(window.PortalAnalytics.render(view, d, { orgs: S.orgs, selected: S.selected, onOrg: function (no) { S.selected = [String(no)]; afterOrgChange(false); } }));
    root.innerHTML = h.join("");
    window.PortalAnalytics.wire(root, view, function () { rerenderView(view); }, function (id) {
      SUB[view] = id;
      rerenderView(view);
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    orgDoneNotice();
  }

  function rerenderView(view) {
    var d = (S.lastData || {})[view];
    if (view === "expense" && d) { renderExpense2(d); return; }
    if (view === "mgmt" && d) { renderMgmtFull(d); return; }
    if (view === "sales" && d) { renderSales2(d); return; }
    if (view === "cash" && d) { renderCash2(d); return; }
  }

  function bindApp() {
    // ★ 2026-10-20 图表交互：任意可点图表元素 → 记录选中项 → 重渲染当前看板
    document.addEventListener("click", function (e) {
      var clr = e.target.closest && e.target.closest("#pick-clear");
      if (clr) { e.preventDefault(); S.pick = ""; rerenderView(S.view); return; }
      var el = e.target.closest && e.target.closest("[data-k]");
      if (!el) return;
      var k = el.dataset.k || "";
      if (!/^sal-|^exp-|^mgt-|^csh-/.test(k)) return;
      e.preventDefault();
      S.pick = (S.pick === k) ? "" : k;      // 再点一次取消
      rerenderView(S.view);
      // 面板在图表下方，点完自动滚过去（否则要手动往下滑才看得到结果）
      if (S.pick) {
        setTimeout(function () {
          var pn = document.querySelector(".pick-panel");
          if (pn) pn.scrollIntoView({ behavior: "smooth", block: "center" });
        }, 80);
      }
    });
    Array.prototype.forEach.call($("nav").querySelectorAll("a[data-v]"), function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        var v = a.dataset.v;
        S.view = v;
        SUB[v] = "";                       // 切换看板先回主页（子页状态不跨看板串）
        Array.prototype.forEach.call($("nav").querySelectorAll("a"), function (x) {
          x.classList.toggle("on", x === a);
          if (x === a) x.setAttribute("aria-current", "page"); else x.removeAttribute("aria-current");
        });
        ["expense", "mgmt", "sales", "cash", "apply", "bills", "pzcfg", "orgs", "source", "adm", "guide"]
          .forEach(function (k) {
            $("v-" + k).classList.toggle("on", k === v);
          });
        $("btn-reset-view").hidden = DASH_VIEWS.indexOf(v) < 0;
        $("ttl").textContent = TITLES[v];
        updateRefreshStatus();
        if ($("breadcrumb")) $("breadcrumb").textContent =
          (v === "apply" || v === "bills" || v === "pzcfg") ? "报销管理"
            : v === "adm" ? "系统"
            : (v === "expense" || v === "mgmt" || v === "sales" || v === "cash") ? "经营概览"
            : "数据管理";
        $("org-panel").hidden = true; $("btn-orgs").setAttribute("aria-expanded", "false");
        $("btn-refresh").hidden = (v === "orgs" || v === "source" || v === "guide");
        $("btn-orgs").hidden = (v === "orgs" || v === "source" || v === "guide");
        $("period-label").hidden = true;
        $("sel-year").hidden = true;
        $("sel-month").hidden = true;
        if (v === "expense" && !S.loaded.expense) loadExpense(false);
        if (v === "mgmt" && !S.loaded.mgmt) loadMgmt(false);
        if (v === "sales" && !S.loaded.sales) loadSales(false);
        if (v === "cash" && !S.loaded.cash) loadCash(false);
        // 报销管理两页都是"办理中"的页面，每次进入都重新拉一次
        if (v === "apply") loadApply();
        if (v === "bills") loadBillsPage();
        if (v === "pzcfg") loadPzCfg();
        if (v === "adm") loadAdm();
      });
    });

    $("btn-orgs").addEventListener("click", function () {
      var p = $("org-panel"); p.hidden = !p.hidden;
      this.setAttribute("aria-expanded", String(!p.hidden));
    });
    if ($("btn-orgs-close")) {
      $("btn-orgs-close").addEventListener("click", function () {
        $("org-panel").hidden = true; $("btn-orgs").setAttribute("aria-expanded", "false");
        $("btn-orgs").focus();
      });
    }
    var me = document.querySelector(".me");
    if (me) me.classList.add("mobile-user");
    $("btn-reset-view").addEventListener("click", function () { window.PortalAnalytics.reset(); rerenderView(S.view); });
    $("btn-refresh").addEventListener("click", function () {
      if (S.view === "expense") loadExpense(true);
      if (S.view === "mgmt") loadMgmt(true);
      if (S.view === "sales") loadSales(true);
      if (S.view === "cash") loadCash(true);
      if (S.view === "apply") loadApply();
      if (S.view === "bills") loadBillsPage();
    });
    $("btn-logout").addEventListener("click", function () {
      api("/api/logout", {}).then(function (r) {
        if (!r.ok) { showErr(r.error); return; }
        if (CFG.localMode) {
          _embeddedBoot = null;
          try { sessionStorage.removeItem("portal_boot_local"); } catch (e) {}
        }
        location.reload();
      });
    });

    var y = new Date().getFullYear(), cur = "";
    for (var i = y - 2; i <= y + 1; i++) {
      cur += '<option value="' + i + '"' + (i === y ? " selected" : "") + ">" + i + " 年</option>";
    }
    $("sel-year").innerHTML = cur;
    cur = "";
    for (var m = 1; m <= 12; m++) {
      cur += '<option value="' + m + '"' + (m === new Date().getMonth() + 1 ? " selected" : "") +
        ">" + m + " 月</option>";
    }
    $("sel-month").innerHTML = cur;
    
    
    $("period-label").hidden = true; $("sel-year").hidden = true; $("sel-month").hidden = true;
  }

  /* ---------------- 费用报销看板 ---------------- */
  var dashboardQueue = [], dashboardActive = null, dashboardStarted = false, dashboardFailures = {};
  var DASH_RENDER = { expense: renderExpense2, mgmt: renderMgmtFull, sales: renderSales2, cash: renderCash2 };
  function updateRefreshStatus() {
    var btn = $("btn-refresh"); if (!btn) return;
    var d = S.lastData[S.view], active = dashboardActive && dashboardActive.view === S.view;
    btn.title = active ? "正在提取数据" : d ? "更新于 " + (d.generated_at || "本次登录") + "；点击重新提取" : "数据等待加载";
    if (dashboardFailures[S.view]) btn.title = "更新失败：" + dashboardFailures[S.view] + "；保留上次数据，点击重试";
    btn.setAttribute("aria-busy", String(!!active));
    var label = btn.querySelector(".refresh-label");
    if (label) label.textContent = active ? "更新中…" : dashboardFailures[S.view] ? "刷新重试" : "刷新数据";
    btn.disabled = !!(active && dashboardActive.force);
  }
  function startDashboards() {
    if (dashboardStarted) return;
    dashboardStarted = true;
    ["expense", "mgmt", "sales", "cash"].forEach(function (view) { dashboardQueue.push({ view: view, force: false, phase: "overview" }); });
    dashboardQueue.push({ view: "mgmt", force: false, phase: "capital" });
    dashboardQueue.push({ view: "mgmt", force: false, phase: "detail" });
    runDashboardQueue();
  }
  function requestDashboard(view, force) {
    if (!dashboardStarted) { startDashboards(); if (!force) return; }
    if (!force) { if (!S.lastData[view]) skeleton(view); return; }
    dashboardQueue = dashboardQueue.filter(function (x) { return x.view !== view; });
    dashboardQueue.unshift({ view: view, force: true, phase: "overview" });
    if (view === "mgmt") {
      dashboardQueue.push({ view: view, force: true, phase: "capital" });
      dashboardQueue.push({ view: view, force: true, phase: "detail" });
    }
    runDashboardQueue();
  }
  function runDashboardQueue() {
    if (dashboardActive || !dashboardQueue.length) return;
    var task = dashboardActive = dashboardQueue.shift(), view = task.view;
    S.loaded[view] = true;
    if (!S.lastData[view]) skeleton(view);
    updateRefreshStatus();
    var now = new Date(), body = { orgs: S.orgs.map(function (o) { return o.number; }), force: task.force };
    if (view === "mgmt") { body.phase = task.phase; body.year = now.getFullYear(); body.month = now.getMonth() + 1; }
    var endpoint = view === "mgmt" ? "mgmt-full" : view;
    function fail(error) { throw new Error(typeof error === "string" ? error : (error || {}).message || "取数失败"); }
    function follow(r) {
      if (!r.ok) return fail(r.error);
      var job = r.job || {};
      if (job.status === "error") return fail(job.error);
      if (job.status === "done") return job.result || {};
      return new Promise(function (resolve) { setTimeout(resolve, 1200); }).then(function () { return api("/api/job/" + job.id, null, "GET"); }).then(follow);
    }
    api("/api/dashboard/" + endpoint, body).then(follow).then(function (data) {
      if (view === "mgmt" && task.phase === "capital" && S.lastData.mgmt) {
        var previous = S.lastData.mgmt;
        data = Object.assign({}, previous, data, { pages: previous.pages, cards: previous.cards,
          generated_at: previous.generated_at, period: previous.period, errors: previous.errors,
          deferred_sources: previous.deferred_sources, capital_updated_at: data.generated_at });
      }
      if (view === "mgmt" && task.phase === "detail" && S.lastData.mgmt) {
        var prior = S.lastData.mgmt;
        data = Object.assign({}, prior, { pages: Object.assign({}, prior.pages, data.pages),
          cards: (prior.cards || []).filter(function (c) { return !data.pages[c.id]; }).concat(data.cards || []),
          errors: (prior.errors || []).concat(data.errors || []), deferred_sources: [], detail_updated_at: data.generated_at });
      }
      delete dashboardFailures[view];
      DASH_RENDER[view](data);
    }).catch(function (e) {
      dashboardFailures[view] = e.message;
      if ((task.phase === "detail" || task.phase === "capital") && S.lastData.mgmt) {
        if (task.phase === "detail") S.lastData.mgmt.deferred_sources = [];
        else { S.lastData.mgmt.capital_pending = false; S.lastData.mgmt.capital_errors = [{ reason: "财报后台加载失败，请点击刷新数据重试" }]; }
        (S.lastData.mgmt.errors || (S.lastData.mgmt.errors = [])).push("后台数据：" + e.message); rerenderView("mgmt");
      } else if (!S.lastData[view]) $("v-" + view).innerHTML = '<div class="banner warn">' + esc(e.message) + ' · 请点击刷新数据重试</div>';
    }).finally(function () {
      dashboardActive = null; updateRefreshStatus();
      setTimeout(runDashboardQueue, 350);
    });
  }
  function loadExpense(force) { requestDashboard("expense", force); }
  function loadMgmt(force) { requestDashboard("mgmt", force); }
  function loadSales(force) { requestDashboard("sales", force); }
  function loadCash(force) { requestDashboard("cash", force); }

  function table(head, rows) {
    if (!rows.length) return '<div class="empty">没有符合条件的数据</div>';
    return '<div class="scroll"><table class="tbl"><thead><tr>' +
      head.map(function (h) { return "<th" + (h.num ? ' class="num"' : "") + ">" + esc(h.t) + "</th>"; }).join("") +
      "</tr></thead><tbody>" + rows.join("") + "</tbody></table></div>";
  }

  function apiUsage(d) {
    var u = d && d.api_usage;
    if (!u) return "";
    return " · 本次调用接口 " + u.calls + " 次（金蝶限速 " + u.per_minute_limit +
      " 次/分钟" + (u.rate_retries ? "，触发限流重试 " + u.rate_retries + " 次" : "") +
      (u.waited_seconds ? "，限速等待 " + u.waited_seconds + "s" : "") + "）";
  }

  function renderExpense(d) {
    if (!d) return;
    S.lastData.expense = d;
    var m = d.metrics, c = d.coverage || {};
    var h = [];

    
    h.push(dataStatus(d, "操作账号：" + esc((d.scope || {}).operator || "—") + "<br>组织范围：" +
      esc(((d.scope || {}).org_names || (d.scope || {}).orgs || []).join(" / ")) +
      "<br>取数方式：" + esc(c.method || "ExecuteBillQuery") + "<br>口径与员工费用报销管理仪表盘一致。",
      orgLabel() + (c.truncated ? " · 统计覆盖不完整"
        : " · " + num(c.bills_after_org_filter, 0) + " 张单据已纳入")));
    orgDoneNotice();

    if (c.truncated) {
      h.push('<div class="banner warn"><b>覆盖度说明（安全阀触发）：</b>共 ' + c.bills_after_org_filter +
        " 张单，只统计了 " + c.bills_with_detail + " 张，未纳入 " + c.truncated +
        " 张。默认不限张数（max_bills_for_detail=0），说明本次数据量异常大，建议按组织分批取。</div>");
    }
    if ((c.header_errors || []).length) {
      h.push('<div class="banner warn">以下单据表头读取失败，未纳入统计：' +
        c.header_errors.map(esc).join("、") + "</div>");
    }

    h.push('<div class="grid g4">' +
      kpi("借款未还", wan(m.borrow_unpaid), "申请借款=是 的借款单余额合计<br>口径：Σ(实际借款 − 已报销 − 已还款 − 核销)", "red") +
      kpi("已申请未报销", wan(m.apply_unreimb), "申请借款=否 且 未关闭<br>口径：申请金额 − 已下推报销单金额", "amber") +
      kpi("报销未付款", wan(m.unpaid), "已审核报销单的未付款金额合计<br>口径：Σ ReimbNotPayAmount", "amber") +
      kpi("单据规模", num(m.n_apply + m.n_reimb, 0) + " 张", "申请 " + num(m.n_apply, 0) + " / 报销 " +
        num(m.n_reimb, 0) + " · 借款单 " + num(m.n_borrow, 0), "blue") +
      "</div>");

    h.push('<div class="sec">申请与报销规模 <span class="sub">全量金额口径</span></div>');
    h.push('<div class="grid g4 compact-metrics">' +
      kpi("费用申请金额", wan(m.apply_amt), "费用申请单合计", "gray") +
      kpi("出差申请金额", wan(m.tapply_amt), "出差申请单合计", "gray") +
      kpi("费用报销金额", wan(m.reimb_amt), "费用报销单合计", "gray") +
      kpi("差旅报销金额", wan(m.treimb_amt), "差旅费报销单合计", "gray") +
      "</div>");

    h.push('<div class="sec">借款未还 Top <span class="sub">按余额倒序，最多 40 条</span></div>');
    h.push(table([{ t: "单据编号" }, { t: "申请人" }, { t: "组织" }, { t: "日期" },
      { t: "借款金额", num: 1 }, { t: "已核销", num: 1 }, { t: "未还余额", num: 1 }],
      (d.borrow_bills || []).map(function (b) {
        return "<tr><td class='mono'>" + esc(b.billno) + "</td><td>" + esc(b.staff) +
          "</td><td>" + esc(b.org ? b.org + " " + (b.org_name || "") : "—") + "</td><td>" + esc(b.date) +
          "</td><td class='num'>" + money(b.amt) + "</td><td class='num'>" + money(b.settled) +
          "</td><td class='num up'>" + money(b.balance) + "</td></tr>";
      })));

    h.push('<div class="sec">已申请未报销 Top <span class="sub">申请金额 − 已下推报销单金额</span></div>');
    h.push(table([{ t: "单据编号" }, { t: "类型" }, { t: "申请人" }, { t: "组织" }, { t: "日期" },
      { t: "申请金额", num: 1 }, { t: "已下推", num: 1 }, { t: "未报销", num: 1 }],
      (d.apply_bills || []).map(function (b) {
        return "<tr><td class='mono'>" + esc(b.billno) + "</td><td>" + esc(b.kind) + "</td><td>" +
          esc(b.staff) + "</td><td>" + esc(b.org ? b.org + " " + (b.org_name || "") : "—") + "</td><td>" +
          esc(b.date) + "</td><td class='num'>" + money(b.amt) + "</td><td class='num'>" +
          money(b.pushed) + "</td><td class='num up'>" + money(b.diff) + "</td></tr>";
      })));

    h.push('<div class="sec">报销未付款 Top <span class="sub">已审核但尚未付给员工</span></div>');
    h.push(table([{ t: "单据编号" }, { t: "类型" }, { t: "申请人" }, { t: "组织" }, { t: "日期" },
      { t: "报销总额", num: 1 }, { t: "未付款", num: 1 }],
      (d.unpaid_bills || []).map(function (b) {
        return "<tr><td class='mono'>" + esc(b.billno) + "</td><td>" + esc(b.kind) + "</td><td>" +
          esc(b.staff) + "</td><td>" + esc(b.org ? b.org + " " + (b.org_name || "") : "—") + "</td><td>" +
          esc(b.date) + "</td><td class='num'>" + money(b.total) + "</td><td class='num up'>" +
          money(b.unpaid) + "</td></tr>";
      })));

    h.push('<div class="sec">审批中 / 草稿 <span class="sub">表头口径，不依赖单据体</span></div>');
    h.push(table([{ t: "单据编号" }, { t: "类型" }, { t: "申请人" }, { t: "组织" }, { t: "日期" },
      { t: "金额", num: 1 }, { t: "状态" }],
      (d.pending_bills || []).map(function (b) {
        return "<tr><td class='mono'>" + esc(b.billno) + "</td><td>" + esc(b.kind) + "</td><td>" +
          esc(b.staff) + "</td><td>" + esc(b.org ? b.org + " " + (b.org_name || "") : "—") + "</td><td>" +
          esc(b.date) + "</td><td class='num'>" + money(b.amt) + "</td><td><span class='pill " +
          (b.status === "审核中" ? "p-blue" : "p-gray") + "'>" + esc(b.status) + "</span></td></tr>";
      })));

    h.push('<div class="sec">按人员汇总 <span class="sub">按「未还 + 未报销 + 未付款」倒序</span></div>');
    h.push(table([{ t: "姓名" }, { t: "部门" }, { t: "申请金额", num: 1 }, { t: "报销金额", num: 1 },
      { t: "借款未还", num: 1 }, { t: "已申请未报销", num: 1 }, { t: "报销未付款", num: 1 }],
      (d.staff_rows || []).map(function (s) {
        return "<tr><td>" + esc(s.name) + "</td><td>" + esc(s.dept || "—") + "</td><td class='num'>" +
          money(s.apply) + "</td><td class='num'>" + money(s.reimb) + "</td><td class='num up'>" +
          money(s.borrow_unpaid) + "</td><td class='num up'>" + money(s.apply_unreimb) +
          "</td><td class='num up'>" + money(s.unpaid) + "</td></tr>";
      })));

    h.push('<div class="chart-grid"><div class="card chart-card"><div class="card-heading"><h3>月度申请与报销</h3><span>单位：万元</span></div><div class="chart-body">' +
      trend(d.monthly || {}) + '</div><div class="chart-legend"><span><i class="legend-dot" style="background:' +
      token("--chart-a") + '"></i>申请金额</span><span><i class="legend-dot" style="background:' +
      token("--chart-b") + '"></i>报销金额</span></div></div>' +
      '<div class="card chart-card"><div class="card-heading"><h3>费用类型分布</h3><span>Top 8 + 其他</span></div><div class="chart-body">' +
      bars((d.exp_type_pie || []).map(function (x) { return { label: x[0], v: x[1] }; }), "blue") +
      '</div><div class="hint">来源：报销单费用项目</div></div></div>');

    $("v-expense").innerHTML = h.join("");
    enhanceExpense(d);
  }

  function kpi(label, val, foot, tone) {
    var cls = tone === "red" ? "up" : tone === "green" ? "down" : "";
    var dot = tone === "red" ? "var(--red)" : tone === "amber" ? "var(--amber)" :
      tone === "green" ? "var(--green)" : tone === "blue" ? "var(--brand)" : "var(--t3)";
    return '<div class="card kpi tone-' + esc(tone) + '"><div class="lab"><i class="dot" style="background:' + dot + '"></i>' +
      esc(label) + '</div><div class="val ' + cls + '">' + val + '</div>' +
      '<details class="foot"><summary>口径说明</summary><div class="foot-detail">' + foot + "</div></details></div>";
  }

  function bars(items, tone, opt) {
    if (!items.length) return '<div class="empty">当前范围暂无数据</div>';
    opt = opt || {};
    var max = Math.max.apply(null, items.map(function (x) { return Math.abs(Number(x.v) || 0); })) || 1;
    var palette = [1, 2, 3, 4, 5, 6, 7, 8].map(function (i) { return token("--chart-" + i); });
    return items.map(function (x, i) {
      var w = Math.abs(Number(x.v) || 0) / max * 100;
      var color = tone === "red" ? token("--red") : palette[i % palette.length];
      var key = opt.prefix ? opt.prefix + ":" + (x.k != null ? x.k : x.label) : "";
      var on = key && opt.picked === key;
      return '<div class="bar-row' + (key ? " pickable" : "") + (on ? " picked" : "") + '"' +
        (key ? ' data-k="' + esc(key) + '"' : "") +
        '><div class="bar-meta"><span>' + esc(x.label) +
        '</span><span>' + money(x.v) + '</span></div><div class="bar-track"><i class="bar-fill" style="width:' +
        w.toFixed(1) + '%;background:' + color + '"></i></div></div>';
    }).join("");
  }

  function trend(monthly) {
    var keys = Object.keys(monthly).sort().slice(-12);
    if (!keys.length) return '<div class="empty">当前范围暂无月度数据</div>';
    var seriesA = token("--chart-a"), seriesB = token("--chart-b");
    var gridColor = token("--chart-grid"), axisColor = token("--chart-axis");
    var W = 600, H = 220, left = 58, top = 18, right = 16, bottom = 36;
    var values = [];
    keys.forEach(function (k) { values.push(Number(monthly[k].apply) || 0, Number(monthly[k].reimb) || 0); });
    var hi = Math.max.apply(null, values.concat([0])), lo = Math.min.apply(null, values.concat([0]));
    if (hi === lo) hi = lo + 10000;
    var range = hi - lo, plotH = H - top - bottom, slot = (W - left - right) / keys.length;
    function y(v) { return top + (hi - v) / range * plotH; }
    var zero = y(0), out = ['<svg class="trend-chart" viewBox="0 0 600 220" role="img" aria-label="月度申请金额与报销金额对比">'];
    for (var n = 0; n <= 3; n++) {
      var value = lo + range * n / 3, yy = y(value);
      out.push('<line x1="' + left + '" y1="' + yy + '" x2="584" y2="' + yy + '" stroke="' + gridColor + '" stroke-dasharray="3 5"/><text x="48" y="' + (yy + 4) + '" text-anchor="end" fill="' + axisColor + '" font-size="11">' + num(value / 10000, 1) + '</text>');
    }
    keys.forEach(function (key, i) {
      var center = left + slot * (i + .5), bw = Math.min(14, slot * .28);
      ["apply", "reimb"].forEach(function (kind, j) {
        var v = Number(monthly[key][kind]) || 0, yy = y(v), height = Math.abs(zero - yy);
        out.push('<rect x="' + (center + (j ? 2 : -bw - 2)) + '" y="' + Math.min(yy, zero) + '" width="' + bw + '" height="' + height + '" rx="2" fill="' + (j ? seriesB : seriesA) + '"><title>' + esc(key) + ' ' + (j ? '报销' : '申请') + '：' + money(v) + '</title></rect>');
      });
      if (keys.length <= 6 || i % 2 === 0 || i === keys.length - 1) out.push('<text x="' + center + '" y="207" text-anchor="middle" fill="' + axisColor + '" font-size="11">' + esc(key.slice(2)) + '</text>');
    });
    out.push('</svg>');
    return out.join('');
  }

  function enhanceExpense(d) {
    var root = $("v-expense"), charts = root.querySelector(".chart-grid");
    var heads = Array.prototype.filter.call(root.children, function (el) { return el.classList.contains("sec"); }).slice(1);
    if (!heads.length) return;
    var active = Math.min(S.expenseTab || 0, heads.length - 1);
    var card = document.createElement("div"); card.className = "card detail-card";
    card.innerHTML = '<div class="card-heading"><h3>费用明细</h3><span>按余额排序 · 金额单位：元</span></div><div class="detail-tabs" role="tablist" aria-label="费用明细类型"></div>';
    var labels = ["借款未还", "未报销申请", "报销未付款", "审批中 / 草稿", "人员汇总"];
    var counts = [d.borrow_bills, d.apply_bills, d.unpaid_bills, d.pending_bills, d.staff_rows];
    heads.forEach(function (head, i) {
      var tableEl = head.nextElementSibling, panel = document.createElement("div"), button = document.createElement("button");
      panel.className = "detail-panel" + (i === active ? " on" : ""); panel.id = "expense-detail-" + i;
      panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", "expense-tab-" + i);
      button.id = "expense-tab-" + i; button.type = "button"; button.className = i === active ? "on" : "";
      button.setAttribute("role", "tab"); button.setAttribute("aria-selected", String(i === active));
      button.setAttribute("aria-controls", panel.id); button.tabIndex = i === active ? 0 : -1;
      button.innerHTML = labels[i] + '<span class="tab-count">' + (counts[i] || []).length + '</span>';
      var sub = head.querySelector(".sub");
      panel.innerHTML = '<div class="detail-toolbar">' + esc(sub ? sub.textContent : "") + '</div>';
      panel.appendChild(tableEl); head.remove();
      card.querySelector(".detail-tabs").appendChild(button); card.appendChild(panel);
      button.addEventListener("click", function () {
        S.expenseTab = i;
        Array.prototype.forEach.call(card.querySelectorAll('[role="tab"]'), function (b) {
          b.classList.toggle("on", b === button); b.setAttribute("aria-selected", String(b === button)); b.tabIndex = b === button ? 0 : -1;
        });
        Array.prototype.forEach.call(card.querySelectorAll(".detail-panel"), function (p) { p.classList.toggle("on", p === panel); });
      });
      button.addEventListener("keydown", function (e) {
        var tabs = card.querySelectorAll('[role="tab"]'), target;
        if (e.key === "ArrowRight") target = (i + 1) % tabs.length;
        if (e.key === "ArrowLeft") target = (i + tabs.length - 1) % tabs.length;
        if (e.key === "Home") target = 0;
        if (e.key === "End") target = tabs.length - 1;
        if (target !== undefined) { e.preventDefault(); tabs[target].click(); tabs[target].focus(); }
      });
    });
    root.appendChild(card);
    if (charts) root.insertBefore(charts, card);
  }

  /* ---------------- 管理会计看板（旧版单模块渲染，仅主题切换时兜底重画用） ---------------- */
  function renderMgmtLegacy(d) {
    if (!d) return;
    S.lastData.mgmt = d;
    var f = d.fund || {}, s = d.subject || {}, g = f.grand || {};
    var h = [];
    
    h.push(dataStatus(d, "组织范围：" + esc(((d.scope || {}).orgs || []).join(" / ")) +
      "<br>环比期间：" + esc(d.period.prev.year) + "-" + esc(d.period.prev.month),
      orgLabel() + " · 会计期间 " + d.period.year + "-" + d.period.month));
    orgDoneNotice();
    (d.notes || []).forEach(function (n) {
      h.push('<div class="banner warn">' + esc(n) + "</div>");
    });

    h.push('<div class="grid g4">' +
      kpi("今日头寸期末", wan(g.end), "来源：金蝶原生资金头寸表<br>银行 + 现金账户期末余额", "green") +
      kpi("今日收入 / 支出", wan(g.income) + " / " + wan(g.outcome),
        "原生资金头寸表 · 当日发生额", "blue") +
      kpi("今日净变动", g.income == null || g.outcome == null ? "—" : money(g.income - g.outcome), "收入 − 支出", (g.income - g.outcome) < 0 ? "green" : "red") +
      kpi("覆盖范围", num(f.account_count, 0) + " 个账户",
        "涉及 " + num(f.org_count, 0) + " 个组织<br>按组织逐个取数后按账号去重", "gray") +
      "</div>");
    h.push('<details class="data-note"><summary>口径提示：头寸与科目余额仅作交叉验证，不直接相减</summary><div class="hint">头寸来自金蝶原生资金头寸表（CN_FundPositionReport），按银行 / 现金账户统计，不依赖会计入账。科目余额的取数范围不同。</div></details>');

    h.push('<div class="sec">按组织资金头寸 <span class="sub">来源：原生资金头寸表 · 按组织逐个取数</span></div>');
    h.push(table([{ t: "组织" }, { t: "账户数", num: 1 }, { t: "期初", num: 1 }, { t: "今日收入", num: 1 },
      { t: "今日支出", num: 1 }, { t: "期末", num: 1 }],
      (f.rows || []).map(function (o) {
        return "<tr><td>" + esc(o.org_number ? o.org_number + " " + o.org_name : o.org_name) +
          "</td><td class='num'>" + num((o.accounts || []).length, 0) + "</td><td class='num'>" +
          money(o.start) + "</td><td class='num'>" + money(o.income) + "</td><td class='num'>" +
          money(o.outcome) + "</td><td class='num " + (Number(o.end) < 0 ? "up" : "") +
          "'>" + money(o.end) + "</td></tr>";
      })));
    if (!(f.rows || []).length) {
      h.push('<div class="banner warn">资金头寸报表没有返回数据。若当前账号无资金/出纳权限，属正常；' +
        "也可能是该账套的报表方案与默认参数不同。</div>");
    }

    var cc = d.cross_check || {};
    h.push('<div class="sec">交叉验证：头寸 vs 货币资金科目余额 ' +
      '<span class="sub">科目余额依赖会计入账，仅作参考，不相减求总差额</span></div>');
    h.push('<div class="banner warn">' + esc(cc.why || "") + "</div>");
    h.push(table([{ t: "组织" }, { t: "头寸账户数", num: 1 }, { t: "头寸期末", num: 1 },
      { t: "货币资金科目余额", num: 1 }, { t: "差额", num: 1 }],
      (cc.both || []).map(function (x) {
        return "<tr><td>" + esc(x.org_number + " " + x.org_name) + "</td><td class='num'>" +
          num(x.accounts, 0) + "</td><td class='num'>" + money(x.fund_end) + "</td><td class='num'>" +
          money(x.subject) + "</td><td class='num " + (Math.abs(x.delta) < 1 ? "" : "up") +
          "'>" + money(x.delta) + "</td></tr>";
      })));
    if ((cc.only_in_subject || []).length) {
      h.push('<div class="banner info">以下组织<b>有货币资金科目余额，但在资金头寸表里没有登记银行/现金账户</b>' +
        "（所以它们不会出现在上面的头寸合计里，这通常不是错误，而是账户尚未在系统登记）：<br>" +
        cc.only_in_subject.map(function (x) {
          return esc((x.org_number || "") + " " + (x.org_name || x.book_name)) + " → " +
            money(x.fund_balance);
        }).join("　·　") + "</div>");
    }
    if ((cc.only_in_fund || []).length) {
      h.push('<div class="banner info">以下组织在头寸表里有账户，但没有匹配到账簿科目余额：' +
        cc.only_in_fund.map(function (x) {
          return esc((x.org_number || "") + " " + (x.org_name || ""));
        }).join("　·　") + "</div>");
    }

    h.push('<div class="sec">科目余额（货币资金）明细 <span class="sub">按账簿 · 账簿→组织自动匹配</span></div>');
    h.push(table([{ t: "账簿编码" }, { t: "账簿名称" }, { t: "归属组织" }, { t: "货币资金余额", num: 1 }],
      (s.rows || []).map(function (x) {
        return "<tr><td class='mono'>" + esc(x.book_number) + "</td><td>" + esc(x.book_name) +
          "</td><td>" + esc(x.org_number ? x.org_number + " " + (x.org_name || "") : "未匹配到组织") +
          "</td><td class='num'>" + money(x.fund_balance) + "</td></tr>";
      })));

    h.push('<div class="sec">科目余额波动 Top <span class="sub">本期发生额（借+贷）环比上月，' +
      "只列明细科目、变动 ≥ 5000 元</span></div>");
    h.push(table([{ t: "科目编码" }, { t: "科目名称" }, { t: "账簿 / 组织" }, { t: "本期发生", num: 1 },
      { t: "上月发生", num: 1 }, { t: "变动额", num: 1 }, { t: "环比" }],
      (d.swings || []).map(function (x) {
        var up = Number(x.delta) > 0;
        return "<tr><td class='mono'>" + esc(x.code) + "</td><td>" + esc(x.name) + "</td><td>" +
          esc(x.book_name || "") + (x.org_number ? " / " + esc(x.org_number) : "") +
          "</td><td class='num'>" + money(x.cur) + "</td><td class='num'>" + money(x.prev) +
          "</td><td class='num " + (up ? "up" : "down") + "'>" + money(x.delta) + "</td><td>" +
          (x.rate === null ? "—" : '<span class="pill ' + (up ? "p-red" : "p-green") + '">' +
            (up ? "+" : "") + pct(x.rate) + "</span>") + "</td></tr>";
      })));

    h.push('<div class="hint">科目余额表方案 ID：<span class="mono">' + esc(s.scheme_id || "") +
      "</span>（环境相关，可在 portal_config.json 里改 scheme_id；方案不对会直接报错而不是静默出错）。</div>");

    $("v-mgmt").innerHTML = h.join("");
    Array.prototype.forEach.call($("v-mgmt").querySelectorAll(".sec"), function (head) {
      var card = document.createElement("div"); card.className = "card detail-card";
      var title = document.createElement("div"); title.className = "card-heading";
      var content = [], next = head.nextElementSibling;
      while (next && !next.classList.contains("sec")) {
        content.push(next); next = next.nextElementSibling;
      }
      head.parentNode.insertBefore(card, head);
      title.appendChild(head); card.appendChild(title);
      content.forEach(function (el) { card.appendChild(el); });
    });
  }

  /* ---------------- 组织与权限 ---------------- */
  function renderOrgsView() {
    var h = [];
    h.push('<div class="sec" style="margin-top:0">自动发现的组织 <span class="sub">' +
      S.orgs.length + " 个 · 组织名不内置</span></div>");
    h.push(table([{ t: "组织编码" }, { t: "组织名称" }, { t: "来源" }],
      S.orgs.map(function (o) {
        return "<tr><td class='mono'>" + esc(o.number) + "</td><td>" + esc(o.name || "（无名称）") +
          "</td><td><span class='pill p-gray'>" + esc((o.sources || []).join(" + ")) + "</span></td></tr>";
      })));

    h.push('<div class="sec">发现过程 <span class="sub">每一级为什么成功/失败都摆出来，不猜</span></div>');
    h.push(table([{ t: "层级" }, { t: "来源" }, { t: "结果" }, { t: "说明" }],
      S.trace.map(function (t) {
        return "<tr><td>" + esc(t.level) + "</td><td class='mono'>" + esc(t.form || "—") +
          "</td><td>" + (t.ok ? '<span class="pill p-green">成功</span> ' +
            (t.rows !== undefined ? esc(t.rows) + " 行" : "") :
            '<span class="pill p-red">失败</span>') + "</td><td class='muted'>" +
          esc(t.message || "") + "</td></tr>";
      })));
    S.notes.forEach(function (n) {
      h.push('<div class="banner info">' + esc(n) + "</div>");
    });

    h.push('<div class="sec">这套机制下的权限边界 <span class="sub">如实说明，不粉饰</span></div>');
    h.push('<div class="card"><table class="tbl"><tbody>' +
      [["门户是否自己判断权限", "否。每个请求都用你本人的金蝶会话去取数，金蝶拒绝就是拒绝。"],
       ["取不到数据时怎么办", "如实显示为空，不做兜底、不缓存成假数据、不换账号重试。"],
       ["组织列表怎么来", "① 组织基础资料（需「组织机构查看权限」）→ ② 账簿 → ③ 你有权看到的单据。"],
       ["普通员工登录会看到什么", "大概率两个看板都是空的：因为费用/资金单据通常不在员工权限内。这是金蝶权限的如实反映。"],
       ["账号密码去哪了", "只在登录时用于换会话，随后从内存丢弃；不落盘、不写日志、不发给第三方。"]]
        .map(function (r) {
          return "<tr><td style='width:190px;color:var(--t2)'>" + esc(r[0]) + "</td><td>" +
            esc(r[1]) + "</td></tr>";
        }).join("") + "</tbody></table></div>");

    $("v-orgs").innerHTML = h.join("");
  }

  /* ---------------- 数据源与口径 ---------------- */
  function clearPortalCache() {
    var n = 0;
    try {
      Object.keys(window.localStorage).forEach(function (k) {
        if (k.indexOf(CACHE_PREFIX) === 0) { window.localStorage.removeItem(k); n += 1; }
      });
    } catch (e) { /* 忽略 */ }
    return n;
  }

  function renderSourceView() {
    var rows = [
      ["借款未还", "ER_ExpenseRequest / _Travel｜ExecuteBillQuery 一次取回表头+单据体",
       "Σ(实际借款 − 已报销 − 已还款 − 核销)，仅「申请借款=是」且已审核", "实时"],
      ["已申请未报销", "同上", "申请金额 − Σ(已下推报销单金额)，仅「申请借款=否」且关闭状态=否", "实时"],
      ["报销未付款", "ER_ExpReimbursement / _Travel｜同上",
       "Σ(单据体 FReimbNotPayAmount)，仅已审核；表头没有这个字段，必须取单据体", "实时"],
      ["审批中 / 草稿", "四类单据表头", "单据状态 B / Z 且未作废", "实时"],
      ["费用类型分布", "报销单单据体 FExpID.FName", "费用项目名称汇总，Top 8 + 其他", "实时"],
      ["资金头寸", "金蝶原生报表 CN_FundPositionReport（资金头寸表）",
       "银行 + 现金账户，期初/当日收支/期末。按组织逐个取数后按银行账号去重；不依赖会计入账", "实时"],
      ["货币资金科目余额", "金蝶原生报表 GL_RPT_AccountBalance（科目余额表）",
       "科目 1001/1002/1012 的叶子科目 Σ(期末借方 − 期末贷方)；参数=账簿 + 方案 ID。"
       + "属会计入账口径（含未达账项），只作交叉验证", "实时"],
      ["科目余额波动", "同上", "本期发生额(借+贷) − 上月发生额，仅叶子科目且变动≥5000", "实时"],
      ["组织列表", "组织基础资料 → 账簿 BD_AccountBook → 业务单据组织字段",
       "三级自动发现；不内置组织名、不内置组织编码", "登录时 / 手动重取"]
    ];
    var h = [intro("数据源与口径", "每个指标都可追溯，数据流向与留存也在这里说清楚。"),
      /* ★ 2026-10-17：数据流向与留存（客户/财务最关心的问题，一次讲清） */
      '<div class="sec" style="margin-top:0">数据流向与留存 <span class="sub">' +
      "数据从哪来、经过哪、存多久、怎么删 —— 采购/安全审查常问的就是这些</span></div>" +
      '<div class="flow">' +
      '<div class="flow-node"><b>金蝶云星空</b><span>数据源头 · 你的账套</span></div>' +
      '<div class="flow-arrow">HTTPS · WebAPI</div>' +
      '<div class="flow-node hot"><b>门户服务器</b><span>内存 30 分钟 · 不写硬盘</span></div>' +
      '<div class="flow-arrow">HTTPS</div>' +
      '<div class="flow-node"><b>你的浏览器</b><span>本机缓存 24 小时</span></div>' +
      "</div>",
      '<div class="hint" style="margin:-4px 0 12px">' +
      "<b>我们不做的事</b>：不把看板业务数据（金额/客户/单据）写入数据库或任何对象存储；" +
      "不发往任何第三方云；不跨公司共享缓存（缓存键带公司码）；不做权限判断缓存（每次取数都用登录者本人身份）。<br>" +
      "<b>后台预热</b>：服务器会定期（默认 20 分钟）主动取一次「销售出库 / 费用报销」放进内存，" +
      "这样你打开就是秒开；同样是内存、不落盘，可在 portal_config.json 里关闭（warmup_token 置空）。</div>" +
      table([{ t: "数据项" }, { t: "存在哪" }, { t: "留存多久" }, { t: "加密" }],
        [["看板业务数据（金额/客户/单据）", "门户服务器内存 + 你的浏览器缓存",
          "内存 30 分钟；浏览器 24 小时", "不落盘"],
         ["金蝶账号密码", "门户本机 state.db（自动重登用）",
          "直到退出或换绑", "Fernet 加密，密钥单独文件"],
         ["登录记录（用户名/时间/成败）", "门户本机 state.db", "长期", "未加密（不含密码）"],
         ["公司配置（公司码/服务器/账套/组织）", "门户本机 state.db", "长期", "未加密"],
         ["报销写操作审计（单号/改前改后金额）", "portal_audit.log", "长期", "未加密"],
         ["发票云接口密钥", "只在服务进程内存", "重启即失效", "不落盘"]
        ].map(function (r) {
          return "<tr><td><b>" + esc(r[0]) + "</b></td><td>" + esc(r[1]) + "</td><td>" +
            esc(r[2]) + "</td><td>" + esc(r[3]) + "</td></tr>";
        })) +
      '<div class="row-actions" style="margin:10px 0 16px">' +
      '<button type="button" class="btn btn-ghost" id="btn-clear-cache">清除本机缓存</button>' +
      '<span class="hint" id="clear-cache-msg" style="margin-left:10px">' +
      "只清当前浏览器里的看板缓存，不影响金蝶里的任何数据</span></div>",
      '<div class="sec" style="margin-top:0">报销管理：写什么、以谁的身份写</div>' +
      '<div class="hint" style="margin:0 0 14px">' +
      "<b>三个写动作</b>：下推报销单 / 建收票单 / 提交审批。<br>" +
      "<b>以谁的身份</b>：以<b>登录者本人</b>的金蝶身份执行 —— 他只能操作自己有权动的单据，" +
      "门户不多放权，也不代替金蝶做判断。<br>" +
      "<b>审计</b>：每个动作写入 portal_audit.log，含操作账号与结果。<br>" +
      "<b>发票云密钥</b>：公司级接口密钥，在「发票云配置」页维护；只存服务进程内存，" +
      "不回显、不落盘，服务重启后需重填。<br>" +
      "<b>员工账号</b>：密码只用于建立会话，只在内存，从不落盘。</div>", '<div class="sec" style="margin-top:0">每个指标的出处 <span class="sub">' +
      "点开就能对账，对不上说明口径不同而不是有人算错</span></div>"];
    h.push(table([{ t: "门户指标" }, { t: "数据源（金蝶）" }, { t: "口径 / 公式" }, { t: "取数时机" }],
      rows.map(function (r) {
        return "<tr><td><b>" + esc(r[0]) + "</b></td><td class='mono'>" + esc(r[1]) + "</td><td>" +
          esc(r[2]) + "</td><td class='muted'>" + esc(r[3]) + "</td></tr>";
      })));

    h.push('<div class="sec">已知边界 <span class="sub">与 kingdee-data-exporter 的能力边界一致</span></div>');
    h.push('<div class="card"><table class="tbl"><tbody>' +
      [       ["金蝶 WebAPI 每分钟调用上限 500 次", "账号级硬限流。本门户用 420 次/分钟滑动窗口限速 + 限流降速重试。" +
        "费用看板已改为「一次查询取回单据体」，全量只需个位数调用，基本不占配额。"],
       ["单据体字段可以一次查回来", "实测 ExecuteBillQuery 的 FieldKeys 直接写单据体字段名（无需 FEntity_ 前缀）即可，" +
        "返回一行一条单据体的明细表、表头字段在每行重复。金额与逐张 View 抽样逐单核对一致。"],
       ["待我审批", "WebAPI 查不到审批进度（不在业务对象元数据里），只能给「单据状态 + 申请人」清单。"],
       ["字段权限对 WebAPI 不完全生效", "界面上看不到的字段，API 有时能查到；价格类字段还可能有服务端脱敏（返回 ***）。"],
       ["报表类对象不能用通用取数", "科目余额/资金头寸走报表接口，需要账簿编码与方案 ID。"],
       ["组织基础资料常被权限挡住", "本次实测就是「您没有组织机构的查看权限」，所以组织列表改为从有权数据的账簿/单据反推。"]]
        .map(function (r) {
          return "<tr><td style='width:210px;color:var(--t2)'>" + esc(r[0]) + "</td><td>" +
            esc(r[1]) + "</td></tr>";
        }).join("") + "</tbody></table></div>");

    $("v-source").innerHTML = h.join("");
    var cc = $("btn-clear-cache");
    if (cc) {
      cc.addEventListener("click", function () {
        var n = clearPortalCache();
        var msg = $("clear-cache-msg");
        if (msg) msg.innerHTML = "<b>已清除 " + n + " 项本机缓存</b>。下次打开看板会重新取数（约 10–60 秒）。";
      });
    }
  }

  /* ---------------- 启动 ---------------- */
  function boot() {
    initTheme();
    // ★ 根地址 = 管理员控制台：**即使带着公司会话 cookie 也不自动进工作台**。
    //   🔴 踩过的坑：原来只看 CFG.loggedIn —— 员工登录过之后打开根地址会直接进到
    //   自己的报销工作台，把管理员登录屏整个绕过（用户看到的"根地址变成了公司界面"）。
    var tc = "", ap = "";
    try {
      var q = new URLSearchParams(window.location.search);
      tc = q.get("c") || ""; ap = q.get("apply") || "";
    } catch (e) { /* 忽略 */ }
    if (ap === "1" && !CFG.localMode) { showApply(); return; }        // ★ 自助申请开通
    if (!tc && CFG.localMode) window.__localLogin = true;
    if (!tc && !CFG.localMode) {
      $("screen-app").hidden = true;
      $("screen-login").hidden = true;
      initLogin();
      return;
    }
    if (CFG.localMode && !CFG.loggedIn) { $("screen-app").hidden = true; initLogin(); return; }
    if (!CFG.loggedIn) { initLogin(); return; }
    $("screen-app").hidden = false;
    bindApp();
    var first = true;
    function refreshMe() {
      api("/api/me", {}, "GET").then(function (r) {
        if (!r.ok) { location.reload(); return; }
        setUser(r.login);
        S.orgs = r.orgs || []; S.trace = r.org_trace || []; S.notes = r.notes || [];
        if (!S.orgs.length && first) {
          first = false;
          prog(true, "正在自动发现组织…", 5);
          api("/api/orgs/rediscover", {}).then(function (rr) {
            if (rr.ok) poll(rr.job, function (res) {
              S.orgs = res.orgs || []; S.trace = res.trace || []; S.notes = res.notes || [];
              S.selected = S.orgs.map(function (o) { return o.number; });
              renderOrgChips(); renderOrgsView(); renderSourceView(); syncOrgSummary(); loadExpense(false);
            });
          });
          return;
        }
        S.selected = S.orgs.map(function (o) { return o.number; });
        renderOrgChips(); renderOrgsView(); renderSourceView();
        syncOrgSummary();
        loadExpense(false);
      });
    }
    refreshMe();
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!CFG.localMode) { boot(); return; }
    // The credential stays in this tab's session storage, not localStorage.
    // Wait for the handshake before allowing login or any API call.
    var previous = _embeddedCreds();
    fetch("/api/bootstrap", {
      credentials: "same-origin",
      headers: previous && previous.sid ? {"X-Portal-Sid": previous.sid} : {}
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (!j.ok || !j.sid || !j.csrf) throw new Error("bootstrap failed");
      _embeddedBoot = {sid: j.sid, csrf: j.csrf};
      try { sessionStorage.setItem("portal_boot_local", JSON.stringify(_embeddedBoot)); } catch (e) {}
      return api("/api/me", null, "GET");
    }).then(function (me) {
      CFG.loggedIn = !!me.ok;
      boot();
    }).catch(function () {
      // Fail closed: do not show a nonfunctional login form without a CSRF token.
      document.body.textContent = "无法连接本地门户服务，请重新打开插件后重试。";
    });
  });
})();
