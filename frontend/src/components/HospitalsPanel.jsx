// Phase 18: capacity bars + status strip per hospital — the multi-hospital
// counterpart to FacilityCard.jsx (kept separate rather than folded into
// it: the two hospital sets have unrelated shapes, three fixed facilities
// with no capacity vs six capacity-tracked ones — see the phase report).
// Seeds from GET /hospitals on mount, then stays live via the shared
// reducer's "hospital" WS messages.
//
// Each card also drives the two hospital-side write routes (POST
// /hospitals/{id}/beds and /status). Those are what make a hospital decline:
// they're the manual equivalent of drift.py, and let you force the
// acceptance checks (diversion, bed availability, ED saturation) by hand
// instead of waiting for a scenario preset to happen to trigger them.
import { memo, useEffect, useState } from "react";
import { Plus, SlidersHorizontal, Trash2, X } from "lucide-react";
import { api } from "../api.js";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const DIVERSIONS = ["OPEN", "PARTIAL", "FULL"];

function CapacityBar({ bedType, total, free, holders, diversion, onDischarge }) {
  const used = total - free;
  const pct = total > 0 ? Math.round((used / total) * 100) : 0;
  // A hospital on diversion cannot take a patient however many beds are free,
  // so a green "0/15" contradicts the FULL badge right above it. The rail and
  // the count carry that state; the filled portion still shows true occupancy
  // rather than being faked to 100%, which would claim beds are taken.
  const blocked = diversion === "FULL";
  const partial = diversion === "PARTIAL";
  // Who actually holds these beds. Reserved = held for an ambulance still en
  // route (releasable); occupied = a patient is in it.
  const reserved = holders?.reserved ?? [];
  const occupied = holders?.occupied ?? [];
  const title = [
    reserved.length ? `reserved: ${reserved.join(", ")}` : null,
    occupied.length ? `occupied: ${occupied.join(", ")}` : null,
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <div>
      <div className="flex items-center gap-2.5 text-xs">
        <span className={cn("w-20 shrink-0", blocked ? "text-danger" : "text-muted-foreground")}>{bedType}</span>
        <div
          className={cn(
            "h-1.5 flex-1 overflow-hidden rounded-full",
            blocked ? "bg-danger-soft ring-1 ring-inset ring-danger-border" : partial ? "bg-warning-soft" : "bg-muted",
          )}
          title={blocked ? `On full diversion — not accepting${title ? `
${title}` : ""}` : title || undefined}
        >
          <div
            className={cn(
              "h-1.5 rounded-full transition-all",
              blocked ? "bg-danger" : pct >= 100 ? "bg-danger" : pct >= 70 ? "bg-warning" : "bg-success",
            )}
            style={{ width: `${Math.min(100, pct)}%` }}
          />
        </div>
        <span
          className={cn(
            "w-12 shrink-0 text-right font-mono",
            blocked ? "text-danger" : "text-muted-foreground",
          )}
        >
          {used}/{total}
        </span>
      </div>
      {used > 0 && (
        <div className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5 pl-[5.5rem] text-[11px]">
          {[
            // Only an occupied bed can be discharged: a reservation belongs to
            // a handshake still in flight, and taking it back is a redirect,
            // not a discharge.
            ...reserved.map((id) => [id, "reserved — ambulance en route", false]),
            ...occupied.map((id) => [id, "occupied — patient arrived", true]),
          ].map(([id, hint, isOccupied]) => (
            <span key={id} className="inline-flex items-center gap-0.5" title={hint}>
              <span className={cn("font-mono", isOccupied ? "font-medium text-foreground" : "text-muted-foreground/80")}>
                {id}
              </span>
              {/* Discharge: the patient was treated and left, so the bed goes
                  back. Capacity only — the transport and its hospital are
                  untouched, since being cured is not a handoff. */}
              {isOccupied && (
              <button
                type="button"
                aria-label={`Discharge ${id}`}
                title={`Discharge ${id} — frees this ${bedType} bed`}
                onClick={() => onDischarge?.(id)}
                className="rounded p-0.5 text-muted-foreground/60 transition-colors hover:bg-danger-soft hover:text-danger focus-visible:outline focus-visible:outline-2 focus-visible:outline-danger"
              >
                <X className="size-3" />
              </button>
              )}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// POST /hospitals/{id}/beds + /status, per hospital. Collapsed by default so
// six cards' worth of inputs don't bury the bars they act on.
function HospitalControls({ hospital, onError }) {
  const [busy, setBusy] = useState(false);

  async function send(action) {
    setBusy(true);
    try {
      await action();
      onError(null);
    } catch (err) {
      onError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-2 space-y-2 border-t border-border pt-2.5">
      {/* Named for what it is: these stand in for a hospital's own reporting
          feed (an HL7/FHIR ADT integration in a real deployment), not for how
          the system is operated. Everything the dispatcher does with beds —
          reserving, releasing, rebalancing, discharging — is automatic. */}
      <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground/70">
        Simulate hospital reporting
      </p>
      <div className="flex items-center gap-1.5">
        <span className="w-20 shrink-0 text-xs text-muted-foreground">diversion</span>
        {DIVERSIONS.map((mode) => (
          <Button
            key={mode}
            variant={hospital.diversion === mode ? "default" : "outline"}
            size="xs"
            disabled={busy}
            onClick={() => send(() => api.reportStatus(hospital.id, { diversion: mode }))}
          >
            {mode}
          </Button>
        ))}
      </div>

      <div className="flex items-center gap-1.5">
        <span className="w-20 shrink-0 text-xs text-muted-foreground">ED sat</span>
        <input
          type="range"
          min="0"
          max="100"
          step="5"
          disabled={busy}
          value={Math.round((hospital.ed_saturation ?? 0) * 100)}
          onChange={(event) =>
            send(() => api.reportStatus(hospital.id, { ed_saturation: Number(event.target.value) / 100 }))
          }
          className="h-1.5 flex-1 accent-foreground"
        />
        <span className="w-12 shrink-0 text-right font-mono text-xs text-muted-foreground">
          {Math.round((hospital.ed_saturation ?? 0) * 100)}%
        </span>
      </div>

      {Object.entries(hospital.beds_total || {}).map(([bedType, total]) => (
        <div key={bedType} className="flex items-center gap-1.5">
          <span className="w-20 shrink-0 text-xs text-muted-foreground">{bedType}</span>
          <Button
            variant="outline"
            size="xs"
            disabled={busy || total <= 0}
            onClick={() => send(() => api.reportBeds(hospital.id, bedType, total - 1))}
          >
            −
          </Button>
          <span className="w-8 text-center font-mono text-xs">{total}</span>
          <Button
            variant="outline"
            size="xs"
            disabled={busy}
            onClick={() => send(() => api.reportBeds(hospital.id, bedType, total + 1))}
          >
            +
          </Button>
          <span className="text-[11px] text-muted-foreground">beds</span>
        </div>
      ))}
    </div>
  );
}

const BED_TYPES = ["GENERAL", "ICU", "PAEDIATRIC", "ISOLATION"];
const CAPABILITIES = [
  "TRAUMA_L1", "TRAUMA_L2", "TRAUMA_L3", "CATH_LAB", "STROKE_CENTRE", "CT_SCAN",
  "NICU", "BURN_UNIT", "BLOOD_BANK", "VENTILATORS", "BARIATRIC",
];

// Phase 21: the roster is event-sourced, so a hospital added here is a real
// member of the network immediately — rankable on the next dispatch, and
// still present after a restart.
function AddHospitalForm({ onDone, onError }) {
  const [form, setForm] = useState({
    id: "", name: "", x: "20", y: "20", ventilators_total: "2",
    beds: { GENERAL: "10", ICU: "2", PAEDIATRIC: "0", ISOLATION: "0" },
    capabilities: ["CT_SCAN"],
  });
  const [busy, setBusy] = useState(false);
  const set = (key, value) => setForm((f) => ({ ...f, [key]: value }));

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.registerHospital({
        id: form.id.trim(),
        name: form.name.trim() || form.id.trim(),
        location: [Number(form.x), Number(form.y)],
        // A bed type left at 0 is omitted entirely: the seeded hospitals
        // encode "has no ICU at all" as an absent key, and acceptance's
        // no_bed check reads it that way.
        beds_total: Object.fromEntries(
          Object.entries(form.beds).map(([k, v]) => [k, Number(v)]).filter(([, v]) => v > 0),
        ),
        capabilities: form.capabilities,
        ventilators_total: Number(form.ventilators_total),
      });
      onError(null);
      onDone();
    } catch (err) {
      onError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3 rounded-lg border border-border bg-card p-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <label className="space-y-1">
          <span className="text-xs text-muted-foreground">id</span>
          <input required value={form.id} onChange={(e) => set("id", e.target.value)}
            placeholder="Hospital_7"
            className="h-8 w-full rounded-md border border-border bg-background px-2 font-mono text-xs" />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted-foreground">name</span>
          <input value={form.name} onChange={(e) => set("name", e.target.value)}
            placeholder="Lakeside General"
            className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs" />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted-foreground">x (km)</span>
          <input type="number" min="0" max="40" step="0.5" value={form.x}
            onChange={(e) => set("x", e.target.value)}
            className="h-8 w-full rounded-md border border-border bg-background px-2 font-mono text-xs" />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted-foreground">y (km)</span>
          <input type="number" min="0" max="40" step="0.5" value={form.y}
            onChange={(e) => set("y", e.target.value)}
            className="h-8 w-full rounded-md border border-border bg-background px-2 font-mono text-xs" />
        </label>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {BED_TYPES.map((bedType) => (
          <label key={bedType} className="space-y-1">
            <span className="text-xs text-muted-foreground">{bedType}</span>
            <input type="number" min="0" value={form.beds[bedType]}
              onChange={(e) => setForm((f) => ({ ...f, beds: { ...f.beds, [bedType]: e.target.value } }))}
              className="h-8 w-full rounded-md border border-border bg-background px-2 font-mono text-xs" />
          </label>
        ))}
        <label className="space-y-1">
          <span className="text-xs text-muted-foreground">ventilators</span>
          <input type="number" min="0" value={form.ventilators_total}
            onChange={(e) => set("ventilators_total", e.target.value)}
            className="h-8 w-full rounded-md border border-border bg-background px-2 font-mono text-xs" />
        </label>
      </div>

      <div>
        <p className="mb-1.5 text-xs text-muted-foreground">capabilities</p>
        <div className="flex flex-wrap gap-1.5">
          {CAPABILITIES.map((capability) => {
            const on = form.capabilities.includes(capability);
            return (
              <Button key={capability} type="button" size="xs" variant={on ? "default" : "outline"}
                onClick={() => setForm((f) => ({
                  ...f,
                  capabilities: on
                    ? f.capabilities.filter((c) => c !== capability)
                    : [...f.capabilities, capability],
                }))}
              >
                {capability}
              </Button>
            );
          })}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <Button type="submit" size="xs" disabled={busy || !form.id.trim()}>Add hospital</Button>
        <Button type="button" size="xs" variant="ghost" onClick={onDone}>Cancel</Button>
      </div>
    </form>
  );
}

function HospitalsPanel({ hospitals }) {
  const [seed, setSeed] = useState({});
  const [open, setOpen] = useState(null);
  const [error, setError] = useState(null);
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    api
      .listHospitals()
      .then((list) => setSeed(Object.fromEntries(list.map((h) => [h.id, h]))))
      .catch(() => {});
  }, []);

  // A "roster" push replaces state.hospitals wholesale, so once one arrives
  // it is the whole truth — the mount-time seed must not resurrect a
  // hospital that has since been decommissioned.
  const merged = Object.keys(hospitals || {}).length ? hospitals : seed;
  const ids = Object.keys(merged).sort();

  if (ids.length === 0) {
    return (
      <Card className="py-0">
        <CardContent className="px-4 py-6 text-center text-sm text-muted-foreground">Loading hospitals…</CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      {error && (
        <p className="rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-xs text-danger">{error}</p>
      )}

      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          {ids.length} hospital{ids.length === 1 ? "" : "s"} in the network
        </p>
        {!adding && (
          <Button size="xs" variant="outline" onClick={() => setAdding(true)}>
            <Plus className="size-3" />
            Add hospital
          </Button>
        )}
      </div>
      {adding && <AddHospitalForm onDone={() => setAdding(false)} onError={setError} />}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {ids.map((id) => {
          const h = merged[id];
          const loadPct = Math.round((h.load ?? 0) * 100);
          return (
            <Card key={id} className="gap-0 py-4">
              <CardHeader className="px-4 pb-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <CardTitle className="truncate text-sm">{h.name}</CardTitle>
                    <p className="font-mono text-xs text-muted-foreground">{h.id}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {h.diversion !== "OPEN" && (
                      <Badge
                        variant="outline"
                        className={cn(
                          h.diversion === "FULL"
                            ? "border-danger-border bg-danger-soft text-danger"
                            : "border-warning-border bg-warning-soft text-warning",
                        )}
                      >
                        {h.diversion}
                        {h.diversion === "PARTIAL" && h.diverted_categories?.length
                          ? `: ${h.diverted_categories.join(",")}`
                          : ""}
                      </Badge>
                    )}
                    <Button
                      variant="ghost"
                      size="xs"
                      aria-label={`Adjust ${h.name}`}
                      aria-pressed={open === id}
                      onClick={() => setOpen(open === id ? null : id)}
                    >
                      <SlidersHorizontal className={cn("size-3", open === id && "text-foreground")} />
                    </Button>
                    <Button
                      variant="ghost"
                      size="xs"
                      aria-label={`Decommission ${h.name}`}
                      title="Decommission — any ambulance already en route is re-routed"
                      onClick={() =>
                        api.decommissionHospital(id).then(
                          () => setError(null),
                          (err) => setError(err.message),
                        )
                      }
                    >
                      <Trash2 className="size-3 text-danger" />
                    </Button>
                  </div>
                </div>

                <div className="mt-2 flex items-center gap-3 text-xs text-muted-foreground">
                  <span>
                    load <span className="font-mono text-foreground">{loadPct}%</span>
                  </span>
                  <span className="text-border">|</span>
                  <span>
                    ED sat <span className="font-mono text-foreground">{Math.round((h.ed_saturation ?? 0) * 100)}%</span>
                  </span>
                </div>
              </CardHeader>

              <CardContent className="space-y-1.5 px-4">
                {Object.entries(h.beds_total || {}).map(([bedType, total]) => (
                  <CapacityBar
                    key={bedType}
                    bedType={bedType}
                    total={total}
                    free={h.free?.[bedType] ?? total}
                    holders={h.holders?.[bedType]}
                    diversion={h.diversion}
                    onDischarge={(transportId) =>
                      api.discharge(transportId).then(
                        () => setError(null),
                        (err) => setError(err.message),
                      )
                    }
                  />
                ))}

                <p
                  className="truncate pt-1.5 text-xs text-muted-foreground"
                  title={(h.specialists_on_shift || []).join(", ")}
                >
                  <span className="text-muted-foreground/70">on shift:</span>{" "}
                  {(h.specialists_on_shift || []).length ? (h.specialists_on_shift || []).join(", ") : "none"}
                </p>

                {open === id && <HospitalControls hospital={h} onError={setError} />}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

export default memo(HospitalsPanel);
