# DivertTrack

A simulation of ambulance destination reassignment that guarantees exactly
one hospital is ever actually receiving a given patient, no matter how
badly the network between the dispatcher and the hospitals misbehaves —
delayed messages, duplicated messages, out-of-order delivery. Nothing here
talks to real hardware; every clock, network link and hospital is
simulated, and the same core protocol runs identically under a fake,
instantaneous test clock and a real wall-clock server.

The base system (Phases 0-11) handles three fixed hospitals and one
transport at a time. An extension on top (Phases 12-20) generalizes this to
six hospitals with real bed capacity, many concurrent transports, and
patients with medical needs a hospital may have to decline — see
"Beyond the PRD" below for why that extension exists and what it actually
proves.

## The invariant

At every instant, for every transport that has been assigned a
destination, **exactly one** hospital is "effectively active" for it — see
below for what that means precisely. There is no tolerance window: this
must hold at the end of every atomic instant (every group of events sharing
one transport and one timestamp), not "most of the time" or "except during
a handover." A hospital being *locally* active without being the current
destination is not a violation of this rule — it is tracked separately, as
overlap, and is expected to happen briefly during a normal handover.

## The effective-active rule

A hospital is effectively active for a transport if and only if its local
state is `ACTIVE` **and** it is that transport's `current_destination`
right now. This definition is what makes the checker an independent audit
rather than a restatement of what the dispatcher believes: `checker.py`
replays the raw event log itself and never imports `Dispatcher` or
`Facility` — it can only agree or disagree with what actually happened.

## Activate-first, withdraw-after-proof — why safety doesn't depend on delay

The entire safety argument rests on one ordering rule, enforced by the
dispatcher regardless of how the network behaves:

**The old hospital is only ever told to withdraw after the new hospital has
proven its activation is scheduled.** Concretely: the dispatcher sends
`ACTIVATE_AT(new, T)` to the new hospital; only once that hospital
acknowledges *receipt* of that command does the dispatcher send
`WITHDRAW_AT(old, T)` to the old one. Both take effect at the same instant
`T`, computed once, up front, from the network's own declared worst case
(`3×D_MAX_MS + GUARD_MS`).

Why this can never produce a moment with nobody active: the new hospital
activates at `T` on its own clock regardless of how the withdraw message to
the old hospital fares afterward. The old hospital withdraws at `T` **or
later** — never earlier, because it was never told to withdraw until the
new hospital's activation was already provably in motion. A slow, duplicated
or reordered network can only make a handover take longer or produce a
brief overlap where both are locally active; it cannot produce a gap. The
bound `D_MAX_MS` affects only how long a transition takes and how long a
stale ("this hospital used to be relevant") badge stays visible — not
whether the invariant holds.

## Epochs

Every redirect (and every abort, and every cancel) allocates
`epoch = highest_epoch_seen + 1` for that transport. Epochs are per
transport, start at 1, never reused, never decrease. This is the fencing
mechanism that lets an abandoned plan's in-flight, delayed messages be
recognized and ignored on arrival, on both the dispatcher's side and the
receiving hospital's side, independently.

## Timing summary

Worst case from redirect to cutover, honoring the bound:
`PREP_MS + 2·D_MAX_MS + 3·D_MAX_MS + GUARD_MS`.

