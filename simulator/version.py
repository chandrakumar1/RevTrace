"""Generator version.

Stamped into every manifest so that an *intentional* change to generation logic
is distinguishable from a regression. If a recorded checksum stops matching and
this version is unchanged, something broke. If this version changed, the
checksum was expected to move.

Bump on any change that alters generated output.
"""

from __future__ import annotations

GENERATOR_VERSION = "1.0.0"
