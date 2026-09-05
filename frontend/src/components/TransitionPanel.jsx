// Pending destination, a live countdown to cutover_at, and ticks for the
// four things R3-R5 wait on: READY, the ambulance's notice-applied,
// ACTIVATE's RECEIVED, and WithdrawSent.
import { useEffect, useRef, useState } from "react";

export default function TransitionPanel({ view, events }) {
  const [, forceTick] = useState(0);
  const anchors = useRef({}); // epoch -> {cutoverAt, tsMs, wallClockAtSchedule}

  useEffect(() => {
    const id = setInterval(() => forceTick((n) => n + 1), 200);
    return () => clearInterval(id);
  }, []);

  if (!view || view.pending_destination == null) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-400">
        No transition in progress.
      </div>
    );
  }

  const epoch = view.pending_epoch;
  const epochEvents = events.filter((e) => e.epoch === epoch);
  const scheduled = epochEvents.find((e) => e.type === "CutoverScheduled");

  if (scheduled && !anchors.current[epoch]) {
    anchors.current[epoch] = {
      cutoverAt: scheduled.payload.cutover_at,
      tsMs: scheduled.ts_ms,
      wallClockAtSchedule: Date.now(),
    };
  }

  let remainingMs = null;
  const anchor = anchors.current[epoch];
  if (anchor) {
    const totalMs = anchor.cutoverAt - anchor.tsMs;
    const elapsedMs = Date.now() - anchor.wallClockAtSchedule;
    remainingMs = Math.max(0, Math.round(totalMs - elapsedMs));
  }

  const ready = epochEvents.some((e) => e.type === "AckReceived" && e.payload.ack_type === "READY");
  // The ambulance's own APPLIED ack is tagged with facility_id === the
  // transport itself (see ambulance.py's _build_ack) — that's what tells it
  // apart from a facility's APPLIED for ACTIVATE_AT.
  const noticeApplied = epochEvents.some(
    (e) => e.type === "AckReceived" && e.payload.ack_type === "APPLIED" && e.facility_id === view.transport_id,
  );
  const activateReceived = epochEvents.some((e) => e.type === "AckReceived" && e.payload.ack_type === "RECEIVED");
  const withdrawSent = epochEvents.some((e) => e.type === "WithdrawSent");

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-slate-900">
          Transitioning to <span className="text-sky-700">{view.pending_destination}</span>
        </h3>
        <span className="text-xs text-slate-500">{view.status}</span>
      </div>
      {remainingMs != null && (
        <p className="mt-1 text-sm text-slate-600">
          Cutover in <span className="font-mono font-semibold">{remainingMs}</span> ms
        </p>
      )}
      <ul className="mt-3 grid grid-cols-2 gap-1 text-sm">
        <Tick label="READY" done={ready} />
        <Tick label="Ambulance notice applied" done={noticeApplied} />
        <Tick label="ACTIVATE received" done={activateReceived} />
        <Tick label="WITHDRAW sent" done={withdrawSent} />
      </ul>
    </div>
  );
}

function Tick({ label, done }) {
  return (
    <li className={`flex items-center gap-1.5 ${done ? "text-emerald-600" : "text-slate-400"}`}>
      <span>{done ? "✓" : "○"}</span>
      {label}
    </li>
  );
}
