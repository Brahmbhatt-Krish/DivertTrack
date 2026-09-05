// State badge, epoch, STALE badge, and the last accepted / last rejected
// command — all derived here from the shared timeline, not fetched.
const STATE_STYLES = {
  IDLE: "bg-slate-100 text-slate-600",
  ARMED: "bg-amber-100 text-amber-700",
  ACTIVE: "bg-emerald-100 text-emerald-700",
  WITHDRAWN: "bg-slate-200 text-slate-500",
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
    <div className={`rounded-lg border border-slate-200 bg-white p-4 ${large ? "text-lg" : ""}`}>
      <div className="flex items-center justify-between">
        <h3 className={`font-semibold text-slate-900 ${large ? "text-2xl" : ""}`}>{facilityId}</h3>
        {stale && (
          <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-bold tracking-wide text-amber-700">
            STALE
          </span>
        )}
      </div>
      <div className="mt-2 flex items-center gap-2">
        <span className={`rounded px-2 py-0.5 text-xs font-semibold ${STATE_STYLES[state] ?? "bg-slate-100"}`}>
          {state}
        </span>
        <span className="text-xs text-slate-500">epoch {epoch}</span>
      </div>
      <dl className="mt-3 space-y-1 text-xs text-slate-600">
        <div>
          <dt className="inline font-medium text-slate-700">Last accepted: </dt>
          <dd className="inline">{describe(lastAccepted)}</dd>
        </div>
        <div>
          <dt className="inline font-medium text-slate-700">Last rejected: </dt>
          <dd className="inline">{describe(lastRejected)}</dd>
        </div>
      </dl>
    </div>
  );
}
