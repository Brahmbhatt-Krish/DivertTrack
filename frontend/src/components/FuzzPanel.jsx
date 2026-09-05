import { useState } from "react";
import { FlaskConical } from "lucide-react";
import { api } from "../api.js";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

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
    <Card className="gap-0 py-4">
      <CardHeader className="px-4 pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <FlaskConical className="size-4 text-muted-foreground" />
            Fuzz
          </CardTitle>
          <div className="flex items-center gap-2">
            <Input
              type="number"
              min={1}
              max={500}
              value={runs}
              onChange={(e) => setRuns(Number(e.target.value))}
              className="h-8 w-20 text-xs"
            />
            <Button size="sm" variant="outline" disabled={busy} onClick={handleRun}>
              {busy ? "Running…" : "Run"}
            </Button>
          </div>
        </div>
      </CardHeader>

      <CardContent className="px-4">
        {error && <p className="text-xs text-danger">{error}</p>}
        {fuzzResult ? (
          <p className="text-xs text-muted-foreground">
            <span className={cn("font-semibold", fuzzResult.failed === 0 ? "text-success" : "text-danger")}>
              {fuzzResult.passed}/{fuzzResult.runs} passed
            </span>{" "}
            · max overlap {fuzzResult.max_local_overlap_ms}ms
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">
            Runs randomized redirect scenarios on a fake clock and re-checks the invariant.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
