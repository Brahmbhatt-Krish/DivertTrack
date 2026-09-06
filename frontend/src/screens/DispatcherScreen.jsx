// The control room (?role=dispatcher).
//
// What dispatch actually operates: the safety verdict, the transition it is
// driving, what is in flight on the wire, the controls, and the log. It
// deliberately does NOT show each hospital's internal state — dispatch only
// knows what it has been told, and putting the hospitals' own screens here
// would blur the boundary the whole protocol is built on.
//
// The facilities row is included as "what dispatch believes", which is the
// honest framing: it is derived from acks that have arrived, and during a
// handoff it can lag what the hospital itself already knows.
import { RadioTower } from "lucide-react";
import InvariantBadge from "../components/InvariantBadge.jsx";
import FacilityCard from "../components/FacilityCard.jsx";
import TransitionPanel from "../components/TransitionPanel.jsx";
import Controls from "../components/Controls.jsx";
import Timeline from "../components/Timeline.jsx";

export default function DispatcherScreen({
  state,
  transportId,
  facilityIds,
  transportView,
  invariantResult,
  manualMode,
  onManualModeChange,
  facilityKey,
}) {
  return (
    <main className="mx-auto max-w-6xl space-y-5 px-6 py-8">
      <div className="flex items-center gap-2.5 text-xs font-medium uppercase tracking-wider text-primary/70">
        <RadioTower className="size-4" />
        Dispatch · <span className="font-mono normal-case tracking-normal text-foreground">{transportId}</span>
      </div>

      <InvariantBadge result={invariantResult} />

      <TransitionPanel view={transportView} events={state.timeline} />

      <div>
        <p className="mb-2 text-xs text-muted-foreground">
          What dispatch believes each facility is doing — derived from acknowledgements that have
          arrived, so it can briefly lag what the facility itself already knows.
        </p>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {facilityIds.map((facilityId) => (
            <FacilityCard
              key={facilityId}
              facilityId={facilityId}
              view={state.facilities[facilityKey(facilityId, transportId)]}
              events={state.timeline}
              arrivedAt={transportView?.arrived_at ?? null}
            />
          ))}
        </div>
      </div>

      <Controls
        transportId={transportId}
        inFlight={state.inFlight}
        manualMode={manualMode}
        onManualModeChange={onManualModeChange}
      />

      <Timeline events={state.timeline} />
    </main>
  );
}
