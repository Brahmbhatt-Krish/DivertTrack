// Phase 18: NoAcceptingFacility, CapacityRebalance, AutoRedirect — surfaced
// as they arrive over the "alert" WS kind (see ws.py's _ALERT_EVENT_TYPES).
import { memo } from "react";
import { Bell, TriangleAlert } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

function AlertsPanel({ alerts }) {
  const recent = [...alerts].slice(-20).reverse();

  return (
    <Card className="gap-0 py-4">
      <CardHeader className="px-4 pb-3">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Bell className="size-4 text-muted-foreground" />
          Alerts
          {recent.length > 0 && <span className="font-normal text-muted-foreground">{recent.length}</span>}
        </CardTitle>
      </CardHeader>

      {/* Capped and scrolled, like Timeline and Transports. Uncapped, this grew
          with every alert until it was the tallest thing on the page — and it
          shares a grid row with the map, so it dragged that row with it. */}
      <CardContent className="max-h-[352px] overflow-y-auto px-4">
        {recent.length === 0 ? (
          <p className="text-xs text-muted-foreground">No alerts yet.</p>
        ) : (
          <ul className="space-y-1.5">
            {recent.map((alert, index) => (
              <li
                key={index}
                className="flex items-start gap-2 rounded-md border border-warning-border bg-warning-soft px-3 py-2"
              >
                <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" />
                <div className="min-w-0 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-warning">{alert.type}</span>
                    {alert.transport_id && (
                      <Badge variant="outline" className="border-warning-border font-mono font-normal text-warning">
                        {alert.transport_id}
                      </Badge>
                    )}
                  </div>
                  <p className="mt-0.5 truncate font-mono text-warning/80" title={JSON.stringify(alert.payload)}>
                    {JSON.stringify(alert.payload)}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

export default memo(AlertsPanel);
