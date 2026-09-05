// Opens the live WebSocket connection to the backend and hands every
// parsed message to onMessage. Reconnects on close (a fresh page load
// resubscribes to the hub, which is fine — the reducer state.js builds is
// derived entirely from messages it has actually seen, not from any
// assumption of an unbroken connection). Also reports connection status
// via onStatusChange so the UI can show it (TopBar).
export function connectLiveEvents(onMessage, onStatusChange) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${window.location.host}/events/live`;
  let socket = null;
  let closedByCaller = false;
  let retryTimer = null;

  function open() {
    socket = new WebSocket(url);

    socket.addEventListener("open", () => {
      console.log("[ws] connected");
      onStatusChange(true);
    });

    socket.addEventListener("message", (event) => {
      try {
        const message = JSON.parse(event.data);
        onMessage(message);
      } catch (err) {
        console.error("[ws] failed to parse message", err, event.data);
      }
    });

    socket.addEventListener("close", () => {
      console.log("[ws] closed");
      onStatusChange(false);
      if (!closedByCaller) {
        retryTimer = setTimeout(open, 1000);
      }
    });

    socket.addEventListener("error", (err) => {
      console.error("[ws] error", err);
    });
  }

  open();

  return () => {
    closedByCaller = true;
    if (retryTimer) clearTimeout(retryTimer);
    if (socket) socket.close();
  };
}
