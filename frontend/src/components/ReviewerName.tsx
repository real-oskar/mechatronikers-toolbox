import { useState } from "react";

import { MAX_NAME } from "../api/types";

const KEY = "mt.reviewer";

function read(): string {
  try {
    return sessionStorage.getItem(KEY) ?? "";
  } catch {
    return ""; // storage blocked: asked again in each view
  }
}

/** Phase 1 reviewer identity (P4): a name entered once per browser session. */
export function useReviewer(): [string, (name: string) => void] {
  const [name, setName] = useState(read);
  const update = (value: string) => {
    const trimmed = value.slice(0, MAX_NAME);
    setName(trimmed);
    try {
      sessionStorage.setItem(KEY, trimmed);
    } catch {
      // not persisted; still used in this view
    }
  };
  return [name, update];
}
