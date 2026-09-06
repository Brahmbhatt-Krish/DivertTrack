// One row per event; StaleIgnored/DuplicateIgnored/CutoverCancelled/
// RedirectQueued/BoundExceeded are highlighted since they're the whole
// point of the demo (a message got discarded or delayed, and the system
// stayed safe anyway). Send->arrival delay is derived by pairing each
// CommandSent with the MessageDelivered that shares its command_id.
import { memo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { eventText } from "@/lib/eventText.js";

const HIGHLIGHT_LABELS = {
  StaleIgnored: "STALE — IGNORED",
  DuplicateIgnored: "DUPLICATE — IGNORED",
  CutoverCancelled: "CUTOVER — CANCELLED",
  RedirectQueued: "REDIRECT — QUEUED",
  BoundExceeded: "BOUND — EXCEEDED",
};

// The reducer keeps 500 events; rendering all of them meant 500 rows (each
// JSON.stringify-ing its payload twice) re-rendering on every update, which
// on its own could lock the tab up during a burst. The scroll box only ever
// shows the newest handful anyway.
const MAX_VISIBLE_ROWS = 60;

function Timeline({ events }) {
  const deliveredAt = {};
  for (const event of events) {
    if (event.type === "MessageDelivered") deliveredAt[event.payload.command_id] = event.ts_ms;
  }

  const rows = events.slice(-MAX_VISIBLE_ROWS).reverse();

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b border-border px-4 py-3">
        <div className="flex items-baseline justify-between">
          <CardTitle className="text-sm">Timeline</CardTitle>
          <span className="text-xs text-muted-foreground">newest first · last {MAX_VISIBLE_ROWS}</span>
        </div>
      </CardHeader>

      <CardContent className="px-0">
        <div className="max-h-96 overflow-y-auto">
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-muted/60 backdrop-blur">
              <TableRow className="hover:bg-transparent">
                <TableHead className="h-9 w-24 text-xs">t (ms)</TableHead>
                <TableHead className="h-9 text-xs">event</TableHead>
                <TableHead className="h-9 w-36 text-xs">facility</TableHead>
                <TableHead className="h-9 text-xs">detail</TableHead>
                <TableHead className="h-9 w-20 text-right text-xs">delay</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((event) => {
                const label = HIGHLIGHT_LABELS[event.type];
                // Readable sentence in the column, raw payload on hover —
                // the precision is still one mouse-over away.
                const detail = eventText(event);
                const raw = JSON.stringify(event.payload);
                const delay =
                  event.type === "CommandSent" && event.payload.command_id in deliveredAt
                    ? deliveredAt[event.payload.command_id] - event.ts_ms
                    : null;
                return (
                  <TableRow key={event.seq} className={cn("text-xs", label && "bg-warning-soft hover:bg-warning-soft")}>
                    <TableCell className="py-1.5 font-mono text-muted-foreground">{event.ts_ms}</TableCell>
                    <TableCell className={cn("py-1.5 font-medium", label ? "text-warning" : "text-foreground")}>
                      {label ?? event.type}
                    </TableCell>
                    <TableCell className="py-1.5 font-mono text-muted-foreground">{event.facility_id ?? "—"}</TableCell>
                    <TableCell className="py-1.5 text-muted-foreground" title={raw}>
                      {detail}
                    </TableCell>
                    <TableCell className="py-1.5 text-right font-mono text-muted-foreground">
                      {delay != null ? `${delay}ms` : "—"}
                    </TableCell>
                  </TableRow>
                );
              })}
              {rows.length === 0 && (
                <TableRow className="hover:bg-transparent">
                  <TableCell colSpan={5} className="py-8 text-center text-sm text-muted-foreground">
                    No events yet.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}

export default memo(Timeline);
