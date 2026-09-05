// Progress, known destination, and a Confirm button (for manual_confirm).
import { api } from "../api.js";

export default function AmbulancePanel({ ambulanceView, transportId }) {
  const progress = ambulanceView?.progress ?? 0;
  const knownDestination = ambulanceView?.known_destination ?? "—";

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-slate-900">Ambulance {transportId}</h3>
        <button
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
          onClick={() => api.confirm(transportId)}
        >
          Confirm
        </button>
      </div>
      <p className="mt-1 text-xs text-slate-500">Known destination: {knownDestination}</p>
      <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          className="h-2 rounded-full bg-sky-500 transition-all"
          style={{ width: `${Math.round(progress * 100)}%` }}
        />
      </div>
    </div>
  );
}
