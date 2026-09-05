export default function TopBar({ connected }) {
  return (
    <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3">
      <h1 className="text-lg font-semibold text-slate-900">DivertTrack</h1>
      <span
        className={`flex items-center gap-1.5 text-xs font-medium ${
          connected ? "text-emerald-600" : "text-red-600"
        }`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-emerald-500" : "bg-red-500"}`} />
        {connected ? "live" : "disconnected"}
      </span>
    </header>
  );
}
