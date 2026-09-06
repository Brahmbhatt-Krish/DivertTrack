// State badge, epoch, STALE badge, and the last accepted / last rejected
// command — all derived here from the shared timeline, not fetched.
import { Building2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const STATE_STYLES = {
  IDLE: "border-border bg-muted text-muted-foreground",
  ARMED: "border-warning-border bg-warning-soft text-warning",
  ACTIVE: "border-success-border bg-success-soft text-success",
  WITHDRAWN: "border-border bg-muted text-muted-foreground",
  // Not a FacilityState. The protocol has four states and arrival is not one
  // of them — a hospital the patient has reached is still ACTIVE, and adding
  // a fifth state would ripple through the transition table and the checker.
  // This is a display label for "ACTIVE, and the ambulance is here".
  ARRIVED: "border-primary/30 bg-primary/10 text-foreground",
};

const ACCEPTED_TYPES = new Set(["FacilityStateChanged"]);
const REJECTED_TYPES = new Set(["StaleIgnored", "DuplicateIgnored", "IllegalTransition", "TargetMismatch"]);

function lastMatching(events, facilityId, predicate) {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.facility_id === facilityId && predicate(event)) return event;
  }
  return null;
}

function describe(event) {
  if (!event) return "—";
  const action = event.payload?.action;
  return action ? `${event.type} (${action})` : event.type;
}

export default function FacilityCard({ facilityId, view, events, arrivedAt = null, large = false }) {
  const state = view?.state ?? "IDLE";
  const epoch = view?.epoch ?? 0;
  const stale = view?.stale ?? false;
  // Only the hospital the ambulance actually reached, and only while it is
  // still the active one — a facility that has since been withdrawn from
  // should not keep claiming the patient.
  const arrived = arrivedAt === facilityId && state === "ACTIVE";
  const badge = arrived ? "ARRIVED" : state;

  const lastAccepted = lastMatching(events, facilityId, (e) => ACCEPTED_TYPES.has(e.type));
  const lastRejected = lastMatching(events, facilityId, (e) => REJECTED_TYPES.has(e.type));

  return (
    // Hospitals are the *participants* in the protocol: they answer, they
    // never decide the handoff. The dispatcher card is marked differently on
    // purpose, so the architecture reads off the screen without narration.
    <Card className={cn("gap-0 border-l-2 border-l-border py-4", large && "py-6")}>
      <CardHeader className="px-4 pb-3">
        <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          <Building2 className="size-3" />
          Receiving facility
        </div>
        <div className="flex items-center justify-between gap-2">
          <CardTitle className={cn("font-mono text-sm", large && "text-xl")}>{facilityId}</CardTitle>
          {stale && (
            <Badge variant="outline" className="border-warning-border bg-warning-soft text-warning">
              STALE
            </Badge>
          )}
        </div>
        <div className="mt-2 flex items-center gap-2">
          <Badge
            variant="outline"
            className={cn("font-medium", STATE_STYLES[badge] ?? STATE_STYLES.IDLE)}
            title={arrived ? "The ambulance has arrived. The facility itself is still ACTIVE." : undefined}
          >
            {badge}
          </Badge>
          <span className="text-xs text-muted-foreground">epoch {epoch}</span>
        </div>
      </CardHeader>

      <CardContent className="space-y-1.5 px-4 text-xs">
        <div className="flex gap-2">
          <span className="w-24 shrink-0 text-muted-foreground">Last accepted</span>
          <span className="truncate" title={describe(lastAccepted)}>
            {describe(lastAccepted)}
          </span>
        </div>
        <div className="flex gap-2">
          <span className="w-24 shrink-0 text-muted-foreground">Last rejected</span>
          <span className="truncate" title={describe(lastRejected)}>
            {describe(lastRejected)}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}
