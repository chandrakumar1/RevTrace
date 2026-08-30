/**
 * Limitations, as the backend wrote them.
 *
 * The strings are rendered verbatim rather than summarised. They are the part
 * of the report that stops the rest being over-read — the wording on interval
 * calibration in particular is careful about the difference between "no
 * evidence of under-coverage" and "calibrated", and paraphrasing would lose
 * exactly the distinction it exists to make.
 */
export function LimitationsList({
  heading,
  items,
}: {
  heading: string;
  items: string[];
}) {
  if (items.length === 0) {
    return null;
  }
  return (
    <div>
      <h3 className="mb-2 text-[0.7rem] font-semibold uppercase tracking-widest text-muted">
        {heading}
      </h3>
      <ul className="space-y-2">
        {items.map((item) => (
          <li
            key={item}
            className="flex gap-2.5 text-xs leading-relaxed break-words text-ink"
          >
            <span aria-hidden className="mt-1.5 size-1 shrink-0 rounded-full bg-faint" />
            <span className="min-w-0">{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
