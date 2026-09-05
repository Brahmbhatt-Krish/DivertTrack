// Phase 18: one row per capacity-aware transport — which ambulance is
// heading to which hospital, and (on click) the full ranked shortlist it was
// chosen from.
//
// Rows arrive live as the hub's "transport_list" message. This panel used to
// fetch on mount and on a manual Refresh button only, which meant the
// ambulance -> hospital mapping went stale on every redirect until you
// clicked; GET /transports is now just the initial seed.
import { Fragment, memo, useEffect, useState } from "react";
import { ChevronRight } from "lucide-react";
import { api } from "../api.js";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

const STATUS_STYLES = {
  STABLE: "border-success-border bg-success-soft text-success",
  NO_ACCEPTING_FACILITY: "border-danger-border bg-danger-soft text-danger",
  NEEDS_REPLAN: "border-warning-border bg-warning-soft text-warning",
};

// Arrival isn't a DispatcherStatus — the journey is over, it isn't another
// handoff state — so a landed transport still reports STABLE. Showing
// arrived_at as its own badge is what makes "it got there" visible.
const ARRIVED_STYLE = "border-primary/30 bg-primary/10 text-foreground";

// GET /transports/{id}/candidates — every hospital the dispatcher weighed for
// this patient, best score first, with a reason on the ones it ruled out.
// Fetched on demand rather than pushed: it's a what-if ranking recomputed
// against current capacity, not part of the transport's state.
function CandidateList({ transportId }) {
  const [candidates, setCandidates] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setCandidates(null);
    setError(null);
    api
      .candidates(transportId)
      .then((list) => !cancelled && setCandidates(list))
      .catch((err) => !cancelled && setError(err.message));
    return () => {
      cancelled = true;
    };
  }, [transportId, reloadKey]);

  // A hospital id forces that specific one, which the dispatcher will still
  // refuse with 409 if it fails the acceptance checks.
  async function sendTo(target) {
    setBusy(true);
    setError(null);
    try {
      await api.redirectCapacityAware(transportId, target);
      setReloadKey((key) => key + 1);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  // Re-plan means "use the ranking you're looking at", but *how* depends on
  // the policy: only AUTO lets a redirect omit the target and have the
  // dispatcher choose. Under MANUAL that request is rejected outright, so
  // here the operator sends it to the top-ranked hospital explicitly — same
  // destination, but an operator decision, which is what MANUAL means.
  async function replan() {
    setBusy(true);
    setError(null);
    try {
      const { policy } = await api.getPolicy();
      if (policy === "auto") {
        await api.redirectCapacityAware(transportId, null);
      } else {
        const top = (candidates ?? []).find((c) => c.score !== null);
        if (!top) throw new Error("No hospital currently accepts this patient.");
        await api.redirectCapacityAware(transportId, top.hospital_id);
      }
      setReloadKey((key) => key + 1);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (candidates === null && !error)
    return <p className="px-4 py-3 text-xs text-muted-foreground">Ranking…</p>;
  if (candidates && candidates.length === 0)
    return <p className="px-4 py-3 text-xs text-muted-foreground">No candidates — this transport has no patient record.</p>;

  const best = (candidates ?? []).find((c) => c.score !== null);

  return (
    <div className="space-y-1.5 px-4 py-3">
      {error && (
        <p className="rounded-md border border-danger-border bg-danger-soft px-2 py-1.5 text-xs text-danger">{error}</p>
      )}
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          Ranked shortlist — higher score wins (distance traded off against load).
        </p>
        <Button
          variant="outline"
          size="xs"
          disabled={busy || !best}
          title="Send to the top-ranked hospital (under auto policy, let the dispatcher choose)"
          onClick={replan}
        >
          Re-plan
        </Button>
      </div>
      {(candidates ?? []).map((candidate) => {
        const accepted = candidate.score !== null;
        return (
          <div key={candidate.hospital_id} className="flex items-center gap-2 text-xs">
            <span className="w-4 shrink-0 text-center text-muted-foreground">
              {candidate === best ? "★" : accepted ? "" : "✕"}
            </span>
            <span className={cn("w-24 shrink-0 font-mono", !accepted && "text-muted-foreground line-through")}>
              {candidate.hospital_id}
            </span>
            {accepted ? (
              <span className="font-mono text-muted-foreground">{candidate.score.toFixed(2)}</span>
            ) : (
              <Badge variant="outline" className="border-danger-border bg-danger-soft font-normal text-danger">
                {candidate.reason}
              </Badge>
            )}
            {accepted && (
              <Button
                variant="ghost"
                size="xs"
                disabled={busy}
                className="ml-auto"
                onClick={() => sendTo(candidate.hospital_id)}
              >
                Send here
              </Button>
            )}
          </div>
        );
      })}
    </div>
  );
}

function TransportsTable({ rows, onFocus }) {
  const [seed, setSeed] = useState([]);
  const [expanded, setExpanded] = useState(null);

  // The hub only pushes when something changes, so a page opened mid-run
  // would otherwise show nothing until the next event.
  useEffect(() => {
    api
      .listTransports()
      .then(setSeed)
      .catch(() => {});
  }, []);

  // Any live push supersedes the seed wholesale — including the empty list
  // sent on /demo/reset, which is why this can't fall back to `seed` when
  // rows is empty.
  const list = rows ?? seed;

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b border-border px-4 py-3">
        <CardTitle className="text-sm">
          Transports
          {list.length > 0 && <span className="ml-2 font-normal text-muted-foreground">{list.length}</span>}
        </CardTitle>
      </CardHeader>

      <CardContent className="px-0">
        <div className="max-h-80 overflow-y-auto">
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-muted/60 backdrop-blur">
              <TableRow className="hover:bg-transparent">
                <TableHead className="h-9 w-6 text-xs" />
                <TableHead className="h-9 text-xs">id</TableHead>
                <TableHead className="h-9 text-xs">status</TableHead>
                <TableHead className="h-9 text-xs">current</TableHead>
                <TableHead className="h-9 text-xs">pending</TableHead>
                <TableHead className="h-9 w-16 text-xs">epoch</TableHead>
                <TableHead className="h-9 w-28 text-xs">position</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {list.length === 0 && (
                <TableRow className="hover:bg-transparent">
                  <TableCell colSpan={7} className="py-8 text-center text-sm text-muted-foreground">
                    No capacity-aware transports yet — start one, or run a scenario preset.
                  </TableCell>
                </TableRow>
              )}
              {list.map((row) => {
                const isOpen = expanded === row.transport_id;
                return (
                  <Fragment key={row.transport_id}>
                    <TableRow
                      className="cursor-pointer text-xs"
                      onClick={() => {
                        setExpanded(isOpen ? null : row.transport_id);
                        onFocus?.(row.transport_id);
                      }}
                    >
                      <TableCell className="py-1.5 pr-0">
                        <ChevronRight className={cn("size-3 text-muted-foreground transition-transform", isOpen && "rotate-90")} />
                      </TableCell>
                      <TableCell className="py-1.5 font-mono font-medium">{row.transport_id}</TableCell>
                      <TableCell className="py-1.5">
                        {row.arrived_at ? (
                          <Badge variant="outline" className={cn("font-normal", ARRIVED_STYLE)}>
                            ARRIVED
                          </Badge>
                        ) : (
                          <Badge variant="outline" className={cn("font-normal", STATUS_STYLES[row.status])}>
                            {row.status}
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="py-1.5 font-mono">
                        {row.arrived_at ? (
                          <span title={`Arrived at ${row.arrived_at}`}>✓ {row.arrived_at}</span>
                        ) : (
                          row.current_destination ?? "—"
                        )}
                      </TableCell>
                      <TableCell className="py-1.5 font-mono text-muted-foreground">
                        {row.pending_destination ?? "—"}
                      </TableCell>
                      <TableCell className="py-1.5 font-mono text-muted-foreground">{row.current_epoch}</TableCell>
                      <TableCell className="py-1.5 font-mono text-muted-foreground">
                        {row.position ? `${row.position[0].toFixed(1)}, ${row.position[1].toFixed(1)}` : "—"}
                      </TableCell>
                    </TableRow>
                    {isOpen && (
                      <TableRow className="hover:bg-transparent">
                        <TableCell colSpan={7} className="bg-muted/30 p-0">
                          <CandidateList transportId={row.transport_id} />
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}

export default memo(TransportsTable);
