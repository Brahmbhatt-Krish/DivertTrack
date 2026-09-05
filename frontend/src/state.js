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
};

export function facilityKey(facilityId, transportId) {
  return `${facilityId}:${transportId}`;
}

export function reduce(state, message) {
  console.log("[state]", message.kind, message);
  switch (message.kind) {
    case "connection":
      return { ...state, connected: message.connected };

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

    case "ambulance":
      return {
        ...state,
        ambulances: {
          ...state.ambulances,
          [message.transport_id]: { known_destination: message.known_destination, progress: message.progress },
        },
      };

    case "invariant":
      return {
        ...state,
        invariant: { ...state.invariant, [message.transport_id]: message.result },
      };

    case "in_flight":
      return { ...state, inFlight: message.messages };

    case "fuzz":
      return { ...state, fuzz: message };

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
