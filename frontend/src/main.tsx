import { StrictMode } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";

import { App, ConfigFailed } from "./App";
import { loadConfig } from "./config";
import "./fonts";
import { LangProvider } from "./i18n";
import "./styles.css";

const root = createRoot(document.getElementById("root")!);
const render = (screen: ReactNode) =>
  root.render(
    <StrictMode>
      <LangProvider>{screen}</LangProvider>
    </StrictMode>,
  );

// The same build serves every mode; `/config.json` from the serving app says which (D74).
loadConfig().then(
  (config) => render(<App config={config} />),
  (e: unknown) => render(<ConfigFailed error={e instanceof Error ? e.message : String(e)} />),
);
