import type {
  AnalyticsSummary,
  AskResponse,
  AuthToken,
  ChatMessageRecord,
  ChatSession,
  DocumentRecord,
  FailedSearch,
  User,
} from "@/types/api";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const TOKEN_KEY = "cka_token";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch {
    throw new ApiError("Could not reach the server. Check your connection.", 0);
  }

  if (response.status === 401) {
    // The token is expired or invalid — drop it so the UI returns to login
    // rather than looping on requests that can never succeed.
    clearToken();
    throw new ApiError("Your session has expired. Please sign in again.", 401);
  }

  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Response had no JSON body; keep the generic message.
    }
    throw new ApiError(detail, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  login: (email: string, password: string) =>
    request<AuthToken>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  register: (payload: {
    email: string;
    password: string;
    full_name: string;
    department?: string;
  }) =>
    request<User>("/auth/register", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  me: () => request<User>("/auth/me"),

  capabilities: () =>
    request<{ role: string; capabilities: string[] }>("/auth/me/capabilities"),

  ask: (payload: {
    question: string;
    session_id?: string | null;
    department?: string | null;
    include_outdated?: boolean;
  }) =>
    request<AskResponse>("/chat/ask", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  sessions: () => request<ChatSession[]>("/chat/sessions"),

  messages: (sessionId: string) =>
    request<ChatMessageRecord[]>(`/chat/sessions/${sessionId}/messages`),

  sendFeedback: (messageId: string, rating: 1 | -1, comment?: string) =>
    request<{ id: string }>("/feedback", {
      method: "POST",
      body: JSON.stringify({ message_id: messageId, rating, comment }),
    }),

  documents: (params: { department?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.department) query.set("department", params.department);
    if (params.limit) query.set("limit", String(params.limit));
    const suffix = query.toString() ? `?${query}` : "";
    return request<DocumentRecord[]>(`/documents${suffix}`);
  },

  departments: () => request<string[]>("/documents/departments"),

  analytics: () => request<AnalyticsSummary>("/admin/analytics"),

  failedSearches: (limit = 50) =>
    request<FailedSearch[]>(`/admin/failed-searches?limit=${limit}`),

  topFailedQueries: () =>
    request<{ query: string; count: number }[]>("/admin/failed-searches/top"),

  outdatedDocuments: () =>
    request<
      {
        id: string;
        title: string;
        department: string;
        academic_year: number;
        years_behind: number;
      }[]
    >("/admin/outdated-documents"),

  uploadDocument: (form: FormData) =>
    request<DocumentRecord>("/documents/upload", { method: "POST", body: form }),
};
