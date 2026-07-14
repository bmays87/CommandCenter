import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, getToken, setToken, UnauthorizedError } from "./api/client";
import { FleetView } from "./views/FleetView";
import { SessionView } from "./views/SessionView";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, error) => !(error instanceof UnauthorizedError) && count < 2,
    },
  },
});

function useHashRoute(): string {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const onChange = () => setHash(location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

function TokenGate({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState(getToken());
  const [error, setError] = useState("");
  return (
    <form
      className="token-gate"
      onSubmit={(e) => {
        e.preventDefault();
        setToken(value.trim());
        api
          .sessions()
          .then(() => onDone())
          .catch((err: unknown) =>
            setError(err instanceof UnauthorizedError ? "Invalid token." : String(err)),
          );
      }}
    >
      <h1>Prodeo Command Center</h1>
      <p>This server requires an API token (PRODEO_API_TOKEN).</p>
      <input
        type="password"
        placeholder="API token"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        autoFocus
      />
      <button type="submit">Connect</button>
      {error ? <div className="notice error">{error}</div> : null}
    </form>
  );
}

function Shell() {
  const route = useHashRoute();
  const [authed, setAuthed] = useState(true); // optimistic; flips on first 401

  useEffect(() => {
    api.sessions().catch((err: unknown) => {
      if (err instanceof UnauthorizedError) setAuthed(false);
    });
  }, []);

  if (!authed) {
    return (
      <TokenGate
        onDone={() => {
          setAuthed(true);
          void queryClient.invalidateQueries();
        }}
      />
    );
  }

  const sessionMatch = /^#\/session\/(.+)$/.exec(route);
  return (
    <div className="shell">
      <nav className="topbar">
        <a href="#/" className="brand">
          ⌘ Prodeo
        </a>
        <span className="topbar-note">command center</span>
      </nav>
      <main>
        {sessionMatch?.[1] ? <SessionView id={sessionMatch[1]} /> : <FleetView />}
      </main>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Shell />
    </QueryClientProvider>
  );
}
