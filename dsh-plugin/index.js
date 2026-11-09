/**
 * Host half of the Kingdee Portal bundle.
 *
 * Owns the local Flask portal process: starts it on activation (when the port has
 * no listener yet), keeps a handle for the lifetime of the plugin row, and reaps
 * only the process this plugin itself started. It also publishes one agent tool so
 * the assistant can check or start the same service the sidebar tab renders.
 */
import http from "node:http";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Row config accepted by this bundle (read from `cordis.patch.yml` or the row in
 * the profile's own patch):
 *
 *   projectDir  portal project directory containing scripts/serve_local.py;
 *               defaults to the repository checkout (one level above this package)
 *   port        loopback port the portal listens on
 *   pythonExe   absolute interpreter path; empty resolves "python" from PATH
 *   autoStart   start the portal on activation when the port has no listener
 *
 * No `Config` export: a declared schema must be a schemastery schema, and this
 * bundle lives outside the profile's dependency tree. Values are read defensively
 * below, so a partial row still activates.
 */
const REPO_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const DEFAULTS = {
  projectDir: REPO_ROOT,
  port: 8985,
  pythonExe: "",
  autoStart: true,
};

/** How long to wait for the Flask process to accept connections. */
const START_TIMEOUT_MS = 30000;
/** Poll cadence while waiting for the listener. */
const POLL_INTERVAL_MS = 250;

export const inject = ["subprocess"];

/** Probe a loopback port with a plain HTTP GET; resolves true only on a real response. */
function probe(port, timeoutMs = 1500) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const timer = setTimeout(() => finish(false), timeoutMs);
    let req;
    try {
      req = http.get(
        { host: "127.0.0.1", port, path: "/", timeout: timeoutMs, agent: false },
        (res) => {
          res.resume();
          clearTimeout(timer);
          finish(true);
        },
      );
    } catch {
      clearTimeout(timer);
      finish(false);
      return;
    }
    req.on("error", () => {
      clearTimeout(timer);
      finish(false);
    });
    req.on("timeout", () => {
      req.destroy();
      clearTimeout(timer);
      finish(false);
    });
  });
}

export function apply(ctx, config) {
  const cfg = { ...DEFAULTS, ...(config ?? {}) };
  const url = `http://127.0.0.1:${cfg.port}`;

  const state = {
    handle: null,
    startedByUs: false,
    disposed: false,
  };

  /** Portal runtime data (state.db / state.key) lives beside the project, not in $HOME. */
  function childEnv() {
    const sep = cfg.projectDir.includes("\\") ? "\\" : "/";
    const trimmed = cfg.projectDir.replace(/[\\/]+$/, "");
    let webOrigin = "";
    try {
      const advertised = ctx.get("webApp")?.getAdvertisedUrl();
      const parsed = new URL(advertised);
      if (parsed.protocol === "http:" && ["127.0.0.1", "localhost"].includes(parsed.hostname) && parsed.port) {
        webOrigin = parsed.origin;
      }
    } catch {
      // Desktop embedding still works. Unknown or non-loopback origins fail closed.
    }
    return {
      ...process.env,
      PYTHONIOENCODING: "utf-8",
      PYTHONUTF8: "1",
      PORTAL_LOCAL_MODE: "1",
      PORTAL_DSH_WEB_ORIGIN: webOrigin,
      PORTAL_STATE_DIR: `${trimmed}${sep}.portal-state-dsh`,
    };
  }

  /** Resolve the interpreter once; an absolute override wins over PATH lookup. */
  async function resolvePython() {
    const sub = ctx.subprocess;
    if (cfg.pythonExe) return sub.resolveExecutable(cfg.pythonExe);
    return sub.resolveExecutable("python");
  }

  /**
   * Ensure the portal answers on its port.
   * @returns {Promise<{url: string, listening: boolean, startedByUs: boolean}>}
   */
  async function ensureServer() {
    if (await probe(cfg.port)) {
      return { url, listening: true, startedByUs: state.startedByUs };
    }
    if (state.handle) {
      // A handle exists but the port is still closed: give the child a moment.
      const ready = await waitUntilListening();
      return { url, listening: ready, startedByUs: state.startedByUs };
    }

    const python = await resolvePython();
    if (state.disposed) return { url, listening: false, startedByUs: false };
    const handle = ctx.subprocess.spawn({
      argv: [
        python,
        "-X",
        "utf8",
        "scripts/serve_local.py",
        "--port",
        String(cfg.port),
        "--no-warmup",
      ],
      cwd: cfg.projectDir,
      stdio: {
        stdin: "ignore",
        stdout: { maxBytes: 65536, spill: { maxBytes: 1048576 } },
        stderr: { maxBytes: 65536, spill: { maxBytes: 1048576 } },
      },
      graceMs: 4000,
      env: childEnv(),
    });
    state.handle = handle;
    state.startedByUs = true;
    handle.done.then(
      () => {
        if (state.handle === handle) {
          state.handle = null;
          state.startedByUs = false;
        }
      },
      () => {
        if (state.handle === handle) {
          state.handle = null;
          state.startedByUs = false;
        }
      },
    );

    const listening = await waitUntilListening();
    return { url, listening, startedByUs: true };
  }

  async function waitUntilListening() {
    const deadline = Date.now() + START_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (await probe(cfg.port)) return true;
      if (!state.handle) return false;
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    }
    return false;
  }

  // Lifecycle: start on activation, reap our own child on unload.
  ctx.effect(() => {
    if (cfg.autoStart) {
      ensureServer().catch((error) => {
        // Surfaced by the sidebar tab's own "failed" state; log for diagnosis.
        console.warn(`kingdee-portal: could not start ${url}: ${String(error)}`);
      });
    }
    return () => {
      state.disposed = true;
      const handle = state.handle;
      state.handle = null;
      state.startedByUs = false;
      if (handle) {
        try {
          handle.terminate();
        } catch {
          /* already exited */
        }
      }
    };
  }, "kingdee-portal: server lifecycle");
}
