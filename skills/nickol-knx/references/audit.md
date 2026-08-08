# Audit — read an existing `.knxproj` and judge it

## Sequence

```
load_project(path, password?, language?)
  → grade_completeness()        set expectations before judging anything
  → analyze_all(name_regex?)    every check at once
  → explain_ga(address)         on anything contested or surprising
  → check_policy(profile_path?) does it match THIS project's own convention?
```

Then the domain checks as the project warrants: `check_secure()`, `check_energy()`,
`check_matter()`, `check_device_parameters(path)`.

## Why `grade_completeness` goes first

It grades a project as a **bare functional skeleton vs. as-built**, by looking for the patterns
a professional actually leaves behind: central macros, device tuning, astro/meteo inputs,
monitoring, deep metering, scenes, address reserves, a debug main group.

This changes how you read everything after it. "No scenes, no central group" is a finding worth
raising on an as-built project and pure noise on a skeleton that was never meant to have them.
Without this step you will report a design stage as a defect list.

## `analyze_all` — what is inside it

Runs and merges: `check_naming`, `check_missing_status`, `check_dpt`, `check_topology`, and
returns them under one `summary` whose `errors`/`warnings`/`info` totals **include topology**
(the report builder alone counts only naming + status + DPT; the tool folds topology in).

Priorities:

- **Missing DPT is a hard error.** HA cannot decode the address. Nothing downstream is
  trustworthy until it is set.
- **Missing status GA is a real defect, not cosmetic.** Every controllable actuator must be able
  to report back — a switch needs a state GA, a dimmer a brightness-state GA, a blind position
  plus position state. Without it, HA displays a guess.
- **Inconsistent DPTs across same-named GAs** is a bug, not a style issue.

`name_regex` scopes naming validation when a project deliberately uses a non-default pattern.

## `check_topology` — KNX canon, not opinion

Enforces the KNX Handbook limits: TP1 **64 devices per segment**, **256 per line**, individual
addresses that are valid and unique in `A.L.D` form, and a coupler present in any multi-line
project. These are physical constraints. A violation is not a style preference, and the user
cannot decide to ignore it.

## `check_policy` — *their* convention, not a universal standard

The important and easily-misreported part: called with no `profile_path`, it validates against
the taxonomy **inferred from the project itself** (falling back to the CLAUDE.md default as a
starting profile). It therefore flags **deviation from the user's own convention** — an
outlier, an address that broke the pattern the rest of the project follows.

Never present its output as "violates the KNX standard". Present it as "inconsistent with how
the rest of this project is organised". Those are very different claims to an integrator.

`check_policy(write_example_to="policy.yaml")` drops a commented example profile into the
workspace for the user to edit and adopt; see `examples/policy-profile.example.yaml`.

## `explain_ga` — use it before trusting anything

Read `SKILL.md` § "How to read a confidence claim" for the tier semantics. Practical triggers
for calling it:

- any GA you are about to turn into an HA entity whose category looks odd
- any GA whose `confidence` comes back `contested`
- the user asks "why did you decide this is a light?"

It replays the classification per decision (`category` / `kind` / `intent` / `status_pairing`),
shows which signals fired with their tier, and returns `conflicts`, `confidence` and
`ets_functions`. Read-only and cheap — reach for it rather than speculating about why the
classifier landed where it did.

**Check `ets_functions` before trusting a category.** Many real projects tag no ETS Functions at
all, in which case it returns `"none — no ETS Function tags this GA (heuristics only)"`. The
`authoritative` tier is then simply unavailable and every classification rests on DPT and name.
That is a fact about the project, worth telling the user: adding ETS Functions is the single
change that most improves classification quality.

## `check_device_parameters` — the odd device out

Different from every other tool here: it takes its **own `path`** (and `password`) and reads
per-device ETS parameter values straight from the project part, data `xknxproject` does not
expose. A password-protected project cannot be read this way.

It groups identical devices by application program and returns two buckets that mean different
things:

- **`clear_outliers`** — a strong majority plus a small minority. Likely a commissioning
  mistake: the one thermostat with a different hysteresis, the one presence detector with a
  different detection time. Worth raising directly.
- **`split_configs`** — balanced 2+ variants. Usually **two zones, deliberately configured
  differently**. Flag for review; never present as an error and never propose "fixing" it.

`min_group` (default 3) sets how many identical siblings are needed before comparison is
meaningful.

## Other domain checks

- **`check_secure()`** — Data Secure posture: secured vs plaintext GA counts, and middle groups
  that **mix** secure and plaintext addresses. That mix is the finding that matters: a function
  is only as secure as its weakest GA. Also emits the keyring handover checklist. Report-only;
  this server never touches key material.
- **`check_energy()`** — metering DPTs (13.x energy, 14.056 power) plus a suggested
  per-circuit / PV / battery / EVSE structure for the HA energy dashboard.
- **`check_matter()`** — which controllable functions round-trip to a Matter cluster, i.e. have
  command **and** status **and** a decodable DPT. In practice it re-surfaces missing-status
  findings with a different consequence attached.

## Comparing two versions

`diff_projects(path_a, path_b, password_a?, password_b?)` — `path_a` is the base/old. Returns
added/removed GAs, DPT changes, renames and security-flag changes. It reads both files
directly, so **no `load_project` first**. Note that it also clobbers nothing: read-only.

Use it to answer "what did the integrator change since last week", and to verify that an ETS
import did what was intended.

## Reading tools

`list_group_addresses(category?, kind?, missing_dpt_only?, limit=500)` — categories are
`lighting / shutter / hvac / sensor / scene / energy / diagnostics`; kinds are
`command / status / sensor`. `get_devices()` and `get_topology()` for the inventory and the
area/line tree.
