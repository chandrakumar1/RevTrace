"""Command-line interface.

    python -m simulator list
    python -m simulator generate S04 --seed 42
    python -m simulator generate S04 --seed 42 --dry-run --checksum-only
    python -m simulator generate-set baseline+leak --seed 7 --out demo_set
    python -m simulator inspect output/S04_seed42 --show-timeline
    python -m simulator verify output/S04_seed42

Standard library only. No third-party CLI framework.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from simulator import simulate
from simulator.config import ScenarioCategory, ScenarioParams
from simulator.scenarios import all_scenarios, scenarios_by_category
from simulator.serialization import events_jsonl, fixture_to_dict, frontend_view
from simulator.validation import SimulationError

DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent / "output"


def _run_dir(root: Path, scenario_id: str, seed: int) -> Path:
    return root / f"{scenario_id}_seed{seed}"


def _write_run(result: object, directory: Path) -> None:
    from simulator.models import SimulationResult

    assert isinstance(result, SimulationResult)
    directory.mkdir(parents=True, exist_ok=True)

    (directory / "fixture.json").write_text(
        json.dumps(fixture_to_dict(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (directory / "events.jsonl").write_text(events_jsonl(result), encoding="utf-8")
    (directory / "frontend.json").write_text(
        json.dumps(frontend_view(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def cmd_list(args: argparse.Namespace) -> int:
    for category in ScenarioCategory:
        specs = scenarios_by_category(category)
        if not specs:
            continue
        print(f"\n{category.value.upper().replace('_', ' ')}")
        for spec in specs:
            print(f"  {spec.id:<6} {spec.name:<40} {spec.purpose}")
    print()
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    params = ScenarioParams(
        currency=args.currency,
        order_count=args.order_count,
        attempt_count=args.attempt_count,
        delay_seconds=args.delay_seconds,
        duplicate_count=args.duplicate_count,
    )
    result = simulate(args.scenario, seed=args.seed, params=params)

    if args.checksum_only:
        print(result.manifest.checksum)
        return 0

    manifest = result.manifest
    print(f"Scenario:  {manifest.scenario_name} ({manifest.scenario_id})")
    print(f"Seed:      {manifest.seed}        Generator: {manifest.generator_version}")
    counts = manifest.counts
    print(
        f"Entities:  {counts['merchants']} merchant, {counts['customers']} customers, "
        f"{counts['orders']} orders, {counts['payment_attempts']} attempts"
    )
    print(f"Events:    {counts['events_emitted']} emitted, {counts['events_unique']} unique")
    if manifest.window_start and manifest.window_end:
        print(
            f"Window:    {manifest.window_start.isoformat()} .. {manifest.window_end.isoformat()}"
        )
    print(f"Checksum:  {manifest.checksum}")

    if args.dry_run:
        print("Output:    (dry run — nothing written)")
        return 0

    directory = _run_dir(
        Path(args.out) if args.out else DEFAULT_OUTPUT_ROOT, manifest.scenario_id, manifest.seed
    )
    _write_run(result, directory)
    print(f"Output:    {directory}")
    return 0


def cmd_generate_set(args: argparse.Namespace) -> int:
    wanted = {name.strip() for name in args.categories.split("+") if name.strip()}
    valid = {c.value for c in ScenarioCategory}
    unknown = wanted - valid
    if unknown:
        print(f"unknown categories: {sorted(unknown)}; known: {sorted(valid)}", file=sys.stderr)
        return 2

    root = Path(args.out) if args.out else DEFAULT_OUTPUT_ROOT
    written = 0
    for spec in all_scenarios():
        if spec.category.value not in wanted:
            continue
        result = simulate(spec.id, seed=args.seed)
        if not args.dry_run:
            _write_run(result, _run_dir(root, spec.id, args.seed))
        print(f"{spec.id:<6} {spec.name:<40} {result.manifest.checksum}")
        written += 1

    print(f"\n{written} scenario(s){' (dry run)' if args.dry_run else f' written to {root}'}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    fixture_path = directory / "fixture.json"
    if not fixture_path.exists():
        print(f"no fixture.json in {directory}", file=sys.stderr)
        return 2

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    manifest = fixture["manifest"]
    print(f"Scenario:  {manifest['scenario_name']} ({manifest['scenario_id']})")
    print(f"Seed:      {manifest['seed']}")
    print(f"Checksum:  {manifest['checksum']}")

    if args.show_timeline:
        print()
        print(f"{'seq':>4}  {'delivery_at':<22} {'occurred_at':<22} {'type':<28} dup")
        for delivery in fixture["deliveries"]:
            envelope, event = delivery["envelope"], delivery["event"]
            marker = "DUP" if envelope["is_duplicate"] else "-"
            print(
                f"{envelope['sequence']:>4}  {event['received_at']:<22} "
                f"{event['occurred_at']:<22} {event['event_type']:<28} {marker}"
            )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    fixture_path = directory / "fixture.json"
    if not fixture_path.exists():
        print(f"no fixture.json in {directory}", file=sys.stderr)
        return 2

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    manifest = fixture["manifest"]
    regenerated = simulate(manifest["scenario_id"], seed=manifest["seed"])

    if regenerated.manifest.checksum == manifest["checksum"]:
        print("OK — regenerated checksum matches manifest")
        return 0

    print("MISMATCH", file=sys.stderr)
    print(f"  recorded:    {manifest['checksum']}", file=sys.stderr)
    print(f"  regenerated: {regenerated.manifest.checksum}", file=sys.stderr)
    print(
        f"  generator: recorded={manifest['generator_version']} "
        f"current={regenerated.manifest.generator_version}",
        file=sys.stderr,
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m simulator",
        description="RevTrace deterministic synthetic event simulator.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list the scenario catalog").set_defaults(func=cmd_list)

    gen = sub.add_parser("generate", help="generate one scenario")
    gen.add_argument("scenario", help="scenario id, e.g. S04")
    gen.add_argument("--seed", type=int, required=True)
    gen.add_argument("--out", default=None, help="output root directory")
    gen.add_argument("--dry-run", action="store_true", help="generate without writing")
    gen.add_argument("--checksum-only", action="store_true", help="print only the checksum")
    gen.add_argument("--currency", default="INR")
    gen.add_argument("--order-count", type=int, default=None)
    gen.add_argument("--attempt-count", type=int, default=None)
    gen.add_argument("--delay-seconds", type=int, default=None)
    gen.add_argument("--duplicate-count", type=int, default=None)
    gen.set_defaults(func=cmd_generate)

    gen_set = sub.add_parser("generate-set", help="generate every scenario in categories")
    gen_set.add_argument("categories", help="e.g. baseline+leak")
    gen_set.add_argument("--seed", type=int, required=True)
    gen_set.add_argument("--out", default=None)
    gen_set.add_argument("--dry-run", action="store_true")
    gen_set.set_defaults(func=cmd_generate_set)

    inspect = sub.add_parser("inspect", help="inspect a generated run")
    inspect.add_argument("directory")
    inspect.add_argument("--show-timeline", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    verify = sub.add_parser("verify", help="regenerate and compare checksums")
    verify.add_argument("directory")
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SimulationError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
