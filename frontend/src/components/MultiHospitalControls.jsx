// Phase 18: batch start, policy toggle, and the five multi-hospital
// presets — kept separate from Controls.jsx (the single AMB-101 demo's
// panel) rather than merged into it, since the two flows don't share a
// transport id or a hospital set. See the phase report.
import { useEffect, useState } from "react";
import { Plus, Sparkles } from "lucide-react";
import { api } from "../api.js";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";

const PRESETS = ["MULTI_NORMAL", "LAST_BED_RACE", "DECLINE_CHAIN", "CAPACITY_DROP", "MASS_CASUALTY"];
const CONDITIONS = ["GENERAL", "CARDIAC", "TRAUMA", "STROKE", "BURN", "RESPIRATORY", "OBSTETRIC", "PAEDIATRIC", "PSYCHIATRIC"];

// A monotonic counter rather than Date.now(): the id ends up inside the
// transport id ("BATCH-<id>-<index>"), which is rendered as a label next to a
// 3.5px dot on the region map — a 13-digit timestamp there is unreadable.
// Never reset, so ids stay unique across /demo/reset within a session.
let patientCounter = 0;

// Weighted like the backend's own generator (seed.random_patient): mostly
// adults, occasionally a child, rarely a neonate. This used to hardcode
// ADULT / no needs / no override, which meant PAEDIATRIC and ISOLATION beds
// could never fill from the UI and the NICU capability and the
// nearest_capable override were unreachable — half the acceptance logic was
// undemoable.
function weighted(choices) {
  const roll = Math.random();
  let cumulative = 0;
  for (const [value, weight] of choices) {
    cumulative += weight;
    if (roll < cumulative) return value;
  }
  return choices[choices.length - 1][0];
}

const NEEDS = ["VENTILATOR", "ISOLATION", "CATH_LAB", "CT_SCAN", "BARIATRIC", "BLOOD_PRODUCTS"];

function randomPatient() {
  return {
    id: `WEB-${++patientCounter}`,
    acuity: 1 + Math.floor(Math.random() * 5),
    condition: CONDITIONS[Math.floor(Math.random() * CONDITIONS.length)],
    age_group: weighted([["ADULT", 0.8], ["CHILD", 0.15], ["NEONATE", 0.05]]),
    needs: NEEDS.filter(() => Math.random() < 0.12),
    // The escape hatch: skips the transport-window and ED-saturation checks
    // and ranks on distance alone.
    override: Math.random() < 0.1 ? "nearest_capable" : "none",
    position: [Math.random() * 40, Math.random() * 40],
  };
}

export default function MultiHospitalControls({ onChanged }) {
  // Read the server's actual mode rather than assuming: the backend now
  // defaults to auto, and a toggle showing "manual" while the dispatcher is
  // auto-routing is worse than no toggle.
  const [policy, setPolicyState] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .getPolicy()
      .then(({ policy: mode }) => setPolicyState(mode))
      .catch(() => setPolicyState("manual"));
  }, []);

  async function run(action) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onChanged?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const handlePolicyChange = (mode) =>
    run(async () => {
      await api.setPolicy(mode);
      setPolicyState(mode);
    });

  const handleBatchStart = (count) => run(() => api.startBatch(Array.from({ length: count }, () => randomPatient())));

  const handlePreset = (name) => run(() => api.runPreset(name));

  const [describe, setDescribe] = useState("");
  const [parsed, setParsed] = useState(null);

  // The model is a parser here, never a decision-maker: it turns the text into
  // a patient, every field is validated against the real enums server-side,
  // and the deterministic acceptance function decides where that patient may
  // go. It is never asked which hospital to use, so it cannot invent one.
  const handleDescribe = (event) => {
    event.preventDefault();
    setParsed(null);
    return run(async () => {
      const result = await api.aiDispatch(describe, [Math.random() * 40, Math.random() * 40]);
      if (result.error) throw new Error(result.error);
      setParsed(result.patient);
      setDescribe("");
    });
  };

  return (
    <Card className="gap-0 py-4">
      <CardHeader className="px-4 pb-3">
        <CardTitle className="text-sm">Multi-hospital controls</CardTitle>
      </CardHeader>

      <CardContent className="space-y-4 px-4">
        {error && (
          <p className="rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-xs text-danger">{error}</p>
        )}

        <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Policy</span>
            <div className="inline-flex rounded-md border border-border p-0.5">
              {["manual", "auto"].map((mode) => (
                <button
                  key={mode}
                  disabled={busy}
                  onClick={() => handlePolicyChange(mode)}
                  className={
                    policy === mode
                      ? "rounded-sm bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground"
                      : "rounded-sm px-2.5 py-1 text-xs text-muted-foreground hover:text-foreground"
                  }
                >
                  {mode}
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Batch start</span>
            {[1, 3, 10].map((count) => (
              <Button key={count} size="xs" variant="outline" disabled={busy} onClick={() => handleBatchStart(count)}>
                <Plus className="size-3" />
                {count}
              </Button>
            ))}
          </div>
        </div>

        <Separator />

        <form onSubmit={handleDescribe} className="space-y-1.5">
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground" htmlFor="ai-describe">
            <Sparkles className="size-3" />
            Describe a patient
          </label>
          <div className="flex gap-2">
            <input
              id="ai-describe"
              value={describe}
              onChange={(e) => setDescribe(e.target.value)}
              placeholder="62-year-old chest pain, critical, needs a cath lab"
              className="h-8 flex-1 rounded-md border border-border bg-background px-2.5 text-xs"
            />
            <Button type="submit" size="xs" disabled={busy || !describe.trim()}>
              Dispatch
            </Button>
          </div>
          {parsed && (
            <p className="font-mono text-[11px] text-muted-foreground">
              acuity {parsed.acuity} · {parsed.condition} · {parsed.age_group}
              {parsed.needs.length ? ` · ${parsed.needs.join(", ")}` : ""}
              {parsed.override !== "none" ? ` · ${parsed.override}` : ""}
            </p>
          )}
        </form>

        <Separator />

        <div>
          <h4 className="text-xs font-medium text-muted-foreground">Scenario presets</h4>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {PRESETS.map((name) => (
              <Button
                key={name}
                size="xs"
                variant="outline"
                disabled={busy}
                onClick={() => handlePreset(name)}
                className="font-mono"
              >
                {name}
              </Button>
            ))}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
