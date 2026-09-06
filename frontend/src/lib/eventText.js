// Turns an event payload into a sentence an operator can read.
//
// The timeline used to print JSON.stringify(payload), which is precise and
// almost unreadable — during a demo nobody parses {"command_id":"cmd-8",
// "ack_type":"RECEIVED"} in the second it is on screen. The raw payload is
// still available on hover, so nothing is hidden; this only changes what the
// eye lands on first.
//
// Every branch below mirrors what the backend actually puts in that event's
// payload (see events.py's EventType and the _log calls in dispatcher.py,
// facility.py and ambulance.py). Anything unrecognised falls through to the
// raw JSON rather than guessing.

// Acks are logged by the dispatcher with no facility_id, so these are
// phrased without a subject rather than inventing one.
const ACK_TEXT = {
  RECEIVED: "Receipt confirmed",
  READY: "Hospital is ready",
  APPLIED: "Change applied",
  DECLINED: "Declined",
};

function bed(payload) {
  return payload.bed_type ? `${payload.bed_type} bed` : "bed";
}

function list(values) {
  if (!values || values.length === 0) return "nothing";
  if (values.length === 1) return values[0];
  return `${values.slice(0, -1).join(", ")} and ${values[values.length - 1]}`;
}

export function eventText(event) {
  const p = event.payload ?? {};
  const at = event.facility_id;

  switch (event.type) {
    // -- the handoff protocol ---------------------------------------------
    case "TransportStarted":
      return `Transport started — first destination ${p.destination}`;
    case "RedirectRequested":
      return p.still_starting
        ? `Retargeted to ${p.target} before ever activating anywhere`
        : `Redirect requested to ${p.target}`;
    case "RedirectQueued":
      return `Redirect to ${p.target} queued — the current cutover is past the point of no return`;
    case "CommandSent":
      // kind distinguishes the two directions on the wire: a "command" goes
      // out to the endpoint, an "ack" is that endpoint answering. Reading
      // both as "sent X to Hospital_5" got the direction backwards for half
      // the timeline.
      return p.kind === "ack"
        ? `${at} answered ${p.label} (${p.command_id})`
        : `Sent ${p.label} to ${at}${p.command_id ? ` (${p.command_id})` : ""}`;
    case "MessageDelivered":
      // facility_id names the channel's endpoint, not necessarily the
      // recipient (an ack travels the other way), so this stays neutral.
      return `${p.command_id} crossed the network${at ? ` — ${at}` : ""}`;
    case "AckReceived":
      return `${ACK_TEXT[p.ack_type] ?? p.ack_type} — ${p.command_id}`;
    case "ReadyConfirmed":
      return `${at} confirmed it is ready`;
    case "CutoverScheduled":
      // cutover_at is a raw wall-clock ms and means nothing to a reader; the
      // t(ms) column already carries the timing.
      return "Cutover scheduled — both hospitals switch at the same instant";
    case "WithdrawSent":
      return `Told the old hospital to stand down (${p.command_id}) — only now that the new one is provably active`;
    case "CutoverApplied":
      return `Cutover applied — ${p.current_destination} is now the active hospital`;
    case "CutoverCancelled":
      return "Cutover cancelled — a newer redirect overtook it before the point of no return";
    case "RedirectAborted":
      return "Redirect aborted — the transport stays where it was";
    case "Arrived":
      return `Ambulance arrived at ${p.at}`;

    // -- the fences: messages the system deliberately ignored --------------
    case "StaleIgnored":
      if (p.notice_seq !== undefined)
        return `Ignored a stale notice (#${p.notice_seq}) — issued before this transport was stood down`;
      if (p.reason === "transition_already_concluded")
        return `Ignored ${p.command_id} — the transition it belonged to had already finished`;
      if (p.reason === "unknown_command")
        return `Ignored ${p.command_id} — not a command this transport ever sent`;
      return `Ignored ${p.command_id} — from an abandoned plan (older epoch)`;
    case "DuplicateIgnored":
      return `Ignored a duplicate of ${p.command_id} — the network delivered it twice`;
    case "LateDeclineIgnored":
      return `Ignored a late decline from ${p.hospital_id} — it had already confirmed its activation`;
    case "TargetMismatch":
      return `Rejected ${p.command_id} — addressed to a different facility`;
    case "IllegalTransition":
      return `Refused ${p.action ?? "a command"} — not a legal move from ${p.state ?? "its current state"}`;
    case "BoundExceeded":
      return p.reason === "activate_received_timeout"
        ? "The new hospital never confirmed in time — aborting rather than risking a gap"
        : `Timing bound exceeded (${p.reason ?? "unknown"})`;

    // -- hospital state ----------------------------------------------------
    case "FacilityStateChanged":
      if (p.declined) return `${at} declined: ${p.declined}`;
      return `${at}: ${p.from} → ${p.to} (${p.action})`;
    case "HospitalStatusChanged": {
      const parts = [];
      if (p.diversion) parts.push(`diversion ${p.diversion}`);
      if (p.ed_saturation !== undefined) parts.push(`ED saturation ${Math.round(p.ed_saturation * 100)}%`);
      if (p.specialists_on_shift) parts.push(`${p.specialists_on_shift.length} specialists on shift`);
      if (p.diverted_categories) parts.push(`diverting ${list(p.diverted_categories)}`);
      return `${at} reported ${parts.length ? parts.join(", ") : "a status change"}`;
    }
    case "BedsReported":
      return `${at} now has ${p.total} ${p.bed_type} bed${p.total === 1 ? "" : "s"} in total`;

    // -- capacity ----------------------------------------------------------
    case "BedReserved":
      return `Held a ${bed(p)} at ${p.hospital_id}`;
    case "BedReleased":
      return `Released the ${bed(p)} at ${p.hospital_id}`;
    case "BedOccupied":
      return `Patient is in the ${bed(p)} at ${p.hospital_id}`;
    case "CandidateDeclined":
      return `${p.hospital_id} declined — ${p.reason}`;
    case "NoAcceptingFacility":
      if (!p.tried || p.tried.length === 0)
        return p.gave_up
          ? "No hospital can take this patient — ambulance standing by"
          : "No better hospital available — staying where it is";
      return `No hospital can take this patient — tried ${list(p.tried)}`;
    case "AutoRedirect":
      return `Auto-redirected from ${p.from} to ${p.to} — ${p.reason}`;
    case "CapacityRebalance":
      return p.displaced && p.displaced.length
        ? `${p.hospital_id} lost ${p.bed_type} capacity — moved ${list(p.displaced)}`
        : `${p.hospital_id} lost ${p.bed_type} capacity — nobody needed moving`;

    // -- the roster --------------------------------------------------------
    case "HospitalRegistered":
      return `${p.name ?? p.id} joined the network`;
    case "HospitalUpdated":
      return `${p.name ?? p.id} updated its details`;
    case "HospitalDecommissioned":
      return `${p.hospital_id} was retired from the network`;

    default:
      return JSON.stringify(p);
  }
}
