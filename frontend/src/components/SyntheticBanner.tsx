/**
 * The synthetic/demo label, shown in the UI itself.
 *
 * Not decoration and not removable. Every figure on this page comes from a
 * generator with planted effects, and a reader must never have to consult
 * documentation to discover that. The label text is the backend's own — it
 * arrives on the payload rather than being written here.
 */
export function SyntheticBanner({ label }: { label: string }) {
  return (
    <div
      role="note"
      className="flex items-center gap-2 rounded-md border border-synthetic/40 bg-synthetic/10 px-3 py-2"
    >
      <span aria-hidden className="size-1.5 rounded-full bg-synthetic" />
      <span className="text-xs font-semibold uppercase tracking-widest text-synthetic">
        {label}
      </span>
    </div>
  );
}
