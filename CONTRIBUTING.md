# Contributing

Thanks for your interest! This project controls real grid-tied hardware, so changes go through
review and automated tests before they reach the device.

## Workflow

1. **Fork** the repo (or branch, if you have write access) and make your change.
2. Keep the core dependency-light: `economics`, `prices`, `schedule`, and `p1` are **stdlib-only**.
   Add new third-party deps only behind an optional extra in `pyproject.toml`.
3. **Add/adjust tests.** The suite is offline (no network, no hardware) — mock or inject I/O.
4. **Open a pull request** against `main`.

## What happens to your PR

- **CI** runs the full test suite on every PR (`.github/workflows/ci.yml`).
- `main` is **protected**: no direct pushes; every change needs a PR with **green CI** and **one
  approving review** from the maintainer.
- When the maintainer merges, the change is **auto-deployed to the live Raspberry Pi** within a
  few minutes — but only if the test suite passes again on the device; otherwise it rolls back.
  So a merged PR really does go live: please be considerate with anything that writes to the
  inverter, and call out hardware-affecting changes in the PR description.

## Local development

```bash
pip install -e ".[dev]"   # core + pytest
pytest                     # offline suite
```

See [`README.md`](README.md) for the architecture and [`deploy/README.md`](deploy/README.md) for
the Raspberry Pi setup. Note the code is tailored to a specific setup (Belgium / BELPEX, a Frank
Energie tariff, a Huawei SUN2000-L1, a HomeWizard P1) — see *Scope & assumptions* in the README
before porting it elsewhere.
