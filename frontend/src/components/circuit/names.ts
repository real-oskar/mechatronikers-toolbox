import type { Key, Translate } from "../../i18n";
import { COMPONENT_KINDS } from "../../lib/circuit";

/** A component kind in the reviewer's language (unknown kinds as they are). */
export function kindName(kind: string, t: Translate): string {
  return (COMPONENT_KINDS as readonly string[]).includes(kind) ? t(`k_${kind}` as Key) : kind;
}

const FIELDS = new Set(["value", "kind", "params", "polarity", "footprint", "role", "pin", "name"]);

/** A corrected field in the reviewer's language. */
export function fieldName(field: string, t: Translate): string {
  return FIELDS.has(field) ? t(`field_${field}` as Key) : field;
}
