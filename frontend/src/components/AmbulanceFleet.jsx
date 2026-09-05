// Phase 21: the ambulance side of the multi-hospital view.
//
// The map already shows *where* each ambulance is; this shows what its crew
// has been told. Those are different facts, and the difference is the whole
// protocol: `known_destination` is what the ambulance was last told by a
// REDIRECT_NOTICE, while `current_destination` is the hospital the dispatcher
// has actually committed to. They diverge for exactly the length of a
// handoff — and a row where they disagree is a redirect in flight, which is
// the moment worth watching.
import { memo } from "react";
import { Ambulance, TriangleAlert } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

// Matches the backend default (SPEED_KM_PER_MIN); used only to turn the
// remaining distance into a human number, never for any decision.
const SPEED_KM_PER_MIN = 1.0;

function etaLabel(remainingKm) {
  if (remainingKm === null || remainingKm === undefined) return null;
  const minutes = remainingKm / SPEED_KM_PER_MIN;
  if (minutes < 1) return "<1 min";
  return `${Math.round(minutes)} min`;
}

function AmbulanceRow({ transportId, ambulance, dispatcherDestination }) {
  const crewDestination = ambulance.known_destination;
  const arrived = ambulance.arrived;
  // A redirect in flight: the dispatcher has moved on but the crew has not
  // been told yet (or has been told and the dispatcher hasn't cut over).
  const diverging = !arrived && crewDestination && dispatcherDestination && crewDestination !== dispatcherDestination;
  const standingBy = !arrived && !crewDestination;
  const pct = Math.round((ambulance.progress ?? 0) * 100);
  const eta = etaLabel(ambulance.remaining_km);

  return (
    <li className="flex flex-col gap-1.5 border-b border-line-soft px-4 py-2.5 last:border-b-0">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
        <span className="font-mono font-medium">{transportId}</span>

        {arrived ? (
          <Badge variant="outline" className="border-primary/30 bg-primary/10 font-normal text-foreground">
            arrived · {crewDestination}
          </Badge>
        ) : standingBy ? (
          <Badge variant="outline" className="border-danger-border bg-danger-soft font-normal text-danger">
            <TriangleAlert className="size-3" />
            no destination
          </Badge>
        ) : (
          <span className="text-muted-foreground">
            heading to <span className="font-mono text-foreground">{crewDestination}</span>
          </span>
        )}

        {diverging && (
          <Badge
            variant="outline"
            className="border-warning-border bg-warning-soft font-normal text-warning"
            title={`The crew is still driving to ${crewDestination}; the dispatcher has committed to ${dispatcherDestination}. This is a redirect mid-handoff.`}
          >
            redirect in flight → {dispatcherDestination}
          </Badge>
        )}

        {!arrived && eta && <span className="ml-auto font-mono text-muted-foreground">{eta}</span>}
      </div>

      <div className="flex items-center gap-2">
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
          <div
            className={cn(
              "h-1.5 rounded-full transition-all",
              arrived ? "bg-primary" : diverging ? "bg-warning" : standingBy ? "bg-danger" : "bg-success",
            )}
            style={{ width: `${standingBy ? 0 : Math.min(100, pct)}%` }}
          />
        </div>
        <span className="w-10 shrink-0 text-right font-mono text-[11px] text-muted-foreground">{pct}%</span>
      </div>
    </li>
  );
}

function AmbulanceFleet({ ambulances, transportRows }) {
  // Only the capacity-aware transports: the plain three-hospital demo has its
  // own AmbulancePanel with the manual-confirm control.
  const byId = Object.fromEntries((transportRows ?? []).map((row) => [row.transport_id, row]));
  const entries = Object.entries(ambulances || {})
    .filter(([transportId]) => transportId in byId)
    .sort(([a], [b]) => a.localeCompare(b));

  const enRoute = entries.filter(([, a]) => !a.arrived && a.known_destination).length;
  const stranded = entries.filter(([, a]) => !a.arrived && !a.known_destination).length;

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b border-border px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Ambulance className="size-4 text-muted-foreground" />
            Ambulances
            {entries.length > 0 && <span className="font-normal text-muted-foreground">{entries.length}</span>}
          </CardTitle>
          {entries.length > 0 && (
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              <span>
                <span className="font-mono text-foreground">{enRoute}</span> en route
              </span>
              {stranded > 0 && (
                <span className="text-danger">
                  <span className="font-mono">{stranded}</span> with nowhere to go
                </span>
              )}
            </div>
          )}
        </div>
      </CardHeader>

      <CardContent className="px-0">
        <div className="max-h-[352px] overflow-y-auto">
          {entries.length === 0 ? (
            <p className="px-4 py-8 text-center text-sm text-muted-foreground">
              No ambulances yet — start a transport or run a scenario preset.
            </p>
          ) : (
            <ul>
              {entries.map(([transportId, ambulance]) => (
                <AmbulanceRow
                  key={transportId}
                  transportId={transportId}
                  ambulance={ambulance}
                  dispatcherDestination={byId[transportId]?.current_destination}
                />
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export default memo(AmbulanceFleet);
