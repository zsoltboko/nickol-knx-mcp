# Generate — exports, packages and deliverables

## The ordering rule

```
analyze_all → fix 🔴 errors in ETS → project_report → HUMAN REVIEW → generate → import/deploy
```

`project_report()` before any import or deploy is not a suggestion. Generating a Home Assistant
package from a project with missing DPTs produces YAML that looks fine and silently cannot
decode half the house.

Every generator takes an optional `output_path` (or `output_dir`). **With it, the file is
written into the confined workspace and the path returned; without it, the content comes back
inline.** Prefer writing for anything the user will actually use — inline YAML in a chat
transcript is not a deployable artifact, and large exports flood the context.

## `generate_ets_group_addresses(fmt="xml", output_path?)`

`fmt="xml"` (ga-export/01) is recommended and the default; `fmt="csv"` is the native ETS layout.
Anything else raises.

This is how accepted `suggest_repairs` proposals reach ETS. Import it in ETS; nothing is applied
to the project by this server.

## `generate_ha_package(output_path?)`

Home Assistant KNX package YAML — colour lights and climate assembled, plus an `expose` block
for date/time broadcast (DPT 19.001).

**Always surface the `review` list.** It is the deliberate design of this tool: anything
ambiguous is *not guessed*. Typical entries:

- **DPT 5.001 — brightness or blind position?** The DPT alone cannot say.
- **Cover flags** — `invert_position`, travel times. No `.knxproj` encodes these; they are
  actuator- and installation-dependent. The user must supply them.

Deploying the YAML without walking the review list is how a blind ends up opening when told to
close. Report `counts` and `review` before the YAML itself.

Every HA entity must carry a `state_address` (or `*_state_address`) wherever the KNX device can
report — that is what makes HA read real state instead of assuming it.

## `generate_handover_pack(output_dir?)`

The as-built commissioning deliverable. Assembles equipment inventory, GA map by domain,
command/status coverage, KNX Secure scope and QA state into `handover.md`, and with `output_dir`
also writes:

| File | Contents |
|---|---|
| `handover.md` | the narrative deliverable |
| `topology.svg` | topology diagram |
| `group-addresses.csv` | full ETS CSV export |
| `ha-package.yaml` | the HA package |

Without `output_dir` only the markdown and SVG come back inline. For a real handover, write the
directory — the point is the file set.

Run `grade_completeness()` first. A handover pack generated from a skeleton project is a
misleading document to give a client.

## `generate_test_protocol(output_path?)`

Drafts a functional acceptance protocol: per function, **command → expected status**, with
pass/fail and sign-off columns. Execution is manual and on-site; this only drafts it.

It is the natural consumer of good command/status pairing — which is why projects that fail
`check_missing_status` produce a thin protocol. If the protocol looks short, that is a finding
about the project, not about the tool.

## `generate_knx_iot(output_path?)`

KNX IoT semantic export (Turtle/RDF) of the project's functional datapoints. A pragmatic
skeleton for the IP-native model, for review — not a certified export.

## `project_report(output_path?, name_regex?)`

The human-readable Markdown report, and the gate in front of everything above. Returns the
`summary` plus the markdown (or the written path). Section 2.4 covers topology findings, and the
totals include them.

## After generating

Commit the artifacts. They live in the git-tracked workspace so every step is reversible — and
so `diff_projects` and a git diff between report versions can show what a round of changes
actually did.

Once deployed, the life of the Home Assistant config itself is a separate concern: see the
[`ha-git-backup`](../../ha-git-backup) skill.
