"use client";

import { useEffect, useRef, useState } from "react";

import { MessageBubble } from "@/components/MessageBubble";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { ChatTurn } from "@/types/api";

const SUGGESTIONS = [
  "What is the late submission penalty for assignments?",
  "When is the deadline to withdraw from a course?",
  "How many credits do I need to graduate?",
  "What GPA do I need to keep my scholarship?",
];

export function ChatWindow() {
  const { user } = useAuth();
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [departments, setDepartments] = useState<string[]>([]);
  const [department, setDepartment] = useState<string>("");
  const [includeOutdated, setIncludeOutdated] = useState(false);
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.departments().then(setDepartments).catch(() => setDepartments([]));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  async function send(question: string) {
    const trimmed = question.trim();
    if (!trimmed || busy) return;

    const userTurn: ChatTurn = {
      id: `u-${Date.now()}`,
      role: "user",
      content: trimmed,
    };
    const placeholderId = `a-${Date.now()}`;
    setTurns((prev) => [
      ...prev,
      userTurn,
      { id: placeholderId, role: "assistant", content: "", pending: true },
    ]);
    setInput("");
    setBusy(true);

    try {
      const response = await api.ask({
        question: trimmed,
        session_id: sessionId,
        department: department || null,
        include_outdated: includeOutdated,
      });
      setSessionId(response.session_id);
      setTurns((prev) =>
        prev.map((turn) =>
          turn.id === placeholderId
            ? {
                id: placeholderId,
                role: "assistant",
                content: response.answer,
                citations: response.citations,
                confidence: response.confidence,
                answered: response.answered,
                messageId: response.message_id,
              }
            : turn,
        ),
      );
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : "Something went wrong. Please try again.";
      setTurns((prev) =>
        prev.map((turn) =>
          turn.id === placeholderId
            ? { id: placeholderId, role: "assistant", content: message, error: true }
            : turn,
        ),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-slate-200 bg-white px-4 py-2.5 text-sm">
        <label className="flex items-center gap-1.5">
          <span className="text-slate-500">Department</span>
          <select
            value={department}
            onChange={(event) => setDepartment(event.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-sm focus:border-campus-500 focus:outline-none"
          >
            <option value="">All</option>
            {departments.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-1.5 text-slate-500">
          <input
            type="checkbox"
            checked={includeOutdated}
            onChange={(event) => setIncludeOutdated(event.target.checked)}
            className="rounded border-slate-300"
          />
          Include previous years
        </label>

        {user && (
          <span className="ml-auto rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
            Answering as {user.role}
          </span>
        )}
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto bg-slate-50 p-4">
        {turns.length === 0 ? (
          <div className="mx-auto mt-8 max-w-lg text-center">
            <h2 className="text-lg font-semibold text-slate-800">
              Ask about university policies and courses
            </h2>
            <p className="mt-1 text-sm text-slate-500">
              Answers come only from official university documents, with a link to the
              exact page. If the documents do not cover your question, you will be told
              so rather than given a guess.
            </p>
            <div className="mt-5 grid gap-2 text-left">
              {SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => send(suggestion)}
                  className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 transition hover:border-campus-500 hover:text-campus-700"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        ) : (
          turns.map((turn) => <MessageBubble key={turn.id} turn={turn} />)
        )}
        <div ref={bottomRef} />
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void send(input);
        }}
        className="border-t border-slate-200 bg-white p-3"
      >
        <div className="flex gap-2">
          <input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Ask about a policy, deadline, or course…"
            maxLength={2000}
            disabled={busy}
            className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-campus-500 focus:outline-none disabled:bg-slate-50"
          />
          <button
            type="submit"
            disabled={busy || input.trim().length === 0}
            className="rounded-lg bg-campus-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-campus-700 disabled:opacity-40"
          >
            {busy ? "Asking…" : "Ask"}
          </button>
        </div>
      </form>
    </div>
  );
}
