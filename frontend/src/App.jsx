// One page, ?role= filters the view: dashboard (default) | hospital_A |
// hospital_B | hospital_C | ambulance.
import { useMemo, useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
import AmbulanceFleet from "./components/AmbulanceFleet.jsx";
import MultiHospitalControls from "./components/MultiHospitalControls.jsx";
import GlobalInvariantBadge from "./components/GlobalInvariantBadge.jsx";
import AmbulanceScreen from "./screens/AmbulanceScreen.jsx";
import HospitalScreen from "./screens/HospitalScreen.jsx";
import DispatcherScreen from "./screens/DispatcherScreen.jsx";

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

  // Each endpoint gets its own screen showing only its own knowledge. Run
  // them in separate windows and the protocol becomes watchable: the new
  // hospital goes ARMED then ACTIVE, the crew's destination flips, and only
  // *then* does the old hospital stand down — each on its own display, in the
  // order the protocol guarantees rather than the order a narrator claims.
  if (role in ROLE_TO_FACILITY_ID) {
    const facilityId = ROLE_TO_FACILITY_ID[role];
    return (
      <div className="min-h-screen">
        <TopBar connected={state.connected} role={role} />
        <HospitalScreen
          facilityId={facilityId}
          view={state.facilities[facilityKey(facilityId, DEMO_TRANSPORT_ID)]}
          events={state.timeline}
          arrivedAt={transportView?.arrived_at ?? null}
        />
      </div>
    );
  }

  if (role === "ambulance") {
    return (
      <div className="min-h-screen">
        <TopBar connected={state.connected} role={role} />
        <AmbulanceScreen ambulanceView={ambulanceView} transportId={DEMO_TRANSPORT_ID} />
      </div>
    );
  }

  if (role === "dispatcher") {
    return (
      <div className="min-h-screen">
        <TopBar connected={state.connected} role={role} />
        <DispatcherScreen
          state={state}
          transportId={DEMO_TRANSPORT_ID}
          facilityIds={FACILITY_IDS}
          transportView={transportView}
          invariantResult={invariantResult}
          manualMode={manualMode}
          onManualModeChange={setManualMode}
          facilityKey={facilityKey}
        />
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <TopBar connected={state.connected} role={role} />

      {/* Two layers, as tabs rather than one long scroll. Stacked, the
          capacity network sat below the fold and reviewers concluded the
          three-hospital protocol demo was the entire system. A tab cannot be
          scrolled past. */}
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Tabs defaultValue="protocol" className="space-y-6">
          <TabsList>
            <TabsTrigger value="protocol">Handoff protocol</TabsTrigger>
            <TabsTrigger value="capacity">Capacity network</TabsTrigger>
          </TabsList>

        <TabsContent value="protocol" className="space-y-4">
          <SectionHeading
            title="Single transport"
            subtitle={`One ambulance (${DEMO_TRANSPORT_ID}), three facilities, an unreliable network — and exactly one facility active at every instant.`}
          />

          <InvariantBadge result={invariantResult} />

          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            {FACILITY_IDS.map((facilityId) => (
              <FacilityCard
                key={facilityId}
                facilityId={facilityId}
                view={state.facilities[facilityKey(facilityId, DEMO_TRANSPORT_ID)]}
                events={state.timeline}
                arrivedAt={transportView?.arrived_at ?? null}
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
        </TabsContent>

        <TabsContent value="capacity" className="space-y-4">
          <SectionHeading
            title="Multi-hospital capacity"
            subtitle="The same protocol, carrying a second problem: real bed capacity, many concurrent transports, hospitals that decline, and a roster that changes while ambulances are in the air."
          />

          <GlobalInvariantBadge refreshKey={refreshKey} />
          <MultiHospitalControls onChanged={bumpRefresh} />
          <HospitalsPanel hospitals={state.hospitals} />

          {/* No items-start here: these two are meant to be the same height,
              and the map's is the one that sets it (a square capped at 320px).
              The fleet list scrolls inside whatever height the row gives it. */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <RegionMap hospitals={state.hospitals} ambulances={state.ambulances} />
            <AmbulanceFleet ambulances={state.ambulances} transportRows={state.transportRows} />
          </div>

          <AlertsPanel alerts={state.alerts} />

          <TransportsTable rows={state.transportRows} />
        </TabsContent>
        </Tabs>
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
