import { useI18n } from "../../i18n";

export function LangSwitch() {
  const { lang, setLang, t } = useI18n();
  return (
    <div className="segmented" role="group" aria-label={t("language")}>
      {(["de", "en"] as const).map((l) => (
        <button key={l} type="button" aria-pressed={lang === l} onClick={() => setLang(l)}>
          {l.toUpperCase()}
        </button>
      ))}
    </div>
  );
}
