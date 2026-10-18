import { lazy, Suspense, useEffect, useMemo } from "react";

import { createRunApi, RunApiProvider } from "./api/client";
import type { RunBases } from "./api/client";
import { RunShell } from "./components/RunShell";
import { ConfigProvider } from "./config";
import type { RuntimeConfig } from "./config";
import { useI18n } from "./i18n";
import { runBases, useRoute } from "./router";

// The table is the host's (and later the hub's) screen: not loaded in the sandbox.
const RunTable = lazy(() => import("./components/RunTable").then((m) => ({ default: m.RunTable })));

function RunPage({ run, events, circuit }: RunBases) {
  const api = useMemo(() => createRunApi({ run, events, circuit }), [run, events, circuit]);
  return (
    <RunApiProvider value={api}>
      <RunShell />
    </RunApiProvider>
  );
}

function Missing({ path }: { path: string }) {
  const { t } = useI18n();
  return (
    <main className="page">
      <p className="notice notice-check">{t("pageMissing", { path })}</p>
    </main>
  );
}

/** One entry for every mode (D74): the runtime config and the route pick the screen. */
export function App({ config }: { config: RuntimeConfig }) {
  const route = useRoute();
  const table = route.page === "home" && config.features.table ? config.api.runs : undefined;
  const bases = runBases(config, route);
  useEffect(() => {
    document.title = table ? "Drawing2CAD · Runs" : "Drawing2CAD · Run";
  }, [table]);

  let screen;
  if (table) {
    screen = (
      <Suspense fallback={<p className="viewport-empty">…</p>}>
        <RunTable runs={table} />
      </Suspense>
    );
  } else if (bases) {
    screen = <RunPage key={bases.run} {...bases} />; // a new run starts from a clean state
  } else {
    screen = <Missing path={route.page === "missing" ? route.path : window.location.pathname} />;
  }
  return <ConfigProvider value={config}>{screen}</ConfigProvider>;
}

export function ConfigFailed({ error }: { error: string }) {
  const { t } = useI18n();
  return (
    <main className="page">
      <p className="notice notice-deviation">{t("configFailed", { error })}</p>
    </main>
  );
}
