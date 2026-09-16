"use client";

import { useState } from "react";

import type { Citation } from "@/types/api";

/** Deep-link into the source PDF at the cited page, when the source is a URL. */
function citationHref(citation: Citation): string | null {
  if (!citation.source_url) return null;
  return citation.page_number
    ? `${citation.source_url}#page=${citation.page_number}`
    : citation.source_url;
}

export function CitationCard({ citation }: { citation: Citation }) {
  const [expanded, setExpanded] = useState(false);
  const href = citationHref(citation);

  return (
    <li className="rounded-lg border border-slate-200 bg-white p-3 text-sm shadow-sm transition hover:border-campus-500">
      <div className="flex items-start gap-2">
        <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded bg-campus-600 text-xs font-semibold text-white">
          {citation.marker}
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {href ? (
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium text-campus-700 underline-offset-2 hover:underline"
              >
                {citation.document_title}
              </a>
            ) : (
              <span className="font-medium text-slate-800">{citation.document_title}</span>
            )}

            {citation.is_outdated && (
              <span
                className="rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-800"
                title="This source is from an earlier academic year and may have been superseded."
              >
                {citation.academic_year} — may be outdated
              </span>
            )}
          </div>

          <p className="mt-0.5 text-xs text-slate-500">
            {citation.page_number ? `Page ${citation.page_number}` : "Page unknown"}
            {" · "}
            {citation.department}
            {" · "}
            {citation.document_type}
            {" · "}
            {citation.academic_year}
          </p>

          {citation.section_path && (
            <p className="mt-0.5 truncate text-xs text-slate-400" title={citation.section_path}>
              {citation.section_path}
            </p>
          )}

          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            className="mt-1.5 text-xs font-medium text-campus-600 hover:text-campus-700"
          >
            {expanded ? "Hide quoted text" : "Show quoted text"}
          </button>

          {expanded && (
            <blockquote className="mt-2 border-l-2 border-slate-300 pl-3 text-xs leading-relaxed text-slate-600">
              {citation.snippet}
            </blockquote>
          )}
        </div>
      </div>
    </li>
  );
}

export function CitationList({ citations }: { citations: Citation[] }) {
  if (citations.length === 0) return null;

  return (
    <div className="mt-3">
      <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Sources ({citations.length})
      </p>
      <ul className="space-y-2">
        {citations.map((citation) => (
          <CitationCard key={`${citation.marker}-${citation.chunk_id}`} citation={citation} />
        ))}
      </ul>
    </div>
  );
}
