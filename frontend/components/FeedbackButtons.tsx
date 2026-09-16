"use client";

import { useState } from "react";

import { api } from "@/lib/api";

type Rating = 1 | -1;

/**
 * Thumbs up/down on an assistant answer.
 *
 * A thumbs-down opens an optional comment box: the rating alone says an answer
 * was wrong, the comment says how, which is what makes the signal usable for
 * retraining rather than just a counter.
 */
export function FeedbackButtons({ messageId }: { messageId: string }) {
  const [rating, setRating] = useState<Rating | null>(null);
  const [showComment, setShowComment] = useState(false);
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [commentSaved, setCommentSaved] = useState(false);

  async function submit(value: Rating, text?: string) {
    setSubmitting(true);
    setError(null);
    try {
      await api.sendFeedback(messageId, value, text);
      setRating(value);
      if (value === -1 && !text) setShowComment(true);
      if (text) {
        setShowComment(false);
        setCommentSaved(true);
      }
    } catch {
      setError("Could not save your feedback.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mt-3 border-t border-slate-100 pt-2">
      <div className="flex items-center gap-2">
        <span className="text-xs text-slate-500">Was this helpful?</span>

        <button
          type="button"
          disabled={submitting}
          onClick={() => submit(1)}
          aria-pressed={rating === 1}
          aria-label="Helpful"
          className={`rounded px-2 py-1 text-sm transition disabled:opacity-50 ${
            rating === 1
              ? "bg-green-100 text-green-700"
              : "text-slate-400 hover:bg-slate-100 hover:text-slate-600"
          }`}
        >
          Yes
        </button>

        <button
          type="button"
          disabled={submitting}
          onClick={() => submit(-1)}
          aria-pressed={rating === -1}
          aria-label="Not helpful"
          className={`rounded px-2 py-1 text-sm transition disabled:opacity-50 ${
            rating === -1
              ? "bg-red-100 text-red-700"
              : "text-slate-400 hover:bg-slate-100 hover:text-slate-600"
          }`}
        >
          No
        </button>

        {rating !== null && !showComment && (
          <span className="text-xs text-slate-400">
            {commentSaved ? "Thanks — noted." : "Thanks for the feedback."}
          </span>
        )}
      </div>

      {showComment && (
        <div className="mt-2">
          <textarea
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            placeholder="What was wrong with this answer? (optional)"
            rows={2}
            maxLength={2000}
            className="w-full rounded border border-slate-300 p-2 text-sm focus:border-campus-500 focus:outline-none"
          />
          <div className="mt-1 flex gap-2">
            <button
              type="button"
              disabled={submitting || comment.trim().length === 0}
              onClick={() => submit(-1, comment.trim())}
              className="rounded bg-campus-600 px-3 py-1 text-xs font-medium text-white hover:bg-campus-700 disabled:opacity-50"
            >
              Send
            </button>
            <button
              type="button"
              onClick={() => setShowComment(false)}
              className="rounded px-3 py-1 text-xs text-slate-500 hover:bg-slate-100"
            >
              Skip
            </button>
          </div>
        </div>
      )}

      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
    </div>
  );
}
