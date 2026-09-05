// Phase 18: "InvariantBadge.jsx becomes global" — kept as a separate small
// component rather than rewriting the existing per-transport badge (still
// used by the original single-demo view).
//
// GET /invariant runs I1-I4 over the whole log, so this deliberately does
// not poll: it fetches on mount, whenever the page's refreshKey changes (a
// batch start, a preset, a policy switch), and when you ask it to. An
// earlier version refetched every 3s and was a large part of what made the
// app unresponsive under load.
import { memo, useCallback, useEffect, useState } from "react";
import { CheckCircle2, RefreshCw, TriangleAlert, XCircle } from "lucide-react";
import { api } from "../api.js";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

function Check({ label, ok }) {
  return (
    <div className="flex items-center gap-2">
      {ok ? <CheckCircle2 className="size-4 text-success" /> : <XCircle className="size-4 text-danger" />}
      <span className="text-sm">
        {label} — <span className={cn("font-semibold", ok ? "text-success" : "text-danger")}>{ok ? "PASS" : "FAIL"}</span>
      </span>
    </div>
  );
}

function GlobalInvariantBadge({ refreshKey }) {
  const [result, setResult] = useState(null);
  const [checking, setChecking] = useState(false);

  const refresh = useCallback(() => {
    setChecking(true);
    api
      .globalInvariant()
      .then(setResult)
      .catch(() => {})
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh, refreshKey]);

  const passed = result ? result.passed : true;
  const overbooked = Boolean(result?.violations?.some((v) => v.kind === "OVERBOOKED"));
  const warnings = result?.capacity_warnings ?? [];

  return (
    <div
      className={cn(
        "rounded-xl border px-5 py-4",
        passed ? "border-success-border bg-success-soft" : "border-danger-border bg-danger-soft",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-1.5">
          <Check label="Exactly one active, all transports" ok={passed} />
          <Check label="No hospital overbooked" ok={!overbooked} />
        </div>
        <Button variant="outline" size="xs" onClick={refresh} disabled={checking} className="bg-background">
          <RefreshCw className={cn("size-3", checking && "animate-spin")} />
          {checking ? "checking" : "re-check"}
        </Button>
      </div>

      {warnings.length > 0 && (
        <ul className="mt-3 space-y-1 border-t border-warning-border/60 pt-2">
          {warnings.map((warning) => (
            <li key={warning} className="flex items-center gap-2 font-mono text-xs text-warning">
              <TriangleAlert className="size-3.5 shrink-0" />
              {warning}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default memo(GlobalInvariantBadge);
