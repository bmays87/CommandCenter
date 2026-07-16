import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ConflictError } from "../api/client";
import type { AnswerRequest, Interaction } from "../api/types";
import { timeAgo } from "../format";

/** One pending interaction with its answer controls (inbox + session view). */
export function InteractionCard({
  interaction,
  showSessionLink = false,
}: {
  interaction: Interaction;
  showSessionLink?: boolean;
}) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [notice, setNotice] = useState("");

  const answer = useMutation({
    mutationFn: (body: AnswerRequest) => api.answerInteraction(interaction.id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["interactions"] });
    },
    onError: (err: unknown) => {
      if (err instanceof ConflictError) {
        setNotice("Already answered elsewhere.");
        void queryClient.invalidateQueries({ queryKey: ["interactions"] });
      } else {
        setNotice(String(err));
      }
    },
  });

  const isPermission = interaction.kind === "permission";
  const busy = answer.isPending;

  return (
    <div className={`interaction kind-${interaction.kind}`}>
      <div className="interaction-top">
        <span className="badge kind-badge">{interaction.kind}</span>
        <span className="interaction-title">{interaction.title}</span>
        <span className="ago">{timeAgo(interaction.requested_at)}</span>
        {showSessionLink ? (
          <a className="interaction-session" href={`#/session/${interaction.session_id}`}>
            session →
          </a>
        ) : null}
      </div>
      {interaction.body ? <pre className="interaction-body">{interaction.body}</pre> : null}
      {isPermission ? (
        <div className="interaction-actions">
          <button
            className="btn approve"
            disabled={busy}
            onClick={() => answer.mutate({ decision: "allow", text: "" })}
          >
            Approve
          </button>
          <button
            className="btn deny"
            disabled={busy}
            onClick={() => answer.mutate({ decision: "deny", text })}
          >
            Deny
          </button>
          <input
            className="interaction-input"
            placeholder="deny reason (optional)"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
      ) : (
        <div className="interaction-actions">
          {(interaction.options ?? []).map((option) => (
            <button
              key={option}
              className="btn option"
              disabled={busy}
              onClick={() => answer.mutate({ text: option })}
            >
              {option}
            </button>
          ))}
          <form
            className="interaction-answer"
            onSubmit={(e) => {
              e.preventDefault();
              if (text.trim()) answer.mutate({ text: text.trim() });
            }}
          >
            <input
              className="interaction-input"
              placeholder="type an answer…"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <button className="btn approve" type="submit" disabled={busy || !text.trim()}>
              Send
            </button>
          </form>
        </div>
      )}
      {notice ? <div className="interaction-notice">{notice}</div> : null}
    </div>
  );
}
