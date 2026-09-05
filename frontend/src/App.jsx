// One page, ?role= filters the view: dashboard (default) | hospital_A |
// hospital_B | hospital_C | ambulance.
import { useMemo, useState } from "react";
import { useHubState, facilityKey } from "./state.js";
import TopBar from "./components/TopBar.jsx";
import InvariantBadge from "./components/InvariantBadge.jsx";
import FacilityCard from "./components/FacilityCard.jsx";
import Timeline from "./components/Timeline.jsx";
import TransitionPanel from "./components/TransitionPanel.jsx";
import Controls from "./components/Controls.jsx";
import AmbulancePanel from "./components/AmbulancePanel.jsx";
import FuzzPanel from "./components/FuzzPanel.jsx";

const DEMO_TRANSPORT_ID = "AMB-101";
const FACILITY_IDS = ["Hospital_A", "Hospital_B", "Hospital_C"];
// The spec's role values (?role=hospital_A|hospital_B|hospital_C) use a
// lowercase "hospital_" prefix, distinct from the actual facility ids
// ("Hospital_A", ...) used everywhere else in the app and backend.
const ROLE_TO_FACILITY_ID = Object.fromEntries(FACILITY_IDS.map((id) => [`hospital_${id.slice(-1)}`, id]));

function useRole() {
  return useMemo(() => new URLSearchParams(window.location.search).get("role") ?? "dashboard", []);
}

export default function App() {
  const state = useHubState();
  const role = useRole();
  const [manualMode, setManualMode] = useState(false);

  const transportView = state.transports[DEMO_TRANSPORT_ID];
  const ambulanceView = state.ambulances[DEMO_TRANSPORT_ID];
  const invariantResult = state.invariant[DEMO_TRANSPORT_ID];

  if (role in ROLE_TO_FACILITY_ID) {
    const facilityId = ROLE_TO_FACILITY_ID[role];
    const view = state.facilities[facilityKey(facilityId, DEMO_TRANSPORT_ID)];
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50 p-8">
        <div className="w-full max-w-md">
          <FacilityCard facilityId={facilityId} view={view} events={state.timeline} large />
        </div>
      </div>
    );
  }

  if (role === "ambulance") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50 p-8">
        <div className="w-full max-w-md">
          <AmbulancePanel ambulanceView={ambulanceView} transportId={DEMO_TRANSPORT_ID} />
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50">
      <TopBar connected={state.connected} />
      <main className="mx-auto max-w-5xl space-y-4 p-4">
        <InvariantBadge result={invariantResult} />

        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {FACILITY_IDS.map((facilityId) => (
            <FacilityCard
              key={facilityId}
              facilityId={facilityId}
              view={state.facilities[facilityKey(facilityId, DEMO_TRANSPORT_ID)]}
              events={state.timeline}
            />
          ))}
        </div>

        <TransitionPanel view={transportView} events={state.timeline} />
        <AmbulancePanel ambulanceView={ambulanceView} transportId={DEMO_TRANSPORT_ID} />
        <Controls
          transportId={DEMO_TRANSPORT_ID}
          inFlight={state.inFlight}
          manualMode={manualMode}
          onManualModeChange={setManualMode}
        />
        <Timeline events={state.timeline} />
        <FuzzPanel fuzzResult={state.fuzz} />
      </main>
    </div>
  );
}
