// Start, redirect, the seven presets (CHAOS styled as the "chaos toggle"),
// Reset, in-flight Hold/Release, and manual-mode Confirm buttons.
import { useState } from "react";
import { api } from "../api.js";

const FACILITIES = ["Hospital_A", "Hospital_B", "Hospital_C"];
const PRESETS = [
  "NORMAL",
  "DELAYED_OLD_FACILITY",
  "OUT_OF_ORDER_ACK",
  "REDIRECT_BEFORE_CUTOVER",
  "LATE_REDIRECT_QUEUED",
  "DUPLICATE_PACKET",
  "CHAOS",
];

export default function Controls({ transportId, inFlight, manualMode, onManualModeChange }) {
  const [redirectTarget, setRedirectTarget] = useState(FACILITIES[1]);
  const [error, setError] = useState(null);

  function run(action) {
    return async (...args) => {
      try {
        setError(null);
        await action(...args);
      } catch (err) {
        setError(err.message);
      }
    };
  }

  const handleStart = run(() => api.startTransport(transportId, FACILITIES[0], manualMode));
  const handleRedirect = run(() => api.redirect(transportId, redirectTarget));
  const handlePreset = run((name) => api.runPreset(name));
  const handleReset = run(() => api.reset());
  const handleHold = run((commandId) => api.hold(commandId));
  const handleRelease = run((commandId) => api.release(commandId));
  const handleConfirm = run((endpointId) => api.confirm(endpointId));

  return (
    <div className="space-y-4 rounded-lg border border-slate-200 bg-white p-4">
      {error && <p className="rounded bg-red-50 px-2 py-1 text-xs text-red-700">{error}</p>}

      <div className="flex flex-wrap items-center gap-2">
        <button
          className="rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700"
          onClick={handleStart}
        >
          Start
        </button>
        <select
          className="rounded border border-slate-300 px-2 py-1 text-sm"
          value={redirectTarget}
          onChange={(e) => setRedirectTarget(e.target.value)}
        >
          {FACILITIES.map((f) => (
            <option key={f} value={f}>
              {f}
            </option>
          ))}
        </select>
        <button
          className="rounded border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          onClick={handleRedirect}
        >
          Redirect
        </button>
        <button
          className="rounded border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          onClick={handleReset}
        >
          Reset
        </button>
        <label className="ml-auto flex items-center gap-1.5 text-xs text-slate-600">
          <input
            type="checkbox"
            checked={manualMode}
            onChange={(e) => onManualModeChange(e.target.checked)}
          />
          Manual mode
        </label>
      </div>

      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Presets</h4>
        <div className="mt-1 flex flex-wrap gap-2">
          {PRESETS.map((name) => (
            <button
              key={name}
              onClick={() => handlePreset(name)}
              className={
                name === "CHAOS"
                  ? "rounded border border-red-300 bg-red-50 px-2 py-1 text-xs font-semibold text-red-700"
                  : "rounded border border-slate-300 px-2 py-1 text-xs text-slate-700"
              }
            >
              {name}
            </button>
          ))}
        </div>
      </div>

      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">In-flight</h4>
        <ul className="mt-1 space-y-1 text-xs">
          {inFlight.length === 0 && <li className="text-slate-400">none</li>}
          {inFlight.map((message) => (
            <li key={message.command_id} className="flex items-center justify-between gap-2">
              <span className="truncate">
                {message.command_id} · {message.label}
                {message.held ? " (held)" : ""}
              </span>
              {message.held ? (
                <button className="text-emerald-600 hover:underline" onClick={() => handleRelease(message.command_id)}>
                  Release
                </button>
              ) : (
                <button className="text-amber-600 hover:underline" onClick={() => handleHold(message.command_id)}>
                  Hold
                </button>
              )}
            </li>
          ))}
        </ul>
      </div>

      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Confirm (manual mode)</h4>
        <div className="mt-1 flex flex-wrap gap-2">
          {[...FACILITIES, transportId].map((endpointId) => (
            <button
              key={endpointId}
              className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700"
              onClick={() => handleConfirm(endpointId)}
            >
              {endpointId}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
