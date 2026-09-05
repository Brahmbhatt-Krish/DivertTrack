// Every write action the UI can take. "No component fetches on its own"
// (see state.js) is about *reading* — these are commands, and Controls.jsx
// / AmbulancePanel.jsx / FuzzPanel.jsx are the only callers.
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
      if (body.detail) detail = body.detail;
    } catch {
      // response body wasn't JSON — keep the generic message
    }
    throw new Error(detail);
  }
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
};
