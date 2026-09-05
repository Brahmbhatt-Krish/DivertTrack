// Phase 18: a simple SVG of the 40x40 km grid — hospital markers sized by
// load, ambulance markers at their live position. Both come from the shared
// reducer state (hospitals via the "hospital" WS kind, positions via
// "ambulance"), so this polls nothing: an earlier version fetched
// GET /transports every 2s, which was a meaningful share of the load that
// made the UI unresponsive.
import { memo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

const GRID_KM = 40;
const SIZE_PX = 360;

function toPx(coord) {
  return (coord / GRID_KM) * SIZE_PX;
}

// Transport ids are "BATCH-<patient id>-<index>" for anything started from
// the batch controls, which is too long to sit next to a 3.5px dot. The
// prefix is the only part shared by every marker, so dropping it is what
// makes the labels distinguishable at this size; the full id stays reachable
// as a tooltip, and still matches the Transports table exactly.
function shortLabel(transportId) {
  return transportId.replace(/^BATCH-/, "");
}

function RegionMap({ hospitals, ambulances }) {
  const hospitalList = Object.values(hospitals || {});
  const ambulanceList = Object.entries(ambulances || {}).filter(([, a]) => a?.position);

  return (
    <Card className="gap-0 py-4">
      <CardHeader className="px-4 pb-3">
        <div className="flex items-baseline justify-between">
          <CardTitle className="text-sm">Region map</CardTitle>
          <span className="text-xs text-muted-foreground">40 × 40 km</span>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col items-center px-4">
      {/* The map is square, so its height follows its width — left-aligned at
          full card width it both left a dead column beside it and made this
          the tallest card on the page. Centred and capped instead. */}
      <svg
        viewBox={`0 0 ${SIZE_PX} ${SIZE_PX}`}
        className="aspect-square w-full max-w-[320px] rounded-lg border border-border bg-muted/30"
      >
        {hospitalList.map((h) => {
          const [x, y] = h.location || [0, 0];
          const r = 6 + (h.load || 0) * 14;
          return (
            <g key={h.id}>
              <circle
                cx={toPx(x)}
                cy={toPx(y)}
                r={r}
                fill={h.diversion === "FULL" ? "#b91c1c" : h.diversion === "PARTIAL" ? "#b45309" : "#171717"}
                opacity={h.diversion === "OPEN" ? 0.85 : 0.9}
              />
              <text x={toPx(x)} y={toPx(y) - r - 4} fontSize="8" textAnchor="middle" fill="#737373">
                {h.id}
              </text>
            </g>
          );
        })}
        {ambulanceList.map(([transportId, ambulance]) => {
          const cx = toPx(ambulance.position[0]);
          const cy = toPx(ambulance.position[1]);
          return (
            <g key={transportId}>
              <title>{`${transportId}${ambulance.known_destination ? ` -> ${ambulance.known_destination}` : ""}`}</title>
              <circle cx={cx} cy={cy} r={3.5} fill="#15803d" stroke="#ffffff" strokeWidth={1} />
              {/* Drawn twice: a thick white stroke underneath, then the fill
                  on top, so the label stays readable wherever it crosses a
                  hospital marker or the grid background. */}
              <text
                x={cx}
                y={cy + 11}
                fontSize="7.5"
                textAnchor="middle"
                stroke="#ffffff"
                strokeWidth={2.5}
                strokeLinejoin="round"
                paintOrder="stroke"
                fill="#15803d"
                fontWeight="600"
              >
                {shortLabel(transportId)}
              </text>
            </g>
          );
        })}
      </svg>

      <div className="mt-3 flex flex-wrap items-center justify-center gap-x-3.5 gap-y-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="size-2 rounded-full bg-primary" /> open
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="size-2 rounded-full bg-warning" /> partial diversion
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="size-2 rounded-full bg-danger" /> full diversion
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="size-2 rounded-full bg-success" /> ambulance
        </span>
        <span className="text-muted-foreground/70">marker size = load</span>
      </div>
      </CardContent>
    </Card>
  );
}

export default memo(RegionMap);
