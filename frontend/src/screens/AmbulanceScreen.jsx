// The crew's screen (?role=ambulance).
//
// Designed to be read at a glance from a moving vehicle, not studied: the
// destination is the largest thing on the page, and it changes underneath the
// crew when a REDIRECT_NOTICE lands. No modal, no dismissing — a crew should
// not have to clear a dialog to find out where they are going.
//
// Deliberately shows only what the crew has actually been *told*
// (known_destination). It never shows the dispatcher's current_destination:
// during a handoff those differ, and the whole point of this screen is to be
// an honest view of one endpoint's knowledge, not a window into dispatch.
import { Ambulance, Check, TriangleAlert } from "lucide-react";
import { api } from "../api.js";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const SPEED_KM_PER_MIN = 1.0;

function etaLabel(remainingKm) {
  if (remainingKm === null || remainingKm === undefined) return null;
  const minutes = remainingKm / SPEED_KM_PER_MIN;
  return minutes < 1 ? "under a minute" : `${Math.round(minutes)} min`;
}

export default function AmbulanceScreen({ ambulanceView, transportId }) {
  const destination = ambulanceView?.known_destination ?? null;
  const arrived = ambulanceView?.arrived ?? false;
  const pct = Math.round((ambulanceView?.progress ?? 0) * 100);
  const eta = etaLabel(ambulanceView?.remaining_km);

  return (
    <div className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-8 px-6 py-10">
      <div className="flex items-center gap-2.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
        <Ambulance className="size-4" />
        Ambulance · <span className="font-mono normal-case tracking-normal text-foreground">{transportId}</span>
      </div>

      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">
          {arrived ? "Arrived at" : destination ? "Proceed to" : "Awaiting destination"}
        </p>
        <p
          className={cn(
            "font-mono text-5xl font-semibold tracking-tight sm:text-6xl",
            destination ? "text-foreground" : "text-muted-foreground",
          )}
        >
          {destination ?? "—"}
        </p>
        {!destination && (
          <p className="flex items-center gap-2 text-sm text-danger">
            <TriangleAlert className="size-4" />
            Stand by — no hospital is currently able to receive this patient.
          </p>
        )}
      </div>

      {destination && (
        <div className="space-y-2">
          <div className="flex items-baseline justify-between text-sm">
            <span className="text-muted-foreground">{arrived ? "Complete" : "En route"}</span>
            <span className="font-mono text-muted-foreground">
              {arrived ? "100%" : eta ? `${eta} · ${pct}%` : `${pct}%`}
            </span>
          </div>
          <div className="h-3 w-full overflow-hidden rounded-full bg-muted">
            <div
              className={cn("h-3 rounded-full transition-all", arrived ? "bg-primary" : "bg-success")}
              style={{ width: `${Math.min(100, pct)}%` }}
            />
          </div>
        </div>
      )}

      <div className="space-y-2 border-t border-border pt-6">
        <Button size="lg" className="w-full" onClick={() => api.confirm(transportId)}>
          <Check className="size-4" />
          Confirm destination
        </Button>
        {/* This is not a formality. The acknowledgement is one of the two
            preconditions for cutover — dispatch will not release the old
            hospital until the crew has confirmed. In manual mode the ack is
            withheld until this is pressed, which is the clearest way to show
            why both proofs are required. */}
        <p className="text-center text-xs text-muted-foreground">
          Dispatch will not release the previous hospital until this is acknowledged.
        </p>
      </div>
    </div>
  );
}
