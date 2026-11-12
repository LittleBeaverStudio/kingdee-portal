window.__ModuleLoader__.load({
  id: "@littlebeaverstudio/kingdee-portal",
  factory(require) {
    const React = require("react");
    const h = React.createElement;

    /**
     * Portal origin. The Host half spawns `scripts/serve_local.py --port <port>`,
     * and its default port is 8985 — keep this constant in sync when the row's
     * `port` config is changed.
     */
    const PORTAL_URL = "http://127.0.0.1:8985";
    const KIND = "kingdee-portal";
    const NS = "kingdeePortal";

    const zh = {
      title: "金蝶门户",
      guideTitle: "金蝶经营门户",
      guideDescription: "在侧栏打开完整的经营门户界面",
      loading: "正在打开门户…",
      failed: "门户页面无法加载",
      failedHint: "本地服务可能尚未启动。点「重新加载」再试，或在浏览器中直接打开。",
      retry: "重新加载",
      external: "在浏览器中打开",
      unavailable: "门户地址未就绪",
    };

    const en = {
      title: "Kingdee Portal",
      guideTitle: "Kingdee business portal",
      guideDescription: "Open the full portal interface in the sidebar",
      loading: "Opening the portal…",
      failed: "The portal page did not load",
      failedHint: "The local service may not be running yet. Retry, or open the address in a browser.",
      retry: "Reload",
      external: "Open in browser",
      unavailable: "Portal address not ready",
    };

    /** Portal glyph: four panes, echoing the portal's dashboard grid artwork. */
    function PortalIcon({ size = 22 }) {
      return h(
        "svg",
        {
          width: size,
          height: size,
          viewBox: "0 0 24 24",
          fill: "none",
          "aria-hidden": true,
          style: { flex: "none", color: "var(--dsw-alias-label-secondary)" },
        },
        h("rect", { x: 3, y: 3, width: 7.5, height: 7.5, rx: 2, fill: "currentColor" }),
        h("rect", { x: 13.5, y: 3, width: 7.5, height: 7.5, rx: 2, fill: "currentColor", opacity: 0.5 }),
        h("rect", { x: 3, y: 13.5, width: 7.5, height: 7.5, rx: 2, fill: "currentColor", opacity: 0.5 }),
        h("rect", { x: 13.5, y: 13.5, width: 7.5, height: 7.5, rx: 2, fill: "currentColor", opacity: 0.28 }),
      );
    }

    /**
     * Tab body: the complete portal interface, embedded from the local service.
     *
     * The frame is always rendered and never covered: a cross-origin frame reports no
     * error, and a probe request from this page would depend on the shell's own
     * connect policy — so nothing here gates on a fetch. A late-loading hint appears
     * below the frame (not over it) and the portal itself stays interactable.
     */
    function PortalBody({ t }) {
      const [loaded, setLoaded] = React.useState(false);
      const [slow, setSlow] = React.useState(false);
      const [attempt, setAttempt] = React.useState(0);

      React.useEffect(() => {
        if (loaded) return undefined;
        const timer = setTimeout(() => setSlow(true), 6000);
        return () => clearTimeout(timer);
      }, [loaded, attempt]);

      const reload = () => {
        setLoaded(false);
        setSlow(false);
        setAttempt((value) => value + 1);
      };

      const hintBar = {
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexWrap: "wrap",
        gap: 8,
        padding: "6px 12px",
        flex: "none",
        borderTop: "0.5px solid var(--dsw-alias-border-l2)",
        background: "var(--dsw-alias-bg-layer-1)",
        color: "var(--dsw-alias-label-secondary)",
        fontSize: 11,
        lineHeight: 1.5,
      };

      const smallButton = {
        font: "inherit",
        fontSize: 11,
        cursor: "pointer",
        borderRadius: 6,
        padding: "3px 10px",
        border: "0.5px solid var(--dsw-alias-border-l2)",
        background: "var(--dsw-alias-bg-layer-2)",
        color: "var(--dsw-alias-label-primary)",
      };

      return h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "column",
            width: "100%",
            height: "100%",
            minWidth: 0,
            minHeight: 0,
            overflow: "hidden",
          },
        },
        h(
          "div",
          {
            style: {
              position: "relative",
              flex: "1 1 auto",
              minHeight: 0,
              minWidth: 0,
              display: "flex",
            },
          },
          h("iframe", {
            key: attempt,
            src: PORTAL_URL,
            title: t("title"),
            style: {
              border: "none",
              flex: "1 1 auto",
              width: "100%",
              height: "100%",
              display: "block",
              background: "var(--dsw-alias-bg-layer-1)",
            },
            allow: "clipboard-read; clipboard-write; downloads; fullscreen",
            referrerPolicy: "no-referrer",
            onLoad: () => setLoaded(true),
          }),
        ),
        !loaded && slow
          ? h(
              "div",
              { style: hintBar },
              h("span", null, t("failed")),
              h("button", { type: "button", onClick: reload, style: smallButton }, t("retry")),
              h(
                "a",
                {
                  href: PORTAL_URL,
                  target: "_blank",
                  rel: "noreferrer",
                  style: { ...smallButton, textDecoration: "none" },
                },
                t("external"),
              ),
            )
          : null,
      );
    }

    /** Tab chip: glyph plus the localized portal name. */
    function PortalTitle({ t }) {
      return h(
        "span",
        { style: { display: "inline-flex", alignItems: "center", gap: 6, minWidth: 0 } },
        h(PortalIcon, { size: 14 }),
        h(
          "span",
          { style: { whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } },
          t("title"),
        ),
      );
    }

    return {
      inject: ["slots", "sidebarRightTabs", "locale"],
      apply(ctx) {
        ctx.effect(() => ctx.locale.register(NS, { zh, en }), "kingdee-portal: copy");
        const t = ctx.locale.bind(NS);

        // Global left navigation entry and matching central panel share an id.
        ctx.effect(() => ctx.slots.inject("sidebar.panellist", () =>
          ctx.slots.register({
            name: "sidebar.panellist",
            id: "kingdee-portal",
            order: 5,
            label: () => t("title"),
          }, PortalIcon)), "kingdee-portal: left navigation");
        ctx.effect(() => ctx.slots.inject("main", () =>
          ctx.slots.register({ name: "main", key: "kingdee-portal", locale: NS }, PortalBody)),
          "kingdee-portal: main panel");

        // Tab type plus the guide card metadata the start page renders.
        ctx.effect(
          () =>
            ctx.sidebarRightTabs.register({
              id: "@littlebeaverstudio/kingdee-portal",
              kind: KIND,
              multiple: false,
              // Keep the frame mounted across tab switches: the portal keeps its page
              // state and does not re-run its bootstrap handshake on every return.
              keepMounted: true,
              priority: "extension",
              title: () => t("title"),
              guide: [
                {
                  id: "open",
                  order: 20,
                  title: () => t("guideTitle"),
                  description: () => t("guideDescription"),
                  icon: PortalIcon,
                },
              ],
            }),
          "kingdee-portal: tab type",
        );

        ctx.effect(
          () =>
            ctx.slots.inject("sidebar.right.pane.tab", () =>
              ctx.slots.register(
                {
                  name: "sidebar.right.pane.tab",
                  key: "@littlebeaverstudio/kingdee-portal",
                  locale: NS,
                },
                PortalBody,
              ),
            ),
          "kingdee-portal: tab body",
        );

        ctx.effect(
          () =>
            ctx.slots.inject("sidebar.right.pane.tab.title", () =>
              ctx.slots.register(
                {
                  name: "sidebar.right.pane.tab.title",
                  key: "@littlebeaverstudio/kingdee-portal",
                  locale: NS,
                },
                PortalTitle,
              ),
            ),
          "kingdee-portal: tab title",
        );
      },
    };
  },
});
