import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import type { Session } from "../api/types";
import { ModelPicker } from "./ModelPicker";
import { ModePicker } from "./ModePicker";

/** Live model switch for a controllable (server-launched) session. */
function ModelControl({ session }: { session: Session }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [model, setModel] = useState("");
  const [error, setError] = useState("");
  const adapters = useQuery({ queryKey: ["adapters"], queryFn: api.adapters });
  const info = adapters.data?.adapters.find((a) => a.name === session.adapter);

  const change = useMutation({
    mutationFn: () => api.setSessionModel(session.id, model),
    onSuccess: (updated) => {
      queryClient.setQueryData(["session", session.id], updated);
      setEditing(false);
      setError("");
    },
    onError: (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
  });

  // The adapter declared it cannot switch models: don't offer the control.
  if (info && !info.capabilities.set_model) return null;

  if (!editing) {
    return (
      <button
        type="button"
        className="btn option"
        onClick={() => {
          setModel(session.model ?? "");
          setError("");
          setEditing(true);
        }}
        title="Switch the model this session runs on"
      >
        {session.model ? `Model: ${session.model}` : "Model"}
      </button>
    );
  }
  return (
    <span className="model-control">
      <ModelPicker
        id="session-model"
        value={model}
        onChange={setModel}
        models={info?.models ?? []}
        disabled={change.isPending}
      />
      <button type="button" onClick={() => change.mutate()} disabled={change.isPending}>
        {change.isPending ? "Applying…" : "Apply"}
      </button>
      <button type="button" onClick={() => setEditing(false)} disabled={change.isPending}>
        Cancel
      </button>
      {error ? <span className="notice error">{error}</span> : null}
    </span>
  );
}

/** Live permission-mode switch (Plan / Manual / Edit Automatically / Auto). */
function ModeControl({ session }: { session: Session }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState("");
  const adapters = useQuery({ queryKey: ["adapters"], queryFn: api.adapters });
  const info = adapters.data?.adapters.find((a) => a.name === session.adapter);
  const change = useMutation({
    mutationFn: (mode: string) => api.setSessionPermissionMode(session.id, mode),
    onSuccess: (updated) => {
      queryClient.setQueryData(["session", session.id], updated);
      setError("");
    },
    onError: (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
  });
  // The adapter declared it cannot switch permission modes: no control.
  if (info && !info.capabilities.set_permission_mode) return null;
  return (
    <span className="mode-control">
      <ModePicker
        value={session.permission_mode || "default"}
        disabled={change.isPending}
        onChange={(mode) => change.mutate(mode)}
      />
      {error ? <span className="notice error">{error}</span> : null}
    </span>
  );
}

/** Context-window fullness. Hidden if the adapter can't report it (the shape
 * is CLI-owned, so a build that lacks it should degrade, not error). */
function ContextPill({ session }: { session: Session }) {
  const usage = useQuery({
    queryKey: ["context", session.id],
    queryFn: () => api.sessionContext(session.id),
    // On demand, plus a slow refresh; a failing call just leaves the pill empty.
    refetchInterval: 30_000,
    retry: false,
  });
  const u = usage.data;
  if (!u || (!u.max_tokens && !u.percentage)) return null;
  const pct = Math.round(u.percentage);
  const k = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));
  return (
    <span className="context-pill" title="Context window in use">
      {pct}%{u.max_tokens ? ` · ${k(u.total_tokens)}/${k(u.max_tokens)}` : ""}
    </span>
  );
}

/**
 * The message composer for a running, server-launched session: send or queue a
 * follow-up prompt, stop the current turn, and switch model/mode inline —
 * Command Center's equivalent of the VS Code input bar. Only controllable
 * sessions reach here (observed sessions aren't owned by the SDK), so every
 * control maps to a real control request.
 */
export function Composer({ session }: { session: Session }) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  // "Busy" = the agent is mid-turn; a message then queues rather than starts one.
  const busy = session.state === "running" || session.state === "starting";

  const send = useMutation({
    mutationFn: (prompt: string) => api.promptSession(session.id, prompt),
    onSuccess: () => {
      setText("");
      setError("");
      void queryClient.invalidateQueries({ queryKey: ["session", session.id] });
    },
    onError: (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
  });

  const stop = useMutation({
    mutationFn: () => api.interruptSession(session.id),
    onSuccess: (updated) => {
      queryClient.setQueryData(["session", session.id], updated);
      setError("");
    },
    onError: (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
  });

  const submit = () => {
    const prompt = text.trim();
    if (prompt) send.mutate(prompt);
  };

  return (
    <div className="composer">
      <div className="composer-controls">
        <ContextPill session={session} />
        <ModelControl session={session} />
        <ModeControl session={session} />
      </div>
      <div className="composer-input">
        <textarea
          rows={2}
          value={text}
          placeholder={busy ? "Queue a message for the agent…" : "Message the agent…"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends; Shift+Enter is a newline (matches the IDE).
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="composer-actions">
          <button
            type="button"
            className="btn deny"
            onClick={() => stop.mutate()}
            disabled={stop.isPending || !busy}
            title="Stop the current turn (the session stays open)"
          >
            {stop.isPending ? "Stopping…" : "Stop"}
          </button>
          <button
            type="button"
            className="btn approve"
            onClick={submit}
            disabled={send.isPending || !text.trim()}
          >
            {send.isPending ? "Sending…" : busy ? "Queue" : "Send"}
          </button>
        </div>
      </div>
      {error ? <div className="notice error">{error}</div> : null}
    </div>
  );
}
