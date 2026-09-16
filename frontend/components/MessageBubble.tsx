"use client";

import { Fragment } from "react";

import { CitationList } from "@/components/CitationCard";
import { FeedbackButtons } from "@/components/FeedbackButtons";
import type { ChatTurn } from "@/types/api";

/**
 * Render answer text with `[n]` markers turned into superscript chips.
 * Splitting on the marker pattern avoids putting model output through
 * `dangerouslySetInnerHTML`, so a document that happens to contain markup
 * cannot inject anything into the page.
 */
function renderWithCitations(text: string) {
  const parts = text.split(/(\[\d+\])/g);
  return parts.map((part, index) => {
    const match = /^\[(\d+)\]$/.exec(part);
    if (!match) return <Fragment key={index}>{part}</Fragment>;
    return (
      <sup
        key={index}
        className="mx-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded bg-campus-100 px-1 text-[10px] font-semibold text-campus-700"
      >
        {match[1]}
      </sup>
    );
  });
}

function ConfidenceBadge({ confidence }: { confidence: number }) {
  const level =
    confidence >= 0.7 ? "high" : confidence >= 0.45 ? "medium" : "low";
  const styles = {
    high: "bg-green-50 text-green-700",
    medium: "bg-amber-50 text-amber-700",
    low: "bg-slate-100 text-slate-600",
  }[level];

  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${styles}`}
      title={`Retrieval confidence: ${confidence.toFixed(2)}`}
    >
      {level} confidence
    </span>
  );
}

export function MessageBubble({ turn }: { turn: ChatTurn }) {
  if (turn.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-campus-600 px-4 py-2.5 text-sm text-white">
          {turn.content}
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-start">
      <div className="w-full max-w-[90%] rounded-2xl rounded-bl-sm border border-slate-200 bg-white px-4 py-3 shadow-sm">
        {turn.pending ? (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <span className="inline-flex gap-1">
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400 [animation-delay:-0.3s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400 [animation-delay:-0.15s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400" />
            </span>
            Searching university documents…
          </div>
        ) : (
          <>
            {turn.answered === false && !turn.error && (
              <div className="mb-2 inline-flex rounded bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
                No confident answer found
              </div>
            )}

            <div className="whitespace-pre-wrap text-sm leading-relaxed text-slate-800">
              {renderWithCitations(turn.content)}
            </div>

            {turn.citations && <CitationList citations={turn.citations} />}

            <div className="mt-2 flex items-center gap-2">
              {turn.confidence !== undefined && turn.answered && (
                <ConfidenceBadge confidence={turn.confidence} />
              )}
            </div>

            {turn.messageId && !turn.error && <FeedbackButtons messageId={turn.messageId} />}
          </>
        )}
      </div>
    </div>
  );
}
