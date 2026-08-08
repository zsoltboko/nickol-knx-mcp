# nickol-knx — the workflow skill

The MCP server ships **37 tools**. This skill ships the thing the tools cannot carry themselves:
**the order to use them in, and how to read what they return.**

Without it, a fresh session sees 37 undifferentiated tool names. It does not know that
`load_project` must come first, that `grade_completeness` changes how every later finding should
be read, that `check_policy` measures a project against *its own* convention rather than a
standard, that an `inferred` DPT is a deduction, or that a group address which stayed silent for
40 minutes has not been proven dead.

## What's inside

| File | Covers |
|---|---|
| [SKILL.md](SKILL.md) | iron rules, the state model, the route table, how to read a confidence claim |
| [references/audit.md](references/audit.md) | judging an existing `.knxproj` |
| [references/repair.md](references/repair.md) | proposing fixes and routing them through ETS |
| [references/generate.md](references/generate.md) | ETS export, HA package, handover pack, test protocol |
| [references/log-forensics.md](references/log-forensics.md) | analysing an ETS bus-monitor recording |
| [references/compose.md](references/compose.md) | building a new project from room templates |
| [references/design-rules.md](references/design-rules.md) | pointer to `CLAUDE.md`, the authoritative design rules |

`SKILL.md` is short on purpose — it always loads. The `references/` files load only when that
path is taken.

Design rules for what a *good* KNX project looks like (GA taxonomy, DPT table, naming,
KNX Secure) stay in the repo's [`CLAUDE.md`](../../CLAUDE.md). This skill does not duplicate
them; it covers tool orchestration.

## Install

The skill needs the MCP server. Register that first — see
[Connecting to Claude](../../README.md#connecting-to-claude) in the main README.

### Claude Code

Copy or symlink the skill folder into a skills directory:

```bash
mkdir -p ~/.claude/skills && cp -r skills/nickol-knx ~/.claude/skills/
```

`~/.claude/skills/` makes it available everywhere; `.claude/skills/` inside a project scopes it
to that project. On Windows the user-level path is `%USERPROFILE%\.claude\skills\`.

Start a new session afterwards.

### Claude Desktop

Desktop does **not** read this repository's `skills/` directory, and it does not load `CLAUDE.md`
either. Zip the skill folder and add it through the app's skill/capability settings:

```bash
cd skills && zip -r nickol-knx.zip nickol-knx
```

Keep `SKILL.md` at the top level inside the zipped folder — that file's YAML frontmatter is what
makes the skill discoverable.

### MCP config paths

The main README's Claude Desktop section gives the macOS path. The full set:

| OS | `claude_desktop_config.json` |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

A complete multi-server example is in
[`examples/claude_desktop_config.json`](../../examples/claude_desktop_config.json). For Claude
Code, the same `mcpServers` block goes in `.mcp.json` at the project root.

## Check that it worked

Ask, in a fresh session, without naming any tool:

> Analyse my ETS project at `<path>`.

It should call `load_project` first, then `grade_completeness` before passing judgement. For the
recording path:

> I have a bus monitor recording from this installation — what is actually going on?

It should load the project first, then the recording, branch on the returned `hint`, and — the
real test — **state the `silent` caveat and flag any `inferred` DPT without being asked.**
Those caveats are the reason the skill exists.

## Related

- [`ha-git-backup`](../ha-git-backup) — the ops companion: what happens to your Home Assistant
  config *after* `generate_ha_package` deploys it.
