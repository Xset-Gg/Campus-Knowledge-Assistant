export type UserRole = "student" | "professor" | "admin";

export type AccessLevel = "public" | "student" | "faculty" | "admin";

export type DocumentType =
  | "syllabus"
  | "policy"
  | "handbook"
  | "form"
  | "announcement"
  | "other";

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: UserRole;
  department: string | null;
  is_active: boolean;
}

export interface AuthToken {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface Citation {
  marker: number;
  chunk_id: string;
  document_id: string;
  document_title: string;
  page_number: number | null;
  section_path: string | null;
  department: string;
  academic_year: number;
  document_type: DocumentType;
  source_url: string | null;
  snippet: string;
  relevance_score: number;
  is_outdated: boolean;
}

export interface AskResponse {
  message_id: string;
  session_id: string;
  answer: string;
  citations: Citation[];
  confidence: number;
  answered: boolean;
  latency_ms: number;
  trace_id: string | null;
}

export interface ChatSession {
  id: string;
  title: string | null;
  created_at: string;
}

export interface ChatMessageRecord {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[] | null;
  confidence: number | null;
  created_at: string;
}

export interface DocumentRecord {
  id: string;
  title: string;
  file_name: string;
  department: string;
  academic_year: number;
  document_type: DocumentType;
  access_level: AccessLevel;
  is_current: boolean;
  source_url: string | null;
  page_count: number | null;
  ingestion_status: string;
  created_at: string;
}

export interface AnalyticsSummary {
  total_questions: number;
  unanswered_questions: number;
  answer_rate: number;
  thumbs_up: number;
  thumbs_down: number;
  avg_latency_ms: number | null;
  document_count: number;
  chunk_count: number;
}

export interface FailedSearch {
  id: string;
  query: string;
  top_score: number | null;
  result_count: number | null;
  created_at: string;
}

/** A chat turn as held in client state, before/after the API round-trip. */
export interface ChatTurn {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  confidence?: number;
  answered?: boolean;
  messageId?: string;
  pending?: boolean;
  error?: boolean;
}
