import { FIXTURES } from "@/fixtures";

/**
 * Selects which committed fixture the page renders.
 *
 * A development affordance, not a product surface — there is no API yet, and
 * this is how the edge cases (underpowered, undefined Qini, refused analysis)
 * get exercised in the browser.
 */
export function FixturePicker({
  value,
  onChange,
  description,
}: {
  value: string;
  onChange: (id: string) => void;
  description: string;
}) {
  return (
    <div className="w-full sm:max-w-md">
      <label
        htmlFor="fixture"
        className="mb-1.5 block text-[0.7rem] font-semibold uppercase tracking-widest text-muted"
      >
        Fixture
      </label>
      <select
        id="fixture"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-md border border-line bg-card px-3 py-2 text-sm shadow-xs outline-none focus-visible:ring-2 focus-visible:ring-ink/20"
      >
        {FIXTURES.map((entry) => (
          <option key={entry.id} value={entry.id}>
            {entry.label}
          </option>
        ))}
      </select>
      <p className="mt-2 text-xs leading-relaxed text-faint">{description}</p>
    </div>
  );
}
