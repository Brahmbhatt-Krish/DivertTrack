// Large, unmissable PASS/FAIL — the whole point of the demo. No result yet
// (nothing has happened) reads as PASS: there's nothing to violate yet.
export default function InvariantBadge({ result }) {
  const passed = result ? result.passed : true;

  return (
    <div
      className={`flex items-center justify-between rounded-xl px-6 py-5 text-3xl font-extrabold tracking-wide ${
        passed ? "bg-emerald-100 text-emerald-700" : "bg-red-100 text-red-700"
      }`}
    >
      <span>{passed ? "PASS" : "FAIL"}</span>
      {result && (
        <span className="text-sm font-normal text-slate-500">
          {result.transitions_checked} instant{result.transitions_checked === 1 ? "" : "s"} checked
          {result.max_local_overlap_ms > 0 && ` · max overlap ${result.max_local_overlap_ms}ms`}
        </span>
      )}
    </div>
  );
}
