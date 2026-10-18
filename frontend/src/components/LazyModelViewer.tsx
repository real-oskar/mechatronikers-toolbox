import { lazy, Suspense } from "react";
import type { ComponentProps } from "react";

import type { ModelViewer as Viewer } from "./ModelViewer";

// three.js is most of the bundle: load it only when a model is on screen.
const ModelViewer = lazy(() => import("./ModelViewer").then((m) => ({ default: m.ModelViewer })));

export function LazyModelViewer(props: ComponentProps<typeof Viewer>) {
  return (
    <Suspense fallback={<div className="viewport-empty">…</div>}>
      <ModelViewer {...props} />
    </Suspense>
  );
}
