import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import type { EnvironmentCheck } from "../api/types";

/**
 * What this machine is missing, and what would fix it — rather than letting
 * things silently degrade and surface later as a stack trace.
 */

function CheckRow({ check }: { check: EnvironmentCheck }) {
  // relevant=false means the question does not apply here (CUDA on a machine
  // with no GPU); showing it as a failure would be noise.
  const mark = !check.relevant ? "–" : check.ok ? "✓" : "!";
  const tone = !check.relevant ? "env-na" : check.ok ? "env-ok" : "env-warn";
  return (
    <li className={`env-check ${tone}`}>
      <span className="env-mark">{mark}</span>
      <div>
        <div className="env-label">
          {check.label}
          <span className="env-detail">{check.detail}</span>
        </div>
        {check.fix ? <p className="field-help">{check.fix}</p> : null}
        {check.fix_command ? <code className="env-fix">{check.fix_command}</code> : null}
      </div>
    </li>
  );
}

export function EnvironmentPanel() {
  const { data, error, isLoading } = useQuery({
    queryKey: ["environment"],
    queryFn: api.environment,
    staleTime: 30_000,
  });

  if (isLoading) return null;
  if (error) return <div className="notice error">{String(error)}</div>;

  const checks = data?.checks ?? [];
  const problems = checks.filter((c) => c.relevant && !c.ok).length;

  return (
    <section className="ext-detail">
      <h2>
        This machine
        {problems > 0 ? <span className="badge ext-blocked">{problems} to look at</span> : null}
      </h2>
      <p className="field-help">
        {data?.platform} · Python {data?.python}
      </p>
      <ul className="env-list">
        {checks.map((check) => (
          <CheckRow key={check.id} check={check} />
        ))}
      </ul>
    </section>
  );
}
