/** A rubber stamp for decisions: who released or rejected what, and when. */
export function Stamp({
  tone,
  title,
  by,
  press = false,
}: {
  tone: "release" | "reject" | "pending";
  title: string;
  by?: string;
  press?: boolean;
}) {
  return (
    <span className={`stamp stamp-${tone}${press ? " stamp-press" : ""}`} role="status">
      <span className="stamp-title">{title}</span>
      {by && <span className="stamp-by">{by}</span>}
    </span>
  );
}
