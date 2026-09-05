import { useState } from "react";
import { api } from "../api.js";

export default function FuzzPanel({ fuzzResult }) {
  const [runs, setRuns] = useState(20);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function handleRun() {
    setBusy(true);
    setError(null);
    try {
      await api.fuzz(runs);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <h3 className="font-semibold text-slate-900">Fuzz</h3>
      <div className="mt-2 flex items-center gap-2">
        <input
          type="number"
          min={1}
          max={500}
          value={runs}
          onChange={(e) => setRuns(Number(e.target.value))}
          className="w-20 rounded border border-slate-300 px-2 py-1 text-sm"
        />
        <button
          disabled={busy}
          onClick={handleRun}
          className="rounded border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          {busy ? "Running…" : "Run"}
        </button>
      </div>
      {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
      {fuzzResult && (
        <p className="mt-2 text-xs text-slate-600">
          <span className={fuzzResult.failed === 0 ? "font-semibold text-emerald-600" : "font-semibold text-red-600"}>
            {fuzzResult.passed}/{fuzzResult.runs} passed
          </span>{" "}
          · max overlap {fuzzResult.max_local_overlap_ms}ms
        </p>
      )}
    </div>
  );
}
