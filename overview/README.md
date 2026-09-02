# RevTrace Overview

A public showcase site for **RevTrace** — a five-section page that explains the
idea in about ninety seconds and then gets out of the way.

**This is not the RevTrace application.** It is a separate, self-contained
project with its own design system and no dependency on the application: no API
calls, no backend, no shared components, no build-time link between the two
repositories. It renders identically whether or not RevTrace is running.

## The idea

When a payment fails and you intervene, and the customer then pays, you have not
learned that you caused the payment. You have learned that both things happened.
Many of those customers would have paid anyway.

RevTrace measures the difference. Every case is randomly assigned to treatment or
a **holdout** that is never contacted, so the gap between the two arms is the
part the intervention actually caused. That splits recovered revenue into two
very different quantities:

- **incremental** — caused by the intervention, and defensible
- **credited, not earned** — arrived anyway, and claimed by a conventional
  recovery dashboard

It also declines to act when the evidence does not support it, keeps every
numerical decision in deterministic code rather than in a language model, and
refuses payment events that fail verification.

## See the real project

- **Application** — <https://revtrace-frontend.onrender.com>
- **Source** — <https://github.com/chandrakumar1/RevTrace>

## Data integrity

Every figure on this site lives in [`src/data/evidence.ts`](src/data/evidence.ts)
and is transcribed from RevTrace's own generated artifacts, each carrying the
artifact path it came from. Three rules hold:

- Nothing is invented — no figure appears that is not in an artifact, and there
  are no savings estimates, security scores, adoption metrics or testimonials.
- An absent value stays absent — a quantity that was never recorded renders as a
  sentence explaining its absence, never as `0` or `undefined`.
- Money is carried as integer minor units and rates as integer basis points;
  they are formatted by slicing decimal strings, so no float touches a money path.

**All figures are synthetic.** The population is generated with planted effects,
so recovering one validates the estimator rather than the world. Nothing here
describes a real customer, a real payment, or real money, and no real Razorpay
transaction has ever been processed.

## Running it

```sh
npm install
npm run dev        # http://localhost:5173
npm run typecheck
npm run build
```

Requires Node 20.19+.

## Layout

```
src/
  data/evidence.ts        every figure, with its source artifact
  lib/                    integer formatting · reveal + scroll motion
  styles/                 design tokens, global styles
  components/scene/       canvas scenes (event field, treatment/holdout split)
  components/sections/    Hook · Problem · Experiment · Different · See it
  components/ui/          shared primitives
design/og.html            source for public/og.png, so it can be regenerated
```

The scenes are 2D canvas rather than WebGL: they are particle fields, not
geometry, and a canvas costs a fraction of a 3D runtime while running the same
on a low-end phone. Both stop when scrolled out of view or when the tab is
hidden, scale their particle count to the viewport, and render a single settled
frame under `prefers-reduced-motion`.
