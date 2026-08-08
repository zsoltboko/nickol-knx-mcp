# Log forensics — analysing an ETS bus-monitor recording

The project says what a system *should* do. A recording shows what it **actually** does,
including logic that lives entirely outside ETS — a visualisation server or gateway with no ETS
application still writes to the bus.

Read-only. This parses an exported file; it does **not** connect to a bus.

## Input format — XML only

The ETS **`CommunicationLog` XML** export (bus monitor / group monitor → save as XML). The file
looks like:

```xml
<CommunicationLog xmlns="http://knx.org/xml/telegrams/01">
  <RecordStart Timestamp="..." ConnectionName="USB Interface (MDRC)" Mode="LinkLayer"/>
  <Telegram Timestamp="..." Service="L_Data.ind" FrameFormat="CommonEmi" RawData="2900BCE0..."/>
```

**There is no CSV parser.** If the user has a group-monitor CSV, they must re-export as XML —
say so plainly rather than attempting a workaround. The decoder needs the raw CommonEMI frame
in `RawData`; a CSV carries pre-decoded columns and no frame.

Parsing streams the file (`iterparse` with `elem.clear()`, DTD/entity-hardened, 2 GiB cap), so
**file size is not a memory concern**. The reduction knobs bound what is *kept for querying*,
not what can be read.

## Sequence

```
load_project(path)                    ← required; without it a recording is just hex
load_telegram_log(path, ...)          ← returns a SUMMARY, never telegrams
  → branch on the returned `hint`:
      unknown senders present  → log_reality_check()      ← the interesting case
      all senders known        → log_overview()
  → log_ga_activity(...)              per-address digest
  → log_series([...])                 compare signals over time
  → log_telegrams(...)                last resort only
```

The `hint` field in `load_telegram_log`'s return is a deliberate routing signal. Follow it.
Senders absent from the project are almost always the reason someone is analysing a recording
at all.

## Reduction knobs on `load_telegram_log`

| Knob | Effect |
|---|---|
| `change_only` (default **true**) | keep a telegram only when the value on that GA changed. Typically an order-of-magnitude reduction |
| `dedupe_window` (seconds) | with `change_only`, re-admit an unchanged value this often — a heartbeat, so a flat signal stays visible in a series instead of vanishing |
| `since` / `until` | ISO timestamp (`2026-07-14T10:30:00Z`) **or an offset from the recording's start** (`+90m`, `-2h`). Applied before decoding |
| `max_records` | cap on kept records |
| `from_end` | with `max_records`, keep the tail instead of the head |

**Aggregate counts stay complete regardless of reduction.** They are built during the streaming
pass, before anything is discarded. This is why a digest is trustworthy even on a truncated
load — and it is why several results carry **both** numbers:

- `telegrams` — the true count on that address, over the whole window
- `stored` — how many records survived reduction and can be inspected individually

If you quote a traffic figure, quote `telegrams`. If a user asks why they cannot see individual
frames for all of them, the answer is `stored`, and the fix is `change_only=false` on a narrower
`since`/`until` window.

`load_telegram_log` also reports `reduction_percent`, `truncated`, `skipped_undecodable`
(point-to-point and unknown frames are skipped, never guessed at) and `unknown_senders`.

## `log_reality_check(limit?)` — the highest-value call

Four questions the project alone cannot answer, plus one weak one. What each result licenses
you to conclude:

**`ghost_senders`** — individual addresses transmitting that are **not in ETS at all**. Per
ghost: how many telegrams, which GAs it writes to (with `senderless_in_project` per target) and
what it reads. This is a visualisation server, a gateway, a logic module, someone's Node-RED.
Strong conclusion: something outside the documented project is driving this bus.

**`senderless_but_written`** — addresses the project says *nothing transmits on*, which
nevertheless get written, and by whom. Strong conclusion: an external writer exists, and the
project model is incomplete.

**`unexpected_senders`** — an address has senders the project does not list as transmitters.
Includes `expected_senders`, `observed_senders` and `multi_sender`. Moderate conclusion: worth
investigating, but a legitimately shared address (a central function) looks the same.

**`group_addresses_not_in_project`** — traffic on addresses the `.knxproj` does not contain.

**`silent`** — project addresses that produced no telegram. **The tool ships a `caveat` string
with this result. Repeat it.** Over a short recording, silence is not evidence of a dead
function: event-driven addresses (mode switching, heat/cool changeover, alarm paths) may simply
not have fired. Never let a user delete an address because it was silent for 40 minutes.

## `log_overview(top?)`

Traffic by main group (with the main-group name from the project), rate per minute, service and
APCI breakdown, top senders (each flagged `in_project`), busiest addresses, and
`unknown_senders`. Use it to orient before drilling in — and to notice a main group carrying an
implausible share of the traffic.

## `log_ga_activity(...)`

Per-address digest: `telegrams`/`stored`, `first_seen`/`last_seen`, `senders`,
`unexpected_senders`, `senderless_in_project`, `read_by`, plus value summary with `dpt` and
`dpt_source`.

Filters worth knowing:

- `ga` — exact `6/2/5`, prefix `6/2` or `6/`, or a zone-template wildcard `7/x/8` (that
  sub-address across every middle group — the fast way to compare one function across all zones)
- `senderless_only` — only addresses where an observed sender proves an external writer
- `unexpected_sender_only` — only addresses with writers the project does not expect
- `min_count` — ignore low-traffic noise

**`dpt_source` is not decoration.** `project`/`object` means the type is declared;
`inferred` means it was deduced from payload width and the communication object's function text.
Report an inferred value as a deduction. A payload that does not fit its type falls back to raw
hex — if you see hex, the decode failed; do not invent a reading.

## `log_series(gas, max_points?, agg?)`

Time series for several addresses on **identical time buckets**, so two signals line up and can
be compared directly. This is how a control loop that exists in no documentation gets found —
a setpoint that tracks a measured dew point, a valve that follows an outdoor temperature.

- `gas` takes the same pattern syntax as `log_ga_activity`
- `max_points` 2..2000 per series; downsampling is mandatory
- `agg` — `last` (default), `mean`, `min`, `max` within each bucket. Use `mean` for a noisy
  analogue signal, `last` for a state
- **Read requests are excluded** — they carry no value and would plot as noise

Each series reports its own `dpt_source`. Two series with different provenance should not be
compared as if both were measured facts.

## `log_telegrams(...)` — last resort

Hard-capped at 200. If the filter matches more, it **refuses and returns the match count**
rather than silently truncating, so you can narrow `ga` / `src` / `since` / `until`. Prefer
`log_ga_activity` or `log_series`: they answer most questions at a fraction of the size.

Reach for it only when the exact sequence of individual frames is the question — a race, an
ordering problem, a burst.

## Known limits — state them, don't work around them

- **3-level group addresses are hardcoded** in the log decoder's address formatting. A 2-level
  project will decode wrong. Check `ga_style` from `load_project` first.
- **CSV is not supported** (see above).
- Only group-addressed `L_Data` Read / Response / Write frames are decoded. Point-to-point
  traffic is counted as `skipped_undecodable`, not guessed at.

## Where this fits

A recording is evidence about the *installed* system, so it belongs alongside — not instead of —
the project audit. A typical real use: `analyze_all` says a GA has no sender; the recording
shows it being written 400 times an hour by an address that is not in ETS. Neither half tells
that story alone.
