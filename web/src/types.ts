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
};

export type ApiErrorShape = {
  error?: {
    code?: string;
    message?: string;
    retryable?: boolean;
  };
};
