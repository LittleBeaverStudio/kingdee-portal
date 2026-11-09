/* 主题引导：必须放在样式表之后、页面渲染之前同步执行，避免配色闪烁。
   ★ 2026-10-17：默认改成**浅色**，对齐「管理会计/经营管理仪表盘」的白底蓝调。
     之前默认跟随系统偏好，而发布者系统是暗色 → 打开门户永远是深色底，
     看起来像"没沿用定时任务的配色"。用户在门户里切换过的选择仍然优先。 */
(function () {
  "use strict";
  var KEY = "portal_theme";

  function systemTheme() {
    try {
      return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
        ? "light" : "dark";
    } catch (e) {
      return "dark";
    }
  }

  var theme = "light";                    // 默认浅色
  try {
    var saved = window.localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") theme = saved;
  } catch (e) { /* 隐私模式下忽略 */ }

  document.documentElement.setAttribute("data-theme", theme);

  window.portalTheme = {
    key: KEY,
    system: systemTheme,
    current: function () {
      return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    },
    save: function (value) {
      try { window.localStorage.setItem(KEY, value); } catch (e) { /* 忽略 */ }
    }
  };
})();
