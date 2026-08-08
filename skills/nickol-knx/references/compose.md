# Compose — a new project from room templates

For a greenfield project or a spec (ТЗ): build from rooms, not from a blank address sheet.

## Sequence

```
validate_room_template()                     ← no args: lists the available templates
validate_room_template(template="bedroom")   ← check one before relying on it
compose_rooms(rooms=[...], dry_run=True)     ← default: nothing written
compose_rooms(rooms=[...], dry_run=False, output_dir="...")
```

## `validate_room_template(template?, path?)`

Report-only, writes nothing. Called with **no arguments** it returns
`available_templates` — the fastest way to see what the library ships (bedroom, children,
living, kitchen, bathroom, corridor). Pass `template=<slot_id>` for a built-in or `path=` for a
custom YAML.

It checks the public contract: a locale-neutral `slot_id`, ru/en labels, per-slot basic/comfort
presets, known function types, valid multiplicities, and that **`area_m2` is a hint with
provenance, never a normative fact**. Do not present a template's area as a measurement of the
user's actual room.

The template format is documented in `nickol_knx_mcp/room_templates/SCHEMA.md`.

## `compose_rooms(rooms, language="ru", project_name?, output_dir?, dry_run=True)`

`rooms` is a list of specs, each `{template, preset?, slot_presets?, params?, label?}`:

| Key | Meaning |
|---|---|
| `template` | the slot_id, e.g. `"bedroom"` |
| `preset` | `basic` or `comfort`, for the whole room |
| `slot_presets` | override individual slots — **a house can mix comfort climate with basic lighting** |
| `params` | override template defaults (window counts, circuit counts) |
| `label` | a custom zone name |

Example: `[{"template": "bedroom", "preset": "comfort"}, {"template": "kitchen",
"preset": "basic", "params": {"circuits": 4}}]`

### What it actually does

Resolves templates + params to a functional model → allocates group addresses (**main = domain,
middle = role, sub sequential**) → writes a real `.knxproj` → **re-reads it with the standard
loader** → runs the normal linters on the re-read project.

That round-trip matters when you report results: the findings you get back came from the same
`analyze_all`-family checks applied to a genuinely parsed project, not from the composer's own
optimism.

Returns a `manifest` (the allocation), ETS GA XML/CSV, and a device `bom` proposal from the
device library.

### Dry run is the default

`dry_run=True` writes nothing. Set `dry_run=False` **with** `output_dir` (a folder inside the
workspace) to write the `.knxproj`, ETS exports, `manifest.yaml` and `bom.yaml`.

Show the user the manifest before writing. Address allocation is the decision that is expensive
to reverse later.

### R1 scope — say this plainly

- Builds **NEW projects only**. Docking into an existing project is R2.
- Exact device selection is R2; the `bom` is a proposal from the generic library.
- Never touches a bus.

`language` must be one of the supported set (default `ru`); it drives the generated labels.

## Filling the device side

The `bom` is generic. To make it exact, feed the local catalog:

`parse_devices_from_project(path, output_path=...)` on a `.knxproj`/`.knxprod` extracts real
vendor comm-object models; point `NICKOL_KNX_CATALOG` at that directory and `decompose_device`
returns catalog-exact expansions. See `repair.md` § "Devices → addresses".

## After composing

The output is a starting structure, not a finished project. Run the normal audit path on it
(`references/audit.md`), then `project_report` before anyone imports anything into ETS.

Reserve address space in every range — never pack a range to 100%. The composer leaves room;
keep it that way as the project grows.
