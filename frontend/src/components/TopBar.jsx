import { Activity } from "lucide-react";
import { cn } from "@/lib/utils";

export default function TopBar({ connected }) {
  return (
    <header className="sticky top-0 z-30 border-b border-border bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-6">
        <div className="flex items-center gap-2.5">
          <div className="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <Activity className="size-4" />
          </div>
          <div className="leading-none">
            <span className="text-sm font-semibold tracking-tight">DivertTrack</span>
            <span className="ml-2 text-xs text-muted-foreground">ambulance destination handoff</span>
          </div>
        </div>

        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
            connected
              ? "border-success-border bg-success-soft text-success"
              : "border-danger-border bg-danger-soft text-danger",
          )}
        >
          <span className={cn("size-1.5 rounded-full", connected ? "bg-success" : "bg-danger")} />
          {connected ? "Live" : "Disconnected"}
        </span>
      </div>
    </header>
  );
}
