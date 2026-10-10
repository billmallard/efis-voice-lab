# Golden set

`questions.yaml` holds 35 cases: 30 answerable, 2 that should get a clarifying question, and
4 out of scope. The fields are documented at the top of the file. `personas.yaml` (caller
personas for L3/L4) comes with M5.

**Status: v1, reviewed (2026-10-09).** The maintainer ruled on HW-005, INST-001 and
VER-002; VFR-001 was settled by reading the code. `trap_sources` names the stale documents
that produce a confident wrong answer when retrieved.

## How the expected answers were written

Every expected answer was checked against the indexed documents, and where the docs and the
shipped config or code disagree, against the config or code. When a document is wrong, the
case says so in `notes:`. The expected answer follows the evidence, not the most prominent
document.

This is deliberate. Real documentation is imperfect. A harness that only works on clean docs
tells you little, and a failure caused by a bad source document is a finding about the corpus,
not about the agent.

## Documentation defects found while building the set

| Case | Document | Defect |
|---|---|---|
| CTRL-001 | pyEfis `README.rst` (fork and upstream), *Controls* | Says the `[` and `]` keys change the altimeter setting. The shipped keybindings (`config/keybindings/default.yaml`) have no such binding. The control is the on-screen ± baro buttons. |
| FGW-003 | FIX-Gateway `doc/getting_started.rst` | Says `fixgw.py` / `fixgwc.py` run "the client and the server respectively", which is backwards. The same page later states it correctly. |
| VFR-001 | pyEfis `README.rst`, *Virtual VFR* | Says to run `./MakeCIFPIndex.py`, which exists in neither pyEfis repo (it ships with pyavtools). It also refers to a `[Screen.PFD]` INI section; the setting is now `screens/virtualvfr_db.yaml`. CIFP is still supported, as Virtual VFR's source and an SVS fallback. |
| VER-002 | fork `docs/moving_map_spec.md` | Says "Development has not started", yet a `moving_map` instrument and screen config exist on master. The user wiki doesn't mention the map. |
| DATA-003 | fork `README.rst`, *Synthetic Vision* | Links `github.com/makerplane/makerplane-data`, which returns 404. The repo is `billmallard/makerplane-data`. |
| DATA-001 | `docs/wiki/Pilots-Guide.md` vs `Widgets-System.md` | The guide says the DATA flag appears when navdata is missing. The widget reference says it shows nothing when there's no updater status file. |
| INST-001, HW-001 | `INSTALLING.md` (fork and upstream) | Prescribes snaps, which aren't current with the fork and won't be for some time, so running from source is the safe answer. It also targets 64-bit Raspbian bullseye, while the reference platform is Debian 13 trixie. |
| HW-005 | pyEfis `README.rst`, *Suggested* | "A full North-America terrain set is ~90 GB" is stale: it predates the mip pyramid. About 120 GB on disk is current (makerplane-data `docs/terrain.md`). |
| VER-003 | upstream `README.rst` | "make init installs all dependencies" is true upstream but false on the fork, where the runtime deps are extras. Retrieving the upstream README produces a confident wrong answer. |

## L1 baseline

The full results are in [docs/notes/M3.md](../docs/notes/M3.md) and
[baselines/L1/2026-10-09.json](../baselines/L1/2026-10-09.json). With top-5 retrieval, 26 of
30 answerable questions find a gold source (hit rate 0.867, MRR 0.75). In three cases, a
trap document outranks every gold source:

- **HW-003:** the internal perf-branch doc outranks the README.
- **INST-001:** the stale snap guide, with no gold source in the top 5.
- **VER-002:** the stale "not started" moving-map spec, with no gold source in the top 5.

These are corpus problems, which fixing the docs solves and tuning the agent can't.
