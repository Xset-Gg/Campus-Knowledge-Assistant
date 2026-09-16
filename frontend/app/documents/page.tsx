"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { DocumentRecord } from "@/types/api";

export default function DocumentsPage() {
  const { can } = useAuth();
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [departments, setDepartments] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.departments().then(setDepartments).catch(() => setDepartments([]));
  }, []);

  useEffect(() => {
    setLoading(true);
    api
      .documents({ department: filter || undefined, limit: 200 })
      .then(setDocuments)
      .catch(() => setError("Could not load documents."))
      .finally(() => setLoading(false));
  }, [filter]);

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-5xl">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-semibold text-slate-800">Document library</h1>
          <select
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-sm focus:border-campus-500 focus:outline-none"
          >
            <option value="">All departments</option>
            {departments.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <span className="ml-auto text-sm text-slate-500">
            {documents.length} document{documents.length === 1 ? "" : "s"} visible to you
          </span>
        </div>

        <p className="mt-1 text-sm text-slate-500">
          This list is filtered by your role — restricted documents are not shown.
        </p>

        {error && <p className="mt-4 text-sm text-red-600">{error}</p>}
        {loading ? (
          <p className="mt-6 text-sm text-slate-500">Loading…</p>
        ) : documents.length === 0 ? (
          <p className="mt-6 text-sm text-slate-500">No documents found.</p>
        ) : (
          <div className="mt-4 overflow-hidden rounded-lg border border-slate-200 bg-white">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-2">Title</th>
                  <th className="px-3 py-2">Department</th>
                  <th className="px-3 py-2">Type</th>
                  <th className="px-3 py-2">Year</th>
                  {can("manage_documents") && <th className="px-3 py-2">Access</th>}
                  <th className="px-3 py-2">Pages</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {documents.map((document) => (
                  <tr key={document.id} className="hover:bg-slate-50">
                    <td className="px-3 py-2">
                      {document.source_url ? (
                        <a
                          href={document.source_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-campus-700 hover:underline"
                        >
                          {document.title}
                        </a>
                      ) : (
                        document.title
                      )}
                      {!document.is_current && (
                        <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800">
                          superseded
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-slate-600">{document.department}</td>
                    <td className="px-3 py-2 text-slate-600">{document.document_type}</td>
                    <td className="px-3 py-2 text-slate-600">{document.academic_year}</td>
                    {can("manage_documents") && (
                      <td className="px-3 py-2">
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">
                          {document.access_level}
                        </span>
                      </td>
                    )}
                    <td className="px-3 py-2 text-slate-500">{document.page_count ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
