"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { AnalyticsSummary, FailedSearch } from "@/types/api";

interface OutdatedDocument {
  id: string;
  title: string;
  department: string;
  academic_year: number;
  years_behind: number;
}

export default function AdminPage() {
  const { can, loading: authLoading } = useAuth();
  const [analytics, setAnalytics] = useState<AnalyticsSummary | null>(null);
  const [failed, setFailed] = useState<FailedSearch[]>([]);
  const [topFailed, setTopFailed] = useState<{ query: string; count: number }[]>([]);
  const [outdated, setOutdated] = useState<OutdatedDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const allowed = can("view_analytics");

  useEffect(() => {
    if (authLoading || !allowed) return;
    Promise.all([
      api.analytics(),
      api.failedSearches(25),
      api.topFailedQueries(),
      api.outdatedDocuments().catch(() => []),
    ])
      .then(([summary, failures, top, stale]) => {
        setAnalytics(summary);
        setFailed(failures);
        setTopFailed(top);
        setOutdated(stale as OutdatedDocument[]);
      })
      .catch(() => setError("Could not load admin data."))
      .finally(() => setLoading(false));
  }, [authLoading, allowed]);

  if (authLoading) return <p className="p-6 text-sm text-slate-500">Loading…</p>;

  if (!allowed) {
    return (
      <div className="p-6">
        <p className="text-sm text-slate-600">
          This page is only available to administrators.
        </p>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-5xl space-y-6">
        <h1 className="text-lg font-semibold text-slate-800">Admin dashboard</h1>
        {error && <p className="text-sm text-red-600">{error}</p>}
        {loading && <p className="text-sm text-slate-500">Loading…</p>}

        {analytics && (
          <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Questions asked" value={analytics.total_questions} />
            <Stat
              label="Answer rate"
              value={`${(analytics.answer_rate * 100).toFixed(1)}%`}
              tone={analytics.answer_rate >= 0.8 ? "good" : "warn"}
            />
            <Stat
              label="Helpful / not"
              value={`${analytics.thumbs_up} / ${analytics.thumbs_down}`}
            />
            <Stat
              label="Median latency"
              value={
                analytics.avg_latency_ms ? `${Math.round(analytics.avg_latency_ms)} ms` : "—"
              }
            />
            <Stat label="Documents" value={analytics.document_count} />
            <Stat label="Chunks indexed" value={analytics.chunk_count} />
            <Stat
              label="Unanswered"
              value={analytics.unanswered_questions}
              tone={analytics.unanswered_questions > 0 ? "warn" : "good"}
            />
          </section>
        )}

        <Panel
          title="Most common unanswered questions"
          description="Each of these is either a retrieval problem to tune or a document the university has not published yet."
        >
          {topFailed.length === 0 ? (
            <Empty>No unanswered questions recorded.</Empty>
          ) : (
            <ul className="divide-y divide-slate-100">
              {topFailed.map((row) => (
                <li key={row.query} className="flex items-center gap-3 px-3 py-2 text-sm">
                  <span className="flex-1 text-slate-700">{row.query}</span>
                  <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
                    asked {row.count}x
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Recent unanswered questions">
          {failed.length === 0 ? (
            <Empty>Nothing to review.</Empty>
          ) : (
            <ul className="divide-y divide-slate-100">
              {failed.map((row) => (
                <li key={row.id} className="px-3 py-2 text-sm">
                  <div className="text-slate-700">{row.query}</div>
                  <div className="mt-0.5 text-xs text-slate-400">
                    {new Date(row.created_at).toLocaleString()} · confidence{" "}
                    {row.top_score?.toFixed(2) ?? "—"} · {row.result_count ?? 0} results
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel
          title="Documents needing review"
          description="Still marked current, but from an earlier academic year."
        >
          {outdated.length === 0 ? (
            <Empty>All current documents are from the current academic year.</Empty>
          ) : (
            <ul className="divide-y divide-slate-100">
              {outdated.map((document) => (
                <li key={document.id} className="flex items-center gap-3 px-3 py-2 text-sm">
                  <span className="flex-1 text-slate-700">{document.title}</span>
                  <span className="text-xs text-slate-500">{document.department}</span>
                  <span className="rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-800">
                    {document.academic_year} · {document.years_behind}y behind
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone?: "good" | "warn";
}) {
  const valueTone =
    tone === "good" ? "text-green-700" : tone === "warn" ? "text-amber-700" : "text-slate-800";
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <div className="text-xs text-slate-500">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${valueTone}`}>{value}</div>
    </div>
  );
}

function Panel({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <div className="border-b border-slate-100 px-3 py-2">
        <h2 className="text-sm font-semibold text-slate-700">{title}</h2>
        {description && <p className="mt-0.5 text-xs text-slate-500">{description}</p>}
      </div>
      {children}
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="px-3 py-4 text-sm text-slate-500">{children}</p>;
}
