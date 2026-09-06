// Every write action the UI can take. "No component fetches on its own"
// (see state.js) is about *reading* — these are commands, and Controls.jsx
// / AmbulancePanel.jsx / FuzzPanel.jsx are the only callers.
// FastAPI's `detail` is a string for a plain HTTPException but an *object*
// for the structured ones (a 409 carries {error, reason}). Interpolating that
// straight into an Error produced the useless "[object Object]" the UI showed.
const REASON_TEXT = {
  already_arrived: "This patient has already arrived — they can't be redirected.",
  not_eligible: "That hospital can't take this patient.",
};

function errorMessage(detail, path, status) {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    if (detail.reason) return REASON_TEXT[detail.reason] ?? `Not eligible — ${detail.reason}`;
    if (detail.error) return REASON_TEXT[detail.error] ?? detail.error;
    // Pydantic validation errors arrive as a list of {loc, msg, ...}.
    if (Array.isArray(detail)) return detail.map((d) => d.msg ?? JSON.stringify(d)).join("; ");
    return JSON.stringify(detail);
  }
  return `${path} failed with ${status}`;
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = `${path} failed with ${response.status}`;
    try {
      const body = await response.json();
      if (body.detail !== undefined) detail = errorMessage(body.detail, path, response.status);
    } catch {
      // response body wasn't JSON — keep the generic message
    }
    throw new Error(detail);
  }
  return response.json();
}

async function del(path) {
  const response = await fetch(path, { method: "DELETE" });
  if (!response.ok) {
    let detail = `${path} failed with ${response.status}`;
    try {
      const body = await response.json();
      if (body.detail !== undefined) detail = errorMessage(body.detail, path, response.status);
    } catch {
      // not JSON — keep the generic message
    }
    throw new Error(detail);
  }
  return response.json();
}

async function get(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path} failed with ${response.status}`);
  return response.json();
}

export const api = {
  startTransport: (transportId, destination, manualConfirm = false) =>
    post("/transports", { transport_id: transportId, destination, manual_confirm: manualConfirm }),
  redirect: (transportId, target) => post(`/transports/${transportId}/redirect`, { target }),
  runPreset: (name) => post(`/demo/preset/${name}`),
  reset: () => post("/demo/reset"),
  hold: (commandId) => post(`/demo/hold/${commandId}`),
  release: (commandId) => post(`/demo/release/${commandId}`),
  confirm: (endpointId) => post(`/demo/confirm/${endpointId}`),
  fuzz: (runs) => post(`/demo/fuzz?runs=${runs}`),
  explain: (transportId) => post(`/ai/explain/${transportId}`),
  recommend: (transportId) => post(`/ai/recommend/${transportId}`),

  // Phase 17: multi-hospital capacity extension
  listHospitals: () => get("/hospitals"),
  reportBeds: (hospitalId, bedType, total) => post(`/hospitals/${hospitalId}/beds`, { bed_type: bedType, total }),
  reportStatus: (hospitalId, changes) => post(`/hospitals/${hospitalId}/status`, changes),
  registerHospital: (hospital) => post("/hospitals", hospital),
  decommissionHospital: (hospitalId) => del(`/hospitals/${hospitalId}`),
  listTransports: () => get("/transports"),
  startBatch: (patients) => post("/transports/batch", { patients }),
  candidates: (transportId) => get(`/transports/${transportId}/candidates`),
  discharge: (transportId) => post(`/transports/${transportId}/discharge`),
  justify: (transportId) => post(`/ai/justify/${transportId}`),
  aiDispatch: (text, position) => post("/ai/dispatch", { text, position }),
  redirectCapacityAware: (transportId, target) => post(`/transports/${transportId}/redirect`, { target: target ?? null }),
  getPolicy: () => get("/policy"),
  setPolicy: (mode) => post("/policy", { mode }),
  globalInvariant: () => get("/invariant"),
};
