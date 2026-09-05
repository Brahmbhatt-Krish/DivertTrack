// Large, unmissable PASS/FAIL — the whole point of the demo. No result yet
// (nothing has happened) reads as PASS: there's nothing to violate yet.
import { CheckCircle2, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";

export default function InvariantBadge({ result }) {
  const passed = result ? result.passed : true;
  const Icon = passed ? CheckCircle2 : XCircle;

  return (
    <div
      className={cn(
        "flex items-center justify-between gap-4 rounded-xl border px-5 py-4",
        passed ? "border-success-border bg-success-soft" : "border-danger-border bg-danger-soft",
      )}
    >
      <div className="flex items-center gap-3">
        <Icon className={cn("size-7", passed ? "text-success" : "text-danger")} />
        <div className="leading-tight">
          <div className={cn("text-2xl font-semibold tracking-tight", passed ? "text-success" : "text-danger")}>
            {passed ? "PASS" : "FAIL"}
          </div>
          <div className="text-xs text-muted-foreground">Exactly one facility active, every instant</div>
        </div>
      </div>

      {result && (
        <div className="text-right text-xs text-muted-foreground">
          <div>
            {result.transitions_checked} instant{result.transitions_checked === 1 ? "" : "s"} checked
          </div>
          {result.max_local_overlap_ms > 0 && <div>max overlap {result.max_local_overlap_ms}ms</div>}
        </div>
      )}
    </div>
  );
}
