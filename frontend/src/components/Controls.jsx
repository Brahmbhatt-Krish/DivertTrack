// Start, redirect, the seven presets (CHAOS styled as the "chaos toggle"),
// Reset, in-flight Hold/Release, and manual-mode Confirm buttons.
import { useState } from "react";
import { AlertTriangle, Pause, Play, RotateCcw } from "lucide-react";
import { api } from "../api.js";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { cn } from "@/lib/utils";

const FACILITIES = ["Hospital_A", "Hospital_B", "Hospital_C"];
const PRESETS = [
  "NORMAL",
  "DELAYED_OLD_FACILITY",
  "OUT_OF_ORDER_ACK",
  "REDIRECT_BEFORE_CUTOVER",
  "LATE_REDIRECT_QUEUED",
  "DUPLICATE_PACKET",
  "CHAOS",
];

export default function Controls({ transportId, inFlight, manualMode, onManualModeChange }) {
  const [redirectTarget, setRedirectTarget] = useState(FACILITIES[1]);
  const [error, setError] = useState(null);
  // A preset only loads a delay table onto the bus — it does nothing
  // visible by itself. Without some confirmation here, clicking one looks
  // like a no-op until you separately Start/Redirect, which reads as "the
  // presets don't work". Track which one is active and say so explicitly.
  const [activePreset, setActivePreset] = useState(null);

  function run(action) {
    return async (...args) => {
      try {
        setError(null);
        await action(...args);
      } catch (err) {
        setError(err.message);
      }
    };
  }

  const handleStart = run(() => api.startTransport(transportId, FACILITIES[0], manualMode));
  const handleRedirect = run(() => api.redirect(transportId, redirectTarget));
  const handlePreset = run(async (name) => {
    await api.runPreset(name);
    setActivePreset(name);
  });
  const handleReset = run(async () => {
    await api.reset();
    setActivePreset(null);
  });
  const handleHold = run((commandId) => api.hold(commandId));
  const handleRelease = run((commandId) => api.release(commandId));
  const handleConfirm = run((endpointId) => api.confirm(endpointId));

  return (
    <Card className="gap-0 py-4">
      <CardHeader className="px-4 pb-3">
        <CardTitle className="text-sm">Controls</CardTitle>
      </CardHeader>

      <CardContent className="space-y-4 px-4">
        {error && (
          <p className="rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-xs text-danger">{error}</p>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" onClick={handleStart}>
            <Play className="size-3.5" />
            Start
          </Button>

          <Select value={redirectTarget} onValueChange={setRedirectTarget}>
            <SelectTrigger size="sm" className="w-[150px] font-mono text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {FACILITIES.map((facility) => (
                <SelectItem key={facility} value={facility} className="font-mono text-xs">
                  {facility}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Button variant="outline" size="sm" onClick={handleRedirect}>
            Redirect
          </Button>
          <Button variant="outline" size="sm" onClick={handleReset}>
            <RotateCcw className="size-3.5" />
            Reset
          </Button>

          <label className="ml-auto flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={manualMode}
              onChange={(e) => onManualModeChange(e.target.checked)}
              className="size-3.5 accent-primary"
            />
            Manual mode
          </label>
        </div>

        <Separator />

        <div>
          <h4 className="text-xs font-medium text-muted-foreground">Network presets</h4>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {PRESETS.map((name) => {
              const isActive = activePreset === name;
              const isChaos = name === "CHAOS";
              return (
                <Button
                  key={name}
                  size="xs"
                  variant={isActive ? "default" : "outline"}
                  onClick={() => handlePreset(name)}
                  className={cn(
                    "font-mono",
                    isChaos && !isActive && "border-danger-border text-danger hover:bg-danger-soft",
                  )}
                >
                  {isChaos && <AlertTriangle className="size-3" />}
                  {name}
                </Button>
              );
            })}
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            {activePreset ? (
              <>
                Loaded <span className="font-medium text-foreground">{activePreset}</span> — it only takes effect on the{" "}
                <em>next</em> Start/Redirect (a preset just changes network delays, it doesn't do anything by itself).
              </>
            ) : (
              "No preset loaded — network delays are random until you pick one."
            )}
          </p>
        </div>

        <Separator />

        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <h4 className="text-xs font-medium text-muted-foreground">In-flight messages</h4>
            <ul className="mt-2 space-y-1">
              {inFlight.length === 0 && <li className="text-xs text-muted-foreground/70">none</li>}
              {inFlight.map((message) => (
                <li
                  key={message.command_id}
                  className="flex items-center justify-between gap-2 rounded-md border border-border px-2 py-1 text-xs"
                >
                  <span className="truncate font-mono">
                    {message.command_id} · {message.label}
                  </span>
                  {message.held ? (
                    <Button size="xs" variant="secondary" onClick={() => handleRelease(message.command_id)}>
                      <Play className="size-3" />
                      Release
                    </Button>
                  ) : (
                    <Button size="xs" variant="ghost" onClick={() => handleHold(message.command_id)}>
                      <Pause className="size-3" />
                      Hold
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          </div>

          <div>
            <h4 className="text-xs font-medium text-muted-foreground">Confirm (manual mode)</h4>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {[...FACILITIES, transportId].map((endpointId) => (
                <Button
                  key={endpointId}
                  size="xs"
                  variant="outline"
                  className="font-mono"
                  onClick={() => handleConfirm(endpointId)}
                >
                  {endpointId}
                </Button>
              ))}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
