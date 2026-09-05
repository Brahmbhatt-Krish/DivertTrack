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
import AiPanel from "./components/AiPanel.jsx";
import HospitalsPanel from "./components/HospitalsPanel.jsx";
import TransportsTable from "./components/TransportsTable.jsx";
import RegionMap from "./components/RegionMap.jsx";
import AlertsPanel from "./components/AlertsPanel.jsx";
import MultiHospitalControls from "./components/MultiHospitalControls.jsx";
import GlobalInvariantBadge from "./components/GlobalInvariantBadge.jsx";

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
  const [refreshKey, setRefreshKey] = useState(0);
  const bumpRefresh = () => setRefreshKey((k) => k + 1);

  const transportView = state.transports[DEMO_TRANSPORT_ID];
  const ambulanceView = state.ambulances[DEMO_TRANSPORT_ID];
  const invariantResult = state.invariant[DEMO_TRANSPORT_ID];

  if (role in ROLE_TO_FACILITY_ID) {
    const facilityId = ROLE_TO_FACILITY_ID[role];
    const view = state.facilities[facilityKey(facilityId, DEMO_TRANSPORT_ID)];
    return (
      <div className="flex min-h-screen items-center justify-center p-8">
        <div className="w-full max-w-md">
          <FacilityCard facilityId={facilityId} view={view} events={state.timeline} large />
        </div>
      </div>
    );
  }

  if (role === "ambulance") {
    return (
      <div className="flex min-h-screen items-center justify-center p-8">
        <div className="w-full max-w-md">
          <AmbulancePanel ambulanceView={ambulanceView} transportId={DEMO_TRANSPORT_ID} />
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <TopBar connected={state.connected} />

      <main className="mx-auto max-w-6xl space-y-10 px-6 py-8">
        <section className="space-y-4">
          <SectionHeading
            title="Single transport"
            subtitle={`The handoff protocol on one ambulance (${DEMO_TRANSPORT_ID}) across three facilities.`}
          />

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

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <TransitionPanel view={transportView} events={state.timeline} />
            <AmbulancePanel ambulanceView={ambulanceView} transportId={DEMO_TRANSPORT_ID} />
          </div>

          <Controls
            transportId={DEMO_TRANSPORT_ID}
            inFlight={state.inFlight}
            manualMode={manualMode}
            onManualModeChange={setManualMode}
          />

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <AiPanel transportId={DEMO_TRANSPORT_ID} />
            <FuzzPanel fuzzResult={state.fuzz} />
          </div>

          <Timeline events={state.timeline} />
        </section>

        <section className="space-y-4">
          <SectionHeading
            title="Multi-hospital capacity"
            subtitle="Six hospitals with real bed capacity, many concurrent transports, and hospitals that can decline."
          />

          <GlobalInvariantBadge refreshKey={refreshKey} />
          <MultiHospitalControls onChanged={bumpRefresh} />
          <HospitalsPanel hospitals={state.hospitals} />

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <RegionMap hospitals={state.hospitals} ambulances={state.ambulances} />
            <AlertsPanel alerts={state.alerts} />
          </div>

          <TransportsTable rows={state.transportRows} />
        </section>
      </main>
    </div>
  );
}

function SectionHeading({ title, subtitle }) {
  return (
    <div className="space-y-1 border-b border-border pb-3">
      <h2 className="text-base font-semibold tracking-tight">{title}</h2>
      <p className="text-sm text-muted-foreground">{subtitle}</p>
    </div>
  );
}
