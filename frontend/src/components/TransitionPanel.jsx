// Pending destination, a live countdown to cutover_at, and ticks for the
// four things R3-R5 wait on: READY, the ambulance's notice-applied,
// ACTIVATE's RECEIVED, and WithdrawSent.
import { useEffect, useRef, useState } from "react";
import { ArrowRight, Check, Circle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export default function TransitionPanel({ view, events }) {
  const [, forceTick] = useState(0);
  const anchors = useRef({}); // epoch -> {cutoverAt, tsMs, wallClockAtSchedule}

  useEffect(() => {
    const id = setInterval(() => forceTick((n) => n + 1), 200);
    return () => clearInterval(id);
  }, []);

  if (!view || view.pending_destination == null) {
    return (
      <Card className="py-0">
        <CardContent className="flex items-center gap-2 px-4 py-3.5 text-sm text-muted-foreground">
          <Circle className="size-3.5" />
          No transition in progress.
        </CardContent>
      </Card>
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
    <Card className="gap-0 py-4">
      <CardHeader className="px-4 pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <span className="text-muted-foreground">Transitioning</span>
            <ArrowRight className="size-3.5 text-muted-foreground" />
            <span className="font-mono">{view.pending_destination}</span>
          </CardTitle>
          <div className="flex items-center gap-2">
            {remainingMs != null && (
              <span className="text-xs text-muted-foreground">
                cutover in <span className="font-mono font-semibold text-foreground">{remainingMs}</span>ms
              </span>
            )}
            <Badge variant="secondary">{view.status}</Badge>
          </div>
        </div>
      </CardHeader>

      <CardContent className="grid grid-cols-1 gap-2 px-4 sm:grid-cols-2">
        <Tick label="READY" done={ready} />
        <Tick label="Ambulance notice applied" done={noticeApplied} />
        <Tick label="ACTIVATE received" done={activateReceived} />
        <Tick label="WITHDRAW sent" done={withdrawSent} />
      </CardContent>
    </Card>
  );
}

function Tick({ label, done }) {
  return (
    <div className={cn("flex items-center gap-2 text-sm", done ? "text-foreground" : "text-muted-foreground")}>
      <span
        className={cn(
          "flex size-4 items-center justify-center rounded-full border",
          done ? "border-success bg-success text-white" : "border-border",
        )}
      >
        {done && <Check className="size-3" strokeWidth={3} />}
      </span>
      {label}
    </div>
  );
}
