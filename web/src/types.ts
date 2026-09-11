export type Session = {
  session_id: string;
  user_id: string;
  expires_at: string;
};

export type Book = {
  book_id: string;
  title: string;
  format: "pdf" | "epub" | "txt" | "markdown";
  active_version_id: string | null;
  status: "active" | "deleting" | "deleted";
  created_at: string;
  row_version: number;
};

export type Chapter = {
  chapter_id: string;
  ordinal: number;
  title: string;
  source_locator: SourceLocator;
};

export type SourceLocator = {
  kind: "page" | "href" | "line" | "offset" | "synthetic";
  value: string;
};

export type Block = {
  block_id: string;
  chapter_id: string;
  ordinal: number;
  text: string;
  text_sha256: string;
  source_locator: SourceLocator;
};

export type Progress = {
  chapter_id: string;
  last_chunk_index: number;
  furthest_chunk_index: number;
  position: {
    chapter_id: string;
    block_id: string;
    block_offset: number;
    updated_at: string;
    device_id: string;
    row_version: number;
  };
  updated_at: string;
  row_version: number;
};

export type Highlight = {
  highlight_id: string;
  chapter_id: string;
  start: { block_id: string; offset: number };
  end: { block_id: string; offset: number };
  exact_quote: string;
  prefix: string;
  suffix: string;
  created_at: string;
};

export type Evidence = {
  evidence_id: string;
  chapter_id: string;
  chunk_id: string;
  chunk_index: number;
  block_ids: string[];
  quote: string;
  source_locator: SourceLocator;
};

export type Answer = {
  run_id: string;
  trace_id: string;
  question: string;
  answer: string;
  status: "accepted" | "running" | "completed" | "failed" | "cancelled";
  evidence: Evidence[];
  created_at: string;
  finished_at?: string | null;
  conversation_id?: string | null;
  intent?: IntentFrame | null;
};

export type IntentFrame = {
  route: "reader_action" | "book_dialogue" | "open_dialogue" | "clarification";
  relation: "new" | "followup" | "confused" | "correction";
  goals: string[];
  context_sources: string[];
  scope: "open" | "current_book" | "passage" | "external" | "system" | "mixed" | "ambiguous";
  tasks: Array<"discuss" | "explain" | "example" | "summarize" | "analyze" | "compare" | "critique" | "quiz" | "note" | "navigate" | "settings">;
  targets: Array<{
    kind: "selection" | "highlight" | "current_chapter" | "previous_turn" | "user_text" | "unspecified";
    identifier: string | null;
    explicit: boolean;
  }>;
  context_needs: Array<"quoted_text" | "surrounding_book" | "current_book" | "recent_turns" | "user_preferences" | "external_sources">;
  external_access: "not_needed" | "recommended" | "required";
  clarification_required: boolean;
  confidence: number;
  requires_evidence: boolean;
  resolved_by: "deterministic" | "semantic_model" | "degraded_fallback";
  routing_ms: number;
  router_model: string | null;
  degraded_reason: "model_unavailable" | "model_error" | "invalid_response" | null;
};

export type BookType = "general" | "philosophy" | "history" | "social_science" | "science" | "practical" | "fiction";

export type Companion = {
  user_skill: {
    role: "teacher" | "friend" | "peer";
    tone: "gentle" | "concise" | "rigorous";
    depth: "quick" | "balanced" | "deep";
    custom_instructions: string;
    long_term_memory_enabled: boolean;
    spoiler_protection: boolean;
    voice_rate: number;
  };
  detected_book_type: BookType;
  effective_book_type: BookType;
  book_type_label: string;
  confidence: number;
  is_overridden: boolean;
  skill_summary: string;
  skill_version: string;
  classification_source: "semantic_model" | "degraded_fallback";
};

export type ReadingMemory = {
  memory_id: string;
  source_run_id: string;
  kind: "discussion";
  summary: string;
  created_at: string;
};

export type ApiErrorShape = {
  error?: {
    code?: string;
    message?: string;
    retryable?: boolean;
  };
};
