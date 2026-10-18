import type { InspectionStatus } from "../../lib/inspection";

/** The numbered balloon of a ballooned drawing: the one bold element of the interface. */
export function Balloon({
  n,
  status = "ok",
  reviewer = false,
  small = false,
  pressed,
  onClick,
  label,
}: {
  n: number;
  status?: InspectionStatus;
  reviewer?: boolean;
  small?: boolean;
  pressed?: boolean;
  onClick?: () => void;
  label?: string;
}) {
  const className = [
    "balloon",
    small ? "balloon-small" : "",
    status !== "ok" ? `balloon-${status}` : "",
    reviewer ? "balloon-reviewer" : "",
  ]
    .filter(Boolean)
    .join(" ");
  if (onClick) {
    return (
      <button type="button" className={className} aria-pressed={pressed} onClick={onClick} aria-label={label}>
        {n}
      </button>
    );
  }
  return (
    <span className={className} aria-label={label}>
      {n}
    </span>
  );
}
