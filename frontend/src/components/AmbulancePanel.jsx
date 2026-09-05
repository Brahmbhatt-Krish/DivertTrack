// Progress, known destination, and a Confirm button (for manual_confirm).
import { Ambulance } from "lucide-react";
import { api } from "../api.js";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export default function AmbulancePanel({ ambulanceView, transportId }) {
  const progress = ambulanceView?.progress ?? 0;
  const knownDestination = ambulanceView?.known_destination ?? "—";
  const pct = Math.round(progress * 100);

  return (
    <Card className="gap-0 py-4">
      <CardHeader className="px-4 pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Ambulance className="size-4 text-muted-foreground" />
            <span className="font-mono">{transportId}</span>
          </CardTitle>
          <Button variant="outline" size="sm" onClick={() => api.confirm(transportId)}>
            Confirm
          </Button>
        </div>
      </CardHeader>

      <CardContent className="px-4">
        <div className="flex items-baseline justify-between text-xs">
          <span className="text-muted-foreground">
            Known destination <span className="font-mono text-foreground">{knownDestination}</span>
          </span>
          <span className="font-mono text-muted-foreground">{pct}%</span>
        </div>
        <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-muted">
          <div className="h-2 rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
        </div>
      </CardContent>
    </Card>
  );
}
