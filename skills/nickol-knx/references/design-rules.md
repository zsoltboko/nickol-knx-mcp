# Design rules — pointer

**`CLAUDE.md` in the project root is authoritative for design rules.** This file is an index,
not a copy. Deliberately: the rules are maintained in one place, and a second full copy here
would drift out of sync and start contradicting it.

## Read the real thing

- **Claude Code** — `CLAUDE.md` loads automatically when the session starts in a directory that
  has one. If you are working in the `nickol-knx-mcp` repo or in a KNX project with `CLAUDE.md`
  dropped in, you already have it in context.
- **Claude Desktop** — `CLAUDE.md` is **not** loaded. If the file is reachable, read it before
  making design recommendations. If it is not, say so rather than inventing a house style, and
  ask the user for their conventions — or run `check_policy()`, which infers the taxonomy from
  the project itself.

## What `CLAUDE.md` covers

| Section | Answers |
|---|---|
| Hard safety rules | no live bus; report before import/deploy; commit every artifact |
| Two-layer architecture | KNX/ETS owns foundational logic and runs autonomously; HA is the upper layer and must read real state |
| Group address structure | 3-level Main/Middle/Sub, the main-group domain split (0 Central/Scenes … 7 Reserve), commands vs status in distinct middle groups, reserves in every range |
| Command/status pairs | every controllable actuator needs a status object; HA entities need `state_address` |
| DPT discipline | missing DPT is a hard error; same logical name → same DPT; the common-DPT table |
| Naming | zone + function; the status keyword that drives pairing |
| Category separation | lighting / dimming / shutters / HVAC / sensors / scenes / energy / diagnostics in their own ranges and HA platforms |
| KNX Secure | the ETS keyring (`.knxkeys`) is handled in ETS/HA, never by this server |
| Typical workflow | the end-to-end sequence, which this skill expands into the route table |

## The rules that change how you read tool output

These few are load-bearing for interpreting results, so they appear where they matter rather
than only here:

- **Status pairing is driven by name tokens** (`status` / `state` / `статус` / `Rückmeldung`).
  A status GA without a keyword will not pair and produces a false `check_missing_status`
  finding — see `repair.md`.
- **Missing DPT is a hard error**, not a warning: HA cannot decode the address — see `audit.md`.
- **The main-group taxonomy in `CLAUDE.md` is a *starting* profile to override**, not a
  standard. `check_policy` defaults to the project's own inferred taxonomy — see `audit.md`.
- **3-level addressing is assumed** by parts of the toolchain, including the log decoder. Check
  `ga_style` from `load_project` — see `log-forensics.md`.

## Project-specific conventions

A real project's conventions beat the defaults. Capture them in a Project Policy Profile
(`check_policy(write_example_to="policy.yaml")`, template at
`examples/policy-profile.example.yaml`) and validate against that from then on. A profile is the
right place for a house style — not an edit to this skill.
