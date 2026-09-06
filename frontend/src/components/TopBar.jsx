import { Activity } from "lucide-react";
import { cn } from "@/lib/utils";

// Each screen is a separate window in a multi-screen demo, so these open in
// new tabs rather than navigating away from whatever you are already watching.
const ROLES = [
  ["dashboard", "Dashboard"],
  ["dispatcher", "Dispatch"],
  ["hospital_A", "Hosp A"],
  ["hospital_B", "Hosp B"],
  ["hospital_C", "Hosp C"],
  ["ambulance", "Ambulance"],
];

export default function TopBar({ connected, role = "dashboard" }) {
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

        <div className="flex items-center gap-3">
        <nav className="hidden items-center gap-0.5 sm:flex">
          {ROLES.map(([value, label]) => (
            <a
              key={value}
              href={`?role=${value}`}
              target={value === role ? undefined : "_blank"}
              rel="noreferrer"
              className={cn(
                "rounded-md px-2 py-1 text-xs transition-colors",
                value === role
                  ? "bg-muted font-medium text-foreground"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              {label}
            </a>
          ))}
        </nav>

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
      </div>
    </header>
  );
}
