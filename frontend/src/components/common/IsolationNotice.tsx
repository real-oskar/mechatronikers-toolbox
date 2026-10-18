import { useI18n } from "../../i18n";

/** D95: a build of this run ran without sandbox isolation, so it could have written the review
 * state. Shown on the run view and on every gate card. */
export function IsolationNotice() {
  const { t } = useI18n();
  return (
    <p className="notice notice-deviation" role="alert">
      {t("unisolated")}
    </p>
  );
}
