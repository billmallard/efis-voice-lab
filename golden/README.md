# Golden set

`questions.yaml` holds 35 cases: 30 answerable, 2 that should get a clarifying question, and
4 out of scope. The fields are documented at the top of the file. `personas.yaml` (caller
personas for L3/L4) comes with M5.

**Status: draft v0, awaiting maintainer review.** Cases with a `review:` field carry an
open question.

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
| VFR-001 | pyEfis `README.rst`, *Virtual VFR* | Says to run `./MakeCIFPIndex.py`, which exists in neither pyEfis repo (it ships with pyavtools). It also refers to a `[Screen.PFD]` INI section, though the config is now YAML. |
| VER-002 | fork `docs/moving_map_spec.md` | Says "Development has not started", yet a `moving_map` instrument and screen config exist on master. The user wiki doesn't mention the map. |
| DATA-003 | fork `README.rst`, *Synthetic Vision* | Links `github.com/makerplane/makerplane-data`, which returns 404. The repo is `billmallard/makerplane-data`. |
| DATA-001 | `docs/wiki/Pilots-Guide.md` vs `Widgets-System.md` | The guide says the DATA flag appears when navdata is missing. The widget reference says it shows nothing when there's no updater status file. |
| HW-001, INST-001 | `INSTALLING.md` | Targets 64-bit Raspbian bullseye, while the README's reference platform is Debian 13 trixie. |
| HW-005 | `README.rst` vs makerplane-data `docs/terrain.md` | Terrain storage is "~90 GB" in one, and 91 GB native, about 120 GB with the pyramid, or about 60 GB compressed in the other. |
| VER-003 | upstream `README.rst` | "make init installs all dependencies" is true upstream but false on the fork, where the runtime deps are extras. Retrieving the upstream README produces a confident wrong answer. |

## Retrieval preview (not the L1 score)

With the M1 index and top-5 retrieval, 27 of the 30 answerable questions find a gold source.
The three misses are informative:

- **ARCH-002** (port 3490): the number only appears in an appliance guide, a plugin doc and a config file.
- **DEV-002** (running tests): the README's *Testing* section is a 73-character stub, and an
  internal performance doc outranks it.
- **VER-002** (moving map): the stale spec ranks first. The trap works as intended.

L1 (M3) turns this into scored, repeatable tests with Ragas context precision and recall.
