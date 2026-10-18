import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { de } from "./de";
import { en } from "./en";
import type { Key } from "./en";

export type Lang = "de" | "en";
export type { Key };

const DICTIONARIES: Record<Lang, Record<Key, string>> = { de, en };
const LOCALES: Record<Lang, string> = { de: "de-DE", en: "en-GB" };
const STORAGE = "mt.lang";

function initialLang(): Lang {
  try {
    const stored = localStorage.getItem(STORAGE);
    if (stored === "de" || stored === "en") return stored;
  } catch {
    // storage blocked: fall back to the browser language
  }
  return typeof navigator !== "undefined" && navigator.language.startsWith("de") ? "de" : "en";
}

interface LangState {
  lang: Lang;
  setLang: (lang: Lang) => void;
}

const LangContext = createContext<LangState>({ lang: "en", setLang: () => undefined });

export function LangProvider({ children, lang: fixed }: { children: ReactNode; lang?: Lang }) {
  const [lang, setState] = useState<Lang>(() => fixed ?? initialLang());
  const setLang = useCallback((next: Lang) => {
    setState(next);
    try {
      localStorage.setItem(STORAGE, next);
    } catch {
      // per-viewer convenience only
    }
  }, []);
  const value = useMemo(() => ({ lang, setLang }), [lang, setLang]);
  return <LangContext.Provider value={value}>{children}</LangContext.Provider>;
}

export type Translate = (key: Key, vars?: Record<string, string | number>) => string;

export interface I18n {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: Translate;
  /** Numbers in the reviewer's locale (decimal comma in German). */
  num: (value: number, digits?: number) => string;
  time: (ts: string | number | null | undefined) => string;
  dateTime: (ts: string | number | null | undefined) => string;
  duration: (ms: number | undefined) => string;
}

export function useI18n(): I18n {
  const { lang, setLang } = useContext(LangContext);
  return useMemo(() => {
    const locale = LOCALES[lang];
    const dict = DICTIONARIES[lang];
    const t: Translate = (key, vars) =>
      dict[key].replace(/\{(\w+)\}/g, (_, name: string) => String(vars?.[name] ?? `{${name}}`));
    const number = (digits?: number) =>
      new Intl.NumberFormat(locale, { maximumFractionDigits: digits ?? 3, minimumFractionDigits: digits === undefined ? 0 : digits });
    const timeFormat = new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const dateTimeFormat = new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "short" });
    const unit = (value: number, u: "second" | "minute" | "hour") =>
      new Intl.NumberFormat(locale, { style: "unit", unit: u, unitDisplay: "narrow", maximumFractionDigits: 1 }).format(value);
    return {
      lang,
      setLang,
      t,
      num: (value, digits) => number(digits).format(value),
      time: (ts) => (ts === null || ts === undefined ? "–" : timeFormat.format(new Date(ts))),
      dateTime: (ts) => (ts === null || ts === undefined ? "–" : dateTimeFormat.format(new Date(ts))),
      duration: (ms) => {
        if (ms === undefined || !Number.isFinite(ms)) return "–";
        const s = ms / 1000;
        if (s < 60) return unit(s < 10 ? Math.round(s * 10) / 10 : Math.round(s), "second");
        const m = Math.floor(s / 60);
        if (m < 60) return `${unit(m, "minute")} ${unit(Math.round(s % 60), "second")}`;
        return `${unit(Math.floor(m / 60), "hour")} ${unit(m % 60, "minute")}`;
      },
    };
  }, [lang, setLang]);
}
