---
name: nickol-knx
description: >-
  Design-time KNX/ETS workflow for the nickol-knx MCP server (37 tools). Use EVERY time the
  task involves: a `.knxproj` or `.knxprod` file, ETS5/ETS6, KNX group addresses or a 3-level
  GA structure, DPT/datapoint types, command/status pairing, generating a Home Assistant KNX
  package or YAML from a project, an ETS group-address import/export (XML/CSV), a commissioning
  handover pack or acceptance test protocol, KNX Data Secure and keyring handover, comparing two
  project versions, composing a new project from room templates — and for analysing an ETS
  **bus monitor / group monitor recording** (CommunicationLog XML) against the project. Also
  trigger on: KNX audit, address schema, actuator status feedback, ghost sender, "what does this
  system actually do", telegram log, dimmer/blind/thermostat group addresses.
  The server CANNOT touch a bus; it reads .knxproj and writes into a confined workspace.
---

# nickol-knx — the design-time KNX workflow

The 37 MCP tools are the capability. This skill is the **order of operations** and the
**rules for reading their output**. Design rules for what a *good* KNX project looks like
(GA taxonomy, DPT table, naming) live in the project's `CLAUDE.md` — see
`references/design-rules.md`.

## IRON RULES

1. **Never propose writing to a live bus.** Not a rule this server relies on you to keep — it
   is structural: the package has no network or bus dependency at all. It reads `.knxproj` and
   writes files. Any real interaction with the house goes through Home Assistant, and only when
   the user explicitly asks.
2. **All writes land in one confined directory.** `_safe_write()` refuses any path outside
   `NICKOL_KNX_WORKSPACE` (default `./knx-workspace`). If a write is refused, that is the
   guardrail working — do not route around it. `workspace_info()` shows the current path.
3. **`project_report()` and human review come BEFORE any ETS import or HA deploy.** Never hand
   the user a generated file with "you can import this now" as the first thing they read.
4. **Nothing here changes ETS.** `suggest_repairs`, `suggest_names`, `decompose_device` produce
   *proposals*. A human accepts them; accepted new GAs then flow through
   `generate_ets_group_addresses` into ETS. Say "proposed", never "fixed".
5. **Commit before and after.** Generated artifacts belong in the git-tracked workspace so every
   step is reversible.

## State model — the sequence is not optional

`load_project(path, password?, language?)` **first, always.** Every analysis, generation and
log tool reads a module-global cached project and raises without it.

`load_telegram_log(path, ...)` additionally **requires a loaded project**. Raw frames carry
addresses and bytes; only the `.knxproj` turns those into names, datapoint types and *expected*
senders. A recording analysed without a project is just hex.

`load_project` returns `ga_style` — check it. Several tools assume 3-level addressing.

Password-protected project: `load_project(path=..., password="...")`. Note that
`check_device_parameters` and `parse_devices_from_project` take their own `path` argument and
read the file directly, separate from the cached project.

## Route table

Pick by what the user actually brought:

| They have | Start here | Reference |
|---|---|---|
| An existing `.knxproj`, want to know if it's any good | `load_project` → `grade_completeness` → `analyze_all` | `references/audit.md` |
| A project with known problems, want them fixed | audit first, then `suggest_repairs` | `references/repair.md` |
| A finished project to hand over / deploy | `project_report` → `generate_ha_package` / `generate_handover_pack` | `references/generate.md` |
| An ETS bus-monitor recording | `load_project` → `load_telegram_log` | `references/log-forensics.md` |
| A spec or a blank sheet | `validate_room_template` → `compose_rooms` | `references/compose.md` |
| Two project versions | `diff_projects(path_a, path_b)` — no `load_project` needed | `references/audit.md` |

When in doubt, audit first. Almost every other path is better decided once
`grade_completeness` has said whether this is a bare skeleton or an as-built project.

## How to read a confidence claim

This is the cross-cutting rule, and the most common way to give a user a confidently wrong
answer. Two tools grade their own evidence, and **you must carry that grading through into
what you tell the user.**

**`explain_ga(address)`** replays a classification and tags each signal with a tier:

| Tier | Source | Weight |
|---|---|---|
| `authoritative` | an ETS **Function** role — the integrator declared it | trust it |
| `structural` | the KNX **DPT** | strong |
| `heuristic` | a **name** keyword | weak — a guess from text |

It also returns a `conflicts` field — e.g. a GA whose DPT says lighting while its name says
"AC" — and a `confidence` field whose value goes **`contested`** when name and DPT disagree.
`confidence: "contested"` is the single highest-value signal in the whole toolchain: it marks
where silent misclassification is happening. Never generate an HA entity from a contested GA
without surfacing the conflict first.

`ets_functions` tells you which tier was even available. A project with no ETS Function tags
can never reach `authoritative` — everything is structural or heuristic, and `confidence` will
be correspondingly low. Say that rather than presenting a heuristic result as settled.

**`log_ga_activity`** tags every decoded value with `dpt_source`:

`project` / `object` (declared somewhere) > `override` > `inferred` > `unknown`

**`inferred` means the type was deduced from payload width and the communication object's
function text. It is a deduction, not a fact.** Report it as one: "9.001 (inferred — the
project declares no DPT here)". A payload that does not fit its type falls back to raw hex
rather than producing a plausible wrong number; if you see hex, say the decode failed, do not
invent a reading.

Applies equally to `decompose_device` and `parse_devices_from_project`: a DPT marked
`unverified` means the vendor app-program declares none. It was never guessed. Do not upgrade
it to a fact on the user's behalf.

## Reporting

Findings carry severities. Fix 🔴 errors in ETS before generating anything downstream —
a missing DPT is a hard error because Home Assistant cannot decode the address at all.

Keep the two layers distinct when you explain results: **KNX/ETS owns the foundational logic
and runs autonomously; Home Assistant is the upper layer** and must read real device state,
never assume it. That is why a missing status GA is a real defect and not a cosmetic one.
