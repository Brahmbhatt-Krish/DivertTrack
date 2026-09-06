// The single reducer every component reads from — no component fetches on
// its own. Each hub message (see ws.py's broadcast shapes) is folded into
// one state object; useHubState() is the only thing that talks to the
// WebSocket.
import { useEffect, useReducer } from "react";
import { connectLiveEvents } from "./ws.js";

export const MAX_TIMELINE_EVENTS = 500;

export const initialState = {
  connected: false,
  transports: {}, // transport_id -> TransportView
  facilities: {}, // "facilityId:transportId" -> FacilityView
  ambulances: {}, // transport_id -> { known_destination, progress }
  invariant: {}, // transport_id -> CheckResult
  inFlight: [], // [{ command_id, kind, label, held, expected_arrival_ms }]
  timeline: [], // event payloads, oldest first
  fuzz: null, // last fuzz summary, or null before any run
  hospitals: {}, // hospital_id -> hospital view (Phase 17)
  transportRows: null, // GET /transports' rows, pushed live by the hub.
  // null (not []) means "nothing pushed yet", so the table can tell an
  // un-started session from one the hub has told us is genuinely empty.
  alerts: [], // [{ type, transport_id, payload, ts }] (Phase 17), newest last
};

export const MAX_ALERTS = 100;

export function facilityKey(facilityId, transportId) {
  return `${facilityId}:${transportId}`;
}

export function reduce(state, message) {
  switch (message.kind) {
    // The hub coalesces everything produced in one ~200ms window into a
    // single message (see ws.py's _flush). Folding it through this same
    // reducer means a burst costs one React render, not one per event —
    // and no per-message console.log, which by itself was a real drag
    // during a mass-casualty run.
    case "batch":
      return message.messages.reduce(reduce, state);

    case "connection":
      return { ...state, connected: message.connected };

    // POST /demo/reset empties the event log server-side. Everything below
    // is a projection of that log, so all of it is now fiction — drop the
    // lot rather than letting stale transports, facilities and bed counts
    // linger until someone reloads. `connected` is live socket state, not a
    // projection, so it survives. The hub sends fresh hospital views in the
    // same batch, immediately after this.
    case "reset":
      return { ...initialState, connected: state.connected };

    case "event":
      return {
        ...state,
        timeline: [...state.timeline, message.event].slice(-MAX_TIMELINE_EVENTS),
      };

    case "transport":
      return {
        ...state,
        transports: { ...state.transports, [message.transport_id]: message.view },
      };

    case "facility":
      return {
        ...state,
        facilities: {
          ...state.facilities,
          [facilityKey(message.facility_id, message.transport_id)]: message.view,
        },
      };

    case "ambulance": {
      if (message.removed) {
        // Discharged and retired — drop it so its marker leaves the map and
        // the fleet list, rather than freezing at its last known position.
        const { [message.transport_id]: _gone, ...rest } = state.ambulances;
        return { ...state, ambulances: rest };
      }
      return {
        ...state,
        ambulances: {
          ...state.ambulances,
          // Everything the view carries, not a hand-picked subset: the
          // fields were listed individually and each new one (position, then
          // arrived/remaining_km) had to be remembered here too — the last
          // omission left arrived ambulances rendering as still en route.
          // `kind` and `transport_id` are the envelope, not the view.
          [message.transport_id]: (({ kind, transport_id, ...view }) => view)(message),
        },
      };
    }

    case "invariant":
      return {
        ...state,
        invariant: { ...state.invariant, [message.transport_id]: message.result },
      };

    case "in_flight":
      return { ...state, inFlight: message.messages };

    case "fuzz":
      return { ...state, fuzz: message };

    // A whole-list replacement, not a delta — see ws.py's _flush.
    case "transport_list":
      return { ...state, transportRows: message.rows };

    // Phase 21: the whole roster, replacing what we had. Sent wholesale so a
    // decommissioned hospital needs no special case — it is simply absent.
    // Keyed by id here to match the "hospital" case's per-hospital updates.
    case "roster":
      return {
        ...state,
        hospitals: Object.fromEntries(message.hospitals.map((h) => [h.id, h])),
      };

    case "hospital":
      return {
        ...state,
        hospitals: { ...state.hospitals, [message.hospital_id]: message.view },
      };

    case "alert":
      return {
        ...state,
        alerts: [...state.alerts, message].slice(-MAX_ALERTS),
      };

    default:
      console.warn("[state] unknown message kind", message.kind, message);
      return state;
  }
}

export function useHubState() {
  const [state, dispatch] = useReducer(reduce, initialState);

  useEffect(() => {
    const close = connectLiveEvents(
      (message) => dispatch(message),
      (connected) => dispatch({ kind: "connection", connected }),
    );
    return close;
  }, []);

  return state;
}
