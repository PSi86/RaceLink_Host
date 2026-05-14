"""Regenerator for ``tests/fixtures/offset_formula_parity.json``.

The fixture is the single source of truth for offset-formula parity
between the Python evaluator (:mod:`racelink.domain.offset_formula`)
and the TypeScript port (``evaluateOffsetMs`` in
``frontend/src/stores/scenes.ts``). Both test suites read it; both must
produce byte-identical output for every case.

When the evaluator legitimately changes, regenerate and review the diff::

    python tests/gen_offset_parity_fixture.py

The fixture is committed. A non-trivial diff means evaluator behaviour
shifted — update the TS port (and the C++ counterpart in
``RaceLink_WLED/racelink_wled.cpp`` if applicable) in lockstep, then
regenerate again.

Tracker entry: ``frontend/POST_MIGRATION_CLEANUP.md`` §12.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from racelink.domain.offset_formula import evaluate_offset_ms

NUM_CASES = 1024
SEED = 20260504

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "offset_formula_parity.json"


def _gen_cases(rng: random.Random) -> list[dict]:
    # 1× none, 3× linear, 3× vshape, 3× modulo — ``none`` is trivial so
    # under-sampling it leaves more budget for the modes that actually
    # exercise clamp / mask / cycle edges.
    mode_pool = ["none"] + ["linear"] * 3 + ["vshape"] * 3 + ["modulo"] * 3

    cases: list[dict] = []
    for _ in range(NUM_CASES):
        mode = rng.choice(mode_pool)
        # gid intentionally exceeds 255 occasionally to exercise the
        # ``& 0xFF`` mask on both runtimes.
        gid = rng.randint(0, 300)

        if mode == "none":
            spec = {"mode": "none"}
        elif mode == "linear":
            spec = {
                "mode": "linear",
                "base_ms": rng.randint(-1000, 70000),
                "step_ms": rng.randint(-200, 200),
            }
        elif mode == "vshape":
            spec = {
                "mode": "vshape",
                "base_ms": rng.randint(-1000, 70000),
                "step_ms": rng.randint(-200, 200),
                "center": rng.randint(0, 32),
            }
        else:  # modulo
            spec = {
                "mode": "modulo",
                "base_ms": rng.randint(-1000, 70000),
                "step_ms": rng.randint(-200, 200),
                # cycle=0 is a documented edge (collapses to 1) — keep
                # in the sample.
                "cycle": rng.randint(0, 16),
            }

        cases.append(
            {
                "spec": spec,
                "group_id": gid,
                "expected": evaluate_offset_ms(spec, gid),
            }
        )

    return cases


def main() -> None:
    rng = random.Random(SEED)
    cases = _gen_cases(rng)

    payload = {
        "_note": (
            "DO NOT EDIT BY HAND. Regenerate via "
            "`python tests/gen_offset_parity_fixture.py` and commit the diff. "
            "See frontend/POST_MIGRATION_CLEANUP.md §12."
        ),
        "generator": "tests/gen_offset_parity_fixture.py",
        "seed": SEED,
        "num_cases": len(cases),
        "cases": cases,
    }

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(cases)} cases to {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
