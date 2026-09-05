// One row per event; StaleIgnored/DuplicateIgnored/CutoverCancelled/
// RedirectQueued/BoundExceeded are highlighted since they're the whole
// point of the demo (a message got discarded or delayed, and the system
// stayed safe anyway). Send->arrival delay is derived by pairing each
// CommandSent with the MessageDelivered that shares its command_id.
const HIGHLIGHT_LABELS = {
  StaleIgnored: "STALE — IGNORED",
  DuplicateIgnored: "DUPLICATE — IGNORED",
  CutoverCancelled: "CUTOVER — CANCELLED",
  RedirectQueued: "REDIRECT — QUEUED",
  BoundExceeded: "BOUND — EXCEEDED",
};

export default function Timeline({ events }) {
  const deliveredAt = {};
  for (const event of events) {
    if (event.type === "MessageDelivered") deliveredAt[event.payload.command_id] = event.ts_ms;
  }

  const rows = events.slice().reverse();

  return (
    <div className="rounded-lg border border-slate-200 bg-white">
      <h3 className="border-b border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700">Timeline</h3>
      <div className="max-h-96 overflow-y-auto">
        <table className="w-full text-left text-xs">
          <thead className="sticky top-0 bg-slate-50 text-slate-500">
            <tr>
              <th className="p-2 font-medium">t (ms)</th>
              <th className="p-2 font-medium">event</th>
              <th className="p-2 font-medium">facility</th>
              <th className="p-2 font-medium">detail</th>
              <th className="p-2 font-medium">delay</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((event) => {
              const label = HIGHLIGHT_LABELS[event.type];
              const delay =
                event.type === "CommandSent" && event.payload.command_id in deliveredAt
                  ? deliveredAt[event.payload.command_id] - event.ts_ms
                  : null;
              return (
                <tr key={event.seq} className={label ? "bg-amber-50" : "odd:bg-slate-50/50"}>
                  <td className="p-2 tabular-nums text-slate-500">{event.ts_ms}</td>
                  <td className={`p-2 font-medium ${label ? "text-amber-700" : "text-slate-800"}`}>
                    {label ?? event.type}
                  </td>
                  <td className="p-2 text-slate-600">{event.facility_id ?? "—"}</td>
                  <td className="max-w-xs truncate p-2 text-slate-500" title={JSON.stringify(event.payload)}>
                    {JSON.stringify(event.payload)}
                  </td>
                  <td className="p-2 tabular-nums text-slate-500">{delay != null ? `${delay}ms` : "—"}</td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={5} className="p-4 text-center text-slate-400">
                  No events yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
