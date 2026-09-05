// Explain (narrates the timeline) and Recommend (ranked hospital list with
// Apply). Apply never talks to the AI again — it calls the ordinary
// redirect route, same as the Controls panel's own redirect button.
import { useState } from "react";
import { Sparkles, Wand2 } from "lucide-react";
import { api } from "../api.js";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export default function AiPanel({ transportId }) {
  const [explanation, setExplanation] = useState(null);
  const [explainError, setExplainError] = useState(null);
  const [explainLoading, setExplainLoading] = useState(false);

  const [ranked, setRanked] = useState(null);
  const [recommendError, setRecommendError] = useState(null);
  const [recommendLoading, setRecommendLoading] = useState(false);
  const [applyError, setApplyError] = useState(null);

  async function handleExplain() {
    setExplainLoading(true);
    setExplainError(null);
    try {
      const result = await api.explain(transportId);
      if (result.error) {
        setExplanation(null);
        setExplainError(result.error);
      } else {
        setExplanation(result.explanation);
      }
    } catch (err) {
      setExplanation(null);
      setExplainError(err.message);
    } finally {
      setExplainLoading(false);
    }
  }

  async function handleRecommend() {
    setRecommendLoading(true);
    setRecommendError(null);
    setApplyError(null);
    try {
      const result = await api.recommend(transportId);
      if (result.error) {
        setRanked(null);
        setRecommendError(result.error);
      } else {
        setRanked(result.ranked);
      }
    } catch (err) {
      setRanked(null);
      setRecommendError(err.message);
    } finally {
      setRecommendLoading(false);
    }
  }

  async function handleApply(hospitalId) {
    setApplyError(null);
    try {
      await api.redirect(transportId, hospitalId);
    } catch (err) {
      setApplyError(err.message);
    }
  }

  return (
    <Card className="gap-0 py-4">
      <CardHeader className="px-4 pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Sparkles className="size-4 text-muted-foreground" />
            AI sidecar
          </CardTitle>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={handleExplain} disabled={explainLoading}>
              {explainLoading ? "Explaining…" : "Explain"}
            </Button>
            <Button variant="outline" size="sm" onClick={handleRecommend} disabled={recommendLoading}>
              <Wand2 className="size-3.5" />
              {recommendLoading ? "Ranking…" : "Recommend"}
            </Button>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-3 px-4">
        {explainError && <p className="text-xs text-muted-foreground">{explainError}</p>}
        {explanation && (
          <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm leading-relaxed">{explanation}</p>
        )}

        {recommendError && <p className="text-xs text-muted-foreground">{recommendError}</p>}
        {applyError && <p className="text-xs text-danger">{applyError}</p>}
        {ranked && ranked.length === 0 && (
          <p className="text-xs text-muted-foreground">No hospital could be recommended.</p>
        )}
        {ranked && ranked.length > 0 && (
          <ul className="space-y-1.5">
            {ranked.map((entry) => (
              <li
                key={entry.hospital_id}
                className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-xs font-medium">{entry.hospital_id}</span>
                    <Badge variant="secondary" className="font-mono">
                      {entry.score.toFixed(2)}
                    </Badge>
                  </div>
                  <p className="truncate text-xs text-muted-foreground" title={entry.reason}>
                    {entry.reason}
                  </p>
                </div>
                <Button size="xs" onClick={() => handleApply(entry.hospital_id)}>
                  Apply
                </Button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