The repository's own `.env` keeps `D_MAX_MS=1500` (matching the test
suite's assumptions). For a demo where handovers are visibly fast, set
`D_MAX_MS=600` — a handover then takes roughly three seconds and every
state change (`PREPARE` → `ARMED` → `ACTIVATE_AT` → `ACTIVE`, the old
hospital's `WITHDRAWN`) is visible on screen rather than flashing past.

## How to run

```bash
make install     # backend venv + pip install, frontend npm install
make dev          # backend on :8000, frontend on :5173, both with reload
make test         # full backend suite
make test-fuzz    # just the (slow) property-based fuzz suite
make demo         # backend with human-paced delays for a live walkthrough
```

The frontend has no build step for the demo — `npm run dev` (via `make dev`)
serves it directly. `?role=dashboard` (default) shows the full operator
view; `?role=hospital_A|hospital_B|hospital_C` and `?role=ambulance` show
single-endpoint views for a multi-screen demo.

## Test output

```
$ make test
...
206 passed, 2 warnings in 46.47s
```

136 of those predate the multi-hospital extension (Phases 0-11: the
handoff protocol itself, the API, the frontend's data flow, Phase 10's AI
sidecar). The other 70 were added across Phases 12-19 for the extension
below. All 206 pass together — the extension's own regression requirement
("every pre-existing test must pass unmodified") holds.

**Fuzz**: three properties, each checked by property-based testing over
randomized scenarios via Hypothesis:

- `test_the_invariant_holds_under_fuzzed_concurrent_redirects` (90 examples,
  `strict_bound=True`) — the base protocol under 1-30 concurrent transports
  and randomized overlapping redirects.
- `test_the_invariant_holds_with_strict_bound_disabled_and_delays_up_to_3x_d_max`
  (90 examples) — the same, with the network allowed to violate `D_MAX_MS`
  by up to 3x (E25: safety must not depend on the bound actually holding).
- `test_capacity_invariants_hold_under_fuzzed_hospitals_and_patients` (25
  examples) — the extension's own fuzz: 1-6 randomized hospitals, 1-10
  randomized patients, both policies. Fewer examples than the two above by
  design (each example is far more expensive to construct — a random
  hospital network, not just a random delay table — and this session
  prioritized covering more of the build over maximizing one fuzz test's
  example count).

All three pass with zero violations at the counts above.

---

# The multi-hospital capacity extension

Phases 12-20 generalize the system above from "three fixed hospitals, one
transport" to six hospitals with real bed/staffing/equipment capacity, many
concurrent transports, and patients a hospital may have to decline. The
handoff protocol itself — epochs, activate-first, withdraw-after-proof,
fencing on both sides — is **completely unchanged**; everything below is a
layer on top of it, gated so the original three-hospital demo behaves
exactly as before whenever a transport has no associated patient.

## The acceptance function

Before a hospital is ever asked to prepare for a transport, one pure
function decides whether it may: `acceptance.accept(patient, hospital,
status, ledger_view, eta_minutes, saturation_limit) -> Decision`. It runs
seven checks, in this exact order, and returns the *first* failure:

| # | Check | Declines with |
|---|-------|----------------|
| 1 | Diversion | `on_full_diversion` / `on_partial_diversion:<category>` |
| 2 | Required capabilities present (+ a free ventilator if needed) | `no_capability:<name>` / `no_ventilator` |
| 3 | A bed of the required type is free | `no_bed:<type>` |
| 4 | The required specialist is on shift | `no_specialist:<name>` |
| 5 | ETA is within the condition's transport-time window (skipped for a `nearest_capable` override) | `outside_window` |
| 6 | ED isn't saturated for a non-critical patient (skipped for override) | `ed_saturated` |
| 7 | — | accepted, with the required bed type |

The same function runs in two places for the same rule: the **dispatcher**
calls it on its own last-known view to decide who to even ask (and to rank
candidates); the **facility** calls it again on its own live, authoritative
view when a `PREPARE` actually arrives. The facility's answer always wins —
a dispatcher acting on stale information gets `DECLINED` and moves on (see
R15 below), it never overrides what the hospital says.

## The bed ledger

`projection.project_ledger(events, patients)` is a pure replay of three
event types — `BedReserved`, `BedReleased`, `BedOccupied` — into, per
hospital, which transports currently hold a reservation or an occupancy in
each bed type. `free()`, `load()` and `ventilators_free()` are pure
functions of that view plus a hospital's live `HospitalStatus`. A bed is
**reserved** the moment the dispatcher decides to try a candidate (before
it even knows if that hospital will accept), and stays reserved through
acceptance and preparation; it becomes **occupied** only once the ambulance
actually arrives, and is released the moment the plan involving it is
abandoned for any reason (declined, cancelled, aborted, or the old
hospital at a successful cutover).

## Rebalance (R18)

If a hospital's own capacity drops (`BedsReported`) below what's currently
reserved and occupied there, the dispatcher displaces just enough
reservations to close the deficit — **least-critical first** (highest
acuity number), and among equally critical ones, the most recently
reserved first. A displaced transport still mid-transition to that hospital
is treated exactly like a declined candidate (release the bed, try the next
one, or abort). A displaced transport that is *already* the current,
active destination there keeps its bed if it has nowhere else to go — the
ledger may briefly over-report in that case, which the checker records as
a warning (`CAPACITY_BREACH_UNRESOLVED`), never as a handoff-safety
violation. The handoff invariant above is never at stake here: rebalance
only ever touches capacity bookkeeping, never who is effectively active.

## The three new invariants

- **I2 — no overbooking**: for every hospital and bed type, at the end of
  every atomic instant, `reserved + occupied ≤ beds_total`. A breach caused
  by a `BedsReported` drop that rebalance couldn't fully resolve is a
  warning; any other breach (something reserved a bed capacity didn't
  allow) is a violation.
- **I3 — arrival has a reservation**: every `Arrived{at}` must be preceded
  by a live (not yet released) `BedReserved` for that transport at that
  hospital.
- **I4 — every acceptance was valid**: replaying a hospital's status and
  ledger at the moment it confirmed readiness, `accept()` must still say
  yes. (Implementation note: `eta_minutes` can't be perfectly reconstructed
  from the event log alone — a transport's exact position at that instant
  isn't itself logged — so this check approximates it as 0, which can only
  ever produce a false pass on the transport-time-window check specifically,
  never on capability/bed/specialist/diversion/saturation.)

## Concurrency note

All dispatcher operations in this simulation run on one event loop and
never interleave — "reserve at `PREPARE`" is race-free without any locking,
because two reservations for the same hospital can never actually execute
at the same instant; whichever call happens to run first simply sees the
result of it. **If this dispatcher were ever sharded across multiple
processes**, handoffs themselves would shard cleanly by transport (nothing
about the protocol requires transports to share a process) — but the bed
ledger is per-hospital and is written to by every transport competing for
that hospital's beds, so it would need its own coordinator (e.g. one
authoritative process per hospital, or a transactional store) to keep the
same race-free guarantee. This system does not build that; it is a real
limitation of scaling this design horizontally, stated here rather than
either ignored or half-solved.

## Beyond the PRD

Strip away the ambulance framing and what's actually been built is a
general **exactly-one-owner handoff kernel**: a way to move "current
owner of X" from A to B, live, under a network that can delay, duplicate
and reorder messages arbitrarily, while guaranteeing at least one owner is
always active and — the harder half — that the *right* one is checkable
independently after the fact from nothing but an append-only log. That
pattern shows up anywhere a single execution or connection needs a clean
handoff without downtime: leader election in a distributed system,
in-place feature-flag or traffic cutover, a database failover that must
never leave zero primaries.

The capacity extension on top proves the same kernel composes with a
second, orthogonal hard problem — bounded-resource admission control —
without weakening the first one. Bed reservation, candidate ranking and
rebalance are all *additions* layered on the handoff protocol; none of
them required touching a single line of the original epoch-fencing,
activate-first, withdraw-after-proof machinery. That composability, not any
one feature, is the actual claim of this extension: a safety property
proven once, independently checkable, can carry real additional complexity
on top of it without re-litigating the proof.
