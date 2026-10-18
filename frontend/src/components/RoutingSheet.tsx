import type { GateState } from "../api/types";
import { useI18n } from "../i18n";
import type { Key } from "../i18n";
import type { Step } from "../lib/timeline";
import { Balloon } from "./common/Balloon";
import { Stamp } from "./common/Stamp";

const OP_KEYS = new Set([
  "provisioning", "analyse", "read", "check", "plan", "replan", "review_gate_1",
  "build", "fix", "export", "revise", "review_gate_2", "give_up",
]); // names the reviewer understands

/** An operation's name for the reviewer; `names` are a run kind's own (e.g. circuits). */
export function opName(
  step: Step,
  t: (key: Key, vars?: Record<string, string | number>) => string,
  names?: Partial<Record<string, string>>,
): string {
  const base = names?.[step.name] ?? (OP_KEYS.has(step.name) ? t(`op_${step.name}` as Key) : step.name);
  return step.name === "fix" || (step.label.includes("#") && step.name !== "provisioning")
    ? `${base} ${step.label.match(/#\d+/)?.[0] ?? ""}`.trim()
    : base;
}

function gateStamp(step: Step, gates: GateState[], t: ReturnType<typeof useI18n>["t"], time: ReturnType<typeof useI18n>["time"]) {
  const decisions: Record<string, ["release" | "reject" | "pending", Key]> = {
    "review.gate_passed": ["release", "stampReleased"],
    "review.rejected": ["reject", "stampRejected"],
    "review.update_requested": ["pending", "stampUpdate"],
    "review.retry_requested": ["pending", "stampRetry"],
    "review.aborted": ["reject", "stampAborted"],
  };
  // `automatic` from the part agent (test bench), `auto` from the circuit run (`--auto-approve`)
  const passed = step.events.find((e) => e.event.type in decisions && e.event.payload.item_id === undefined);
  if (passed) {
    const p = passed.event.payload;
    const [tone, title] = decisions[passed.event.type]!;
    return (
      <Stamp
        tone={tone}
        title={t(title)}
        by={p.automatic ? t("automatic") : p.auto ? t("automaticRun") : t("stampBy", { name: String(p.reviewer ?? "?"), time: time(passed.event.ts) })}
      />
    );
  }
  const open = gates.find((g) => g.open);
  return open && step.status === "waiting" ? <Stamp tone="pending" title={t("opWaiting")} /> : null;
}

/** The agent's work as a routing sheet: one numbered operation per step, gates as sign-offs. */
export function RoutingSheet({
  steps,
  selected,
  onSelect,
  gates,
  names,
}: {
  steps: Step[];
  selected: string | undefined;
  onSelect: (key: string) => void;
  gates: GateState[];
  names?: Partial<Record<string, string>>;
}) {
  const { t, duration, time } = useI18n();
  return (
    <nav className="routing" aria-label={t("tabRouting")}>
      <div className="routing-head">
        <h2>{t("tabRouting")}</h2>
        <span className="fine muted">{steps.length > 0 && time(steps[0]!.start)}</span>
      </div>
      {steps.length === 0 && <p className="viewport-empty">{t("waitingFirst")}</p>}
      <ol className="op-list">
        {steps.map((step, i) => {
          const isGate = step.name.startsWith("review_gate") || step.name === "give_up";
          const band = i > 0 && step.revision !== steps[i - 1]!.revision;
          const state =
            step.status === "waiting" ? t("opWaiting") : step.status === "running" ? t("opRunning")
              : step.status === "failed" ? t("opFailed") : duration(step.durationMs);
          return (
            <li key={step.key}>
              {band && <div className="op-band">{t("revisionBand", { n: step.revision })}</div>}
              <button
                type="button"
                className={`op op-${step.status}${step.warned ? " op-warned" : ""}`}
                aria-current={selected === step.key}
                onClick={() => onSelect(step.key)}
              >
                <span className="op-no">{(i + 1) * 10}</span>
                <span className="op-name">{opName(step, t, names)}</span>
                <span className="op-state">{step.status === "done" ? `${step.warned ? "⚠" : "✓"} ${state}` : state}</span>
                {(step.pois.length > 0 || step.cost > 0) && (
                  <span className="op-meta">
                    {step.pois.length > 0 && (
                      <>
                        <Balloon n={step.pois.length} small status={step.pois.some((p) => p.item.severity === "blocking") ? "nok" : "check"} />
                        {step.pois.length === 1 ? t("findingsOne") : t("findings", { n: step.pois.length })}
                      </>
                    )}
                    {step.cost > 0 && <span>{t("credits", { n: step.cost.toFixed(3) })}</span>}
                  </span>
                )}
              </button>
              {isGate && <div className="op-gate">{gateStamp(step, gates, t, time)}</div>}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
