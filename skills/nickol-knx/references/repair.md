# Repair — propose fixes, route them through ETS

## The framing that must not slip

This server **cannot modify a `.knxproj`**. Every tool here emits a *proposal*. The workflow is:

```
suggest_repairs()  →  human reviews  →  accepted GAs  →  generate_ets_group_addresses()  →  import in ETS
```

Use "proposed", "suggested", "candidate". Never "fixed", "added", "corrected" — the user will
believe their project changed, discover in ETS that it did not, and stop trusting the report.

## `suggest_repairs()`

Turns findings into reviewable fixes rather than a defect list:

- **infer a DPT** for a GA that has none
- **correct a suspect sub-DPT** (temperature that is not 9.001, power that is not 14.056)
- **synthesise a status/feedback GA in a free address slot** — the highest-value repair, and the
  one that needs the most review, because it invents an address
- **add an absolute-brightness GA for a relative-only dimmer** (a dimmer with 3.007 but no
  5.001 cannot be set to a value from HA, only nudged)

Present the synthesised addresses explicitly, with the slot chosen, so the user can veto the
placement. Reserve space in every range — never pack a range to 100%.

## `suggest_names()`

Naming hygiene: empty names, and status GAs missing a status keyword.

The second one is not cosmetic. **Status pairing is driven by name tokens** — the engine matches
feedback to its command by looking for `status` / `state` / `статус` / `Rückmeldung`. A status GA
without a status keyword will not pair, which then shows up as a false `check_missing_status`
finding. Fixing the name fixes the analysis, not just the aesthetics.

Names should encode **zone + function**: "Kitchen ceiling light switch", "Bedroom blind position
status".

## Ordering

Run `check_missing_status()` and `check_dpt()` (or `analyze_all`) before `suggest_repairs` so
you can tie each proposal to the finding it answers. A proposal presented without its finding
reads as an arbitrary opinion about the user's project.

## Devices → addresses

When the gap is not a broken GA but a *missing* one, work from the device instead:

- **`decompose_device(order_number, channels=1)`** — a KNX actuator channel is never one GA. It
  expands into command/status/dimming/position/mode objects, each with its DPT. Accepts an order
  number, type or alias (`ZIO-MB24`, `dimmer`, `JRA/S`, `presence detector`). Use it to turn a
  spec/ТЗ device list into an address structure.
- **`list_device_recipes()`** — what the built-in library knows.
- **`parse_devices_from_project(path, output_path?, password?)`** — extracts the **exact vendor
  comm-object model** from the manufacturer application programs (`M-*`) inside a `.knxproj` or
  `.knxprod`, rather than a generic recipe. Read-only and PII-safe: it reads vendor catalog data
  only, never the client project part. With `output_path` it writes a device-library YAML into
  the workspace; point `NICKOL_KNX_CATALOG` at that directory to make `decompose_device` return
  catalog-exact results.

  A DPT reported as **`unverified`** means the vendor app-program declares none. It was not
  guessed, and you must not guess it either.

## What a good repair set looks like

Every controllable actuator ends up with a status object. Every GA has a DPT. Same logical name
means same DPT across the project. Commands and status stay in distinct, predictable middle
groups. Reserves remain in every range.

That target state is defined in the project's `CLAUDE.md`; see `design-rules.md`.
