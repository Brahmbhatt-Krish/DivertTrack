// One hospital's own screen (?role=hospital_A|hospital_B|hospital_C).
//
// A receiving-department wall display: what *this* facility has been asked to
// do and what state it is in. It shows only this hospital's own knowledge —
// never the dispatcher's view, never the other hospitals — because that is the
// honest boundary, and watching three of these side by side is what makes the
// protocol visible.
//
// The state badge is the whole screen. ARMED means "prepare the bay"; ACTIVE
// means "this patient is yours"; WITHDRAWN means "stand down". A receiving
// team needs one word from across a room.
import { Building2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const STATE_COPY = {
  IDLE: { label: "IDLE", hint: "No inbound patient." },
  ARMED: { label: "PREPARING", hint: "Prepare to receive — not yet confirmed." },
  ACTIVE: { label: "RECEIVING", hint: "This patient is inbound to you." },
  WITHDRAWN: { label: "STOOD DOWN", hint: "Released — the patient is going elsewhere." },
  ARRIVED: { label: "ARRIVED", hint: "The patient is here." },
};

const STATE_STYLES = {
  IDLE: "border-border bg-muted text-muted-foreground",
  ARMED: "border-warning-border bg-warning-soft text-warning",
  ACTIVE: "border-success-border bg-success-soft text-success",
  WITHDRAWN: "border-border bg-muted text-muted-foreground",
  ARRIVED: "border-primary/30 bg-primary/10 text-foreground",
};

const REJECTED_TYPES = new Set([
  "StaleIgnored", "DuplicateIgnored", "IllegalTransition", "TargetMismatch",
]);

function lastMatching(events, facilityId, predicate) {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.facility_id === facilityId && predicate(event)) return event;
  }
  return null;
}

export default function HospitalScreen({ facilityId, view, events, arrivedAt }) {
  const rawState = view?.state ?? "IDLE";
  const arrived = arrivedAt === facilityId && rawState === "ACTIVE";
  const state = arrived ? "ARRIVED" : rawState;
  const copy = STATE_COPY[state] ?? STATE_COPY.IDLE;

  const lastAccepted = lastMatching(events, facilityId, (e) => e.type === "FacilityStateChanged");
  // The rejections are the interesting half: each one is a message that would
  // have corrupted this facility's state, arriving and doing nothing.
  const rejections = events
    .filter((e) => e.facility_id === facilityId && REJECTED_TYPES.has(e.type))
    .slice(-4)
    .reverse();

  return (
    <div className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-8 px-6 py-10">
      <div className="flex items-center gap-2.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
        <Building2 className="size-4" />
        Receiving facility ·{" "}
        <span className="font-mono normal-case tracking-normal text-foreground">{facilityId}</span>
      </div>

      <div className="space-y-4">
        <div
          className={cn(
            "inline-flex rounded-lg border px-5 py-3 font-mono text-3xl font-semibold tracking-tight sm:text-4xl",
            STATE_STYLES[state] ?? STATE_STYLES.IDLE,
          )}
        >
          {copy.label}
        </div>
        <p className="text-base text-ink-soft text-muted-foreground">{copy.hint}</p>
        <p className="font-mono text-xs text-muted-foreground">epoch {view?.epoch ?? 0}</p>
      </div>

      <div className="space-y-2 border-t border-border pt-6 text-sm">
        <div className="flex gap-3">
          <span className="w-32 shrink-0 text-muted-foreground">Last instruction</span>
          <span className="font-mono text-xs">
            {lastAccepted
              ? `${lastAccepted.payload.action ?? lastAccepted.type} → ${lastAccepted.payload.to ?? "—"}`
              : "—"}
          </span>
        </div>
        <div className="flex gap-3">
          <span className="w-32 shrink-0 text-muted-foreground">Ignored</span>
          <div className="min-w-0 space-y-0.5">
            {rejections.length === 0 ? (
              <span className="font-mono text-xs text-muted-foreground">—</span>
            ) : (
              rejections.map((event) => (
                <p key={event.seq} className="truncate font-mono text-xs text-warning">
                  {event.type} · {event.payload.command_id ?? ""}
                </p>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
