// State badge, epoch, STALE badge, and the last accepted / last rejected
// command — all derived here from the shared timeline, not fetched.
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const STATE_STYLES = {
  IDLE: "border-border bg-muted text-muted-foreground",
  ARMED: "border-warning-border bg-warning-soft text-warning",
  ACTIVE: "border-success-border bg-success-soft text-success",
  WITHDRAWN: "border-border bg-muted text-muted-foreground",
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

export default function FacilityCard({ facilityId, view, events, large = false }) {
  const state = view?.state ?? "IDLE";
  const epoch = view?.epoch ?? 0;
  const stale = view?.stale ?? false;

  const lastAccepted = lastMatching(events, facilityId, (e) => ACCEPTED_TYPES.has(e.type));
  const lastRejected = lastMatching(events, facilityId, (e) => REJECTED_TYPES.has(e.type));

  return (
    <Card className={cn("gap-0 py-4", large && "py-6")}>
      <CardHeader className="px-4 pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className={cn("font-mono text-sm", large && "text-xl")}>{facilityId}</CardTitle>
          {stale && (
            <Badge variant="outline" className="border-warning-border bg-warning-soft text-warning">
              STALE
            </Badge>
          )}
        </div>
        <div className="mt-2 flex items-center gap-2">
          <Badge variant="outline" className={cn("font-medium", STATE_STYLES[state] ?? STATE_STYLES.IDLE)}>
            {state}
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
