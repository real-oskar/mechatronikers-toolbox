import type { Phase } from "../api/types";
import type { Key, Translate } from "../i18n";

// THE phase texts per run kind (#110): the run table, the hub overview, the title block and the
// circuit panels (#109) all use this module. Circuit runs have their own steps (analyse, ERC,
// gate 1, netlists, ngspice); where a phase means the same for both kinds (queued, provisioning,
// failed, archived) they share the text. Kind "all" (the overview's phase filter) is neutral.

const CIRCUIT: Partial<Record<Phase, Key>> = {
  analysing: "phaseCircuit_analysing",
  checking: "phaseCircuit_checking",
  waiting_gate_1: "phaseCircuit_waiting_gate_1",
  exporting: "phaseCircuit_exporting",
  simulating: "phaseCircuit_simulating",
  valid: "phaseCircuit_valid",
};

const NEUTRAL: Partial<Record<Phase, Key>> = {
  analysing: "phaseAny_analysing",
  waiting_gate_1: "phaseAny_waiting_gate_1",
  valid: "phaseAny_valid",
};

/** The phase in words for a run of `kind` ("part", "circuit", or "all" for a list over both);
 * `fixing` names the round (`n` of `max`) when given. */
export function phaseText(kind: string, phase: Phase, t: Translate, round?: { n: number; max: number | null }): string {
  if (kind === "all" && NEUTRAL[phase]) return t(NEUTRAL[phase]!);
  if ((kind === "circuit" || kind === "all") && CIRCUIT[phase]) return t(CIRCUIT[phase]!);
  if (phase === "fixing") return round ? t("phase_fixing", { n: round.n, max: round.max ?? "?" }) : t("phaseReworking");
  return t(`phase_${phase}` as Key);
}

/** How the phase cell is marked: released, needs a look, or not OK. */
export function phaseClass(phase: Phase): string | undefined {
  if (phase === "valid" || phase === "released") return "status-ok";
  if (phase === "gave_up" || phase === "aborted" || phase === "failed") return "status-nok";
  if (phase === "waiting_gate_1") return "status-check";
  return undefined;
}

/** "released after inspection · 4 open points": a release leaves unresolved points open (D70),
 * so the final phase says how many (#152): a released part, a circuit's "netlists ready". */
export function withOpenPoints(text: string, phase: Phase, open: number, t: Translate, kind = "part"): string {
  const final = phase === "released" || (phase === "valid" && kind === "circuit");
  if (!final || open <= 0) return text;
  return `${text} · ${open === 1 ? t("openPointOne") : t("openPoints", { n: open })}`;
}
