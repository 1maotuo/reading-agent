import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { ApiError, api, apiAll, requestId } from "./api";
import { ArrowIcon, BookIcon, CloseIcon, MenuIcon, PanelIcon, QuoteIcon, SendIcon, SettingsIcon, SparkIcon, StopIcon, UploadIcon } from "./icons";
import type { Answer, Block, Book, BookType, Chapter, Companion, Evidence, Highlight, Progress, ReadingMemory, Session } from "./types";

type ViewState = "library" | "reader";
type SelectionDraft = {
  block: Block;
  start: number;
  end: number;
  quote: string;
};

type ReaderPreferences = {
  theme: "paper" | "night";
  brightness: number;
  fontSize: number;
  lineHeight: number;
  contentWidth: number;
};

const DEFAULT_READER_PREFERENCES: ReaderPreferences = {
  theme: "paper",
  brightness: 100,
  fontSize: 20,
  lineHeight: 2,
  contentWidth: 740
};

function storedNumber(key: string, fallback: number): number {
  const raw = window.localStorage.getItem(key);
  if (raw === null) return fallback;
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

type SpeechResultEvent = { resultIndex: number; results: { [index: number]: { 0: { transcript: string }; isFinal: boolean }; length: number } };
type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: SpeechResultEvent) => void) | null;
  onerror: (() => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};
type SpeechWindow = Window & typeof globalThis & { webkitSpeechRecognition?: new () => SpeechRecognitionLike };

const FORMAT_LABEL: Record<Book["format"], string> = {
  pdf: "PDF",
  epub: "EPUB",
  txt: "TXT",
  markdown: "Markdown"
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const known: Record<string, string> = {
      unauthenticated: "账号或密码不正确",
      unsupported_format: "目前只支持 PDF、EPUB、TXT 和 Markdown",
      invalid_input: "文件或输入内容无法处理，请检查后重试",
      book_not_ready: "书籍还在解析中，请稍后再打开",
      anchor_invalid: "这段划线已经无法与原文对应，请重新选择",
      evidence_required: "当前没有足够原文证据回答这个问题",
      version_conflict: "阅读进度已在其他页面更新，已为你重新同步"
    };
    return known[error.code] ?? error.message;
  }
  return "刚刚没有成功，请稍后再试";
}

function extensionFormat(file: File): Book["format"] | null {
  const suffix = file.name.split(".").pop()?.toLowerCase();
  if (suffix === "pdf" || suffix === "epub" || suffix === "txt") return suffix;
  if (suffix === "md" || suffix === "markdown") return "markdown";
  return null;
}

async function textHash(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function inlineAnswer(text: string) {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={index}>{part.slice(1, -1)}</code>;
    return part;
  });
}

function AnswerText({ text }: { text: string }) {
  return (
    <div className="answer-copy">
      {text.split(/\r?\n/).map((line, index) => {
        const trimmed = line.trim();
        if (!trimmed) return <span className="answer-space" key={index} />;
        if (/^#{1,3}\s/.test(trimmed)) return <h4 key={index}>{inlineAnswer(trimmed.replace(/^#{1,3}\s+/, ""))}</h4>;
        if (/^[-*]\s+/.test(trimmed)) return <p className="answer-list" key={index}>{inlineAnswer(trimmed.replace(/^[-*]\s+/, ""))}</p>;
        if (/^\d+[.)]\s+/.test(trimmed)) return <p className="answer-list ordered" key={index}>{inlineAnswer(trimmed)}</p>;
        return <p key={index}>{inlineAnswer(line)}</p>;
      })}
    </div>
  );
}

function Logo() {
  return <div className="brand"><span className="brand-mark">页</span><span>页伴</span></div>;
}

function LoginView({ onLogin }: { onLogin: (session: Session) => void }) {
  const [identifier, setIdentifier] = useState("reader@example.local");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const session = await api<Session>("/api/v1/sessions", {
        method: "POST",
        body: JSON.stringify({ identifier, password })
      });
      onLogin(session);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-intro">
        <Logo />
        <div className="intro-copy">
          <p className="eyebrow">READ WITH EVIDENCE</p>
          <h1>难读的地方，<br />有人陪你慢慢想。</h1>
          <p>不替你读完一本书，只在需要的时候，把原文、上下文和你的问题放在一起。</p>
        </div>
        <p className="intro-footnote">私有书籍 · 原文证据 · 按需解释</p>
      </section>
      <section className="login-panel">
        <form className="login-card" onSubmit={submit}>
          <div>
            <p className="eyebrow">欢迎回来</p>
            <h2>继续上次的阅读</h2>
          </div>
          <label>邮箱<input type="email" autoComplete="username" value={identifier} onChange={(event) => setIdentifier(event.target.value)} placeholder="reader@example.local" /></label>
          <label>密码<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="开发预览密码：reading-demo" /></label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="primary-button" disabled={busy || !identifier || !password}>{busy ? "正在进入…" : "进入书架"}<ArrowIcon /></button>
          <p className="preview-note">当前为本地开发预览，正式发布前将替换为生产账号与加密存储。</p>
        </form>
      </section>
    </main>
  );
}

function LibraryView({ books, onOpen, onRefresh, onLogout }: { books: Book[]; onOpen: (book: Book) => void; onRefresh: () => Promise<void>; onLogout: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");

  async function upload(file?: File) {
    if (!file) return;
    const format = extensionFormat(file);
    if (!format) {
      setUploadError("请选择 PDF、EPUB、TXT 或 Markdown 文件");
      return;
    }
    setUploading(true);
    setUploadError("");
    try {
      const form = new FormData();
      form.set("title", file.name.replace(/\.(pdf|epub|txt|md|markdown)$/i, ""));
      form.set("format", format);
      form.set("file", file);
      const created = await api<{ book_id: string; job_id: string }>("/api/v1/books", {
        method: "POST",
        body: form,
        headers: { "Idempotency-Key": requestId() }
      });
      const job = await api<{ status: string; error_code?: string }>(`/api/v1/jobs/${created.job_id}`);
      if (job.status !== "succeeded") throw new ApiError("书籍解析失败", job.error_code ?? "invalid_input", 422);
      await onRefresh();
      const ready = await api<Book>(`/api/v1/books/${created.book_id}`);
      onOpen(ready);
    } catch (reason) {
      setUploadError(errorMessage(reason));
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <div className="library-shell">
      <aside className="library-sidebar">
        <Logo />
        <nav><button className="nav-button active"><BookIcon />我的书架</button></nav>
        <div className="sidebar-bottom"><span className="avatar">读</span><div><strong>本地读者</strong><small>开发预览</small></div><button onClick={onLogout}>退出</button></div>
      </aside>
      <main className="library-main">
        <header className="library-header"><div><p className="eyebrow">你的阅读空间</p><h1>今天想读点什么？</h1></div><button className="primary-button compact" onClick={() => fileRef.current?.click()} disabled={uploading}><UploadIcon />{uploading ? "正在解析…" : "导入书籍"}</button></header>
        <input ref={fileRef} hidden type="file" accept=".pdf,.epub,.txt,.md,.markdown" onChange={(event) => upload(event.target.files?.[0])} />
        {uploadError && <div className="inline-error" role="alert"><span>{uploadError}</span><button onClick={() => setUploadError("")}><CloseIcon /></button></div>}
        {books.length === 0 ? (
          <button className="empty-library" onClick={() => fileRef.current?.click()}>
            <span className="empty-icon"><UploadIcon /></span><strong>导入你的第一本书</strong><span>支持 PDF、EPUB、TXT 和 Markdown，文件只用于你的私人阅读。</span>
          </button>
        ) : (
          <section className="book-section">
            <div className="section-heading"><h2>最近阅读</h2><span>{books.length} 本</span></div>
            <div className="book-grid">
              {books.map((book, index) => (
                <button className="book-card" key={book.book_id} onClick={() => onOpen(book)}>
                  <span className={`book-cover tone-${index % 4}`}><small>{FORMAT_LABEL[book.format]}</small><strong>{book.title}</strong><i>页伴阅读</i></span>
                  <span className="book-meta"><strong>{book.title}</strong><small>{book.active_version_id ? "可以阅读" : "正在处理"}</small></span>
                </button>
              ))}
              <button className="book-card add-book" onClick={() => fileRef.current?.click()}><span><UploadIcon /><strong>继续添加</strong><small>把下一本书带进来</small></span></button>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}

function offsetInside(root: HTMLElement, node: Node, offset: number): number {
  const range = document.createRange();
  range.selectNodeContents(root);
  range.setEnd(node, offset);
  return range.toString().length;
}

function ReaderView({ book, onBack }: { book: Book; onBack: () => void }) {
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [chapterId, setChapterId] = useState("");
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [progress, setProgress] = useState<Progress | null>(null);
  const progressRef = useRef<Progress | null>(null);
  const [highlights, setHighlights] = useState<Highlight[]>([]);
  const [answers, setAnswers] = useState<Answer[]>([]);
  const [question, setQuestion] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [liveAnswer, setLiveAnswer] = useState("");
  const [pendingQuestion, setPendingQuestion] = useState("");
  const [answerPhase, setAnswerPhase] = useState("正在理解你的问题");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [selection, setSelection] = useState<SelectionDraft | null>(null);
  const [notice, setNotice] = useState("");
  const [mobilePane, setMobilePane] = useState<"toc" | "text" | "ai">("text");
  const [companion, setCompanion] = useState<Companion | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [bookTypeChoice, setBookTypeChoice] = useState<BookType | "auto">("auto");
  const [savingSkill, setSavingSkill] = useState(false);
  const [listening, setListening] = useState(false);
  const [memories, setMemories] = useState<ReadingMemory[]>([]);
  const [tocOpen, setTocOpen] = useState(() => window.localStorage.getItem("reader.tocOpen") !== "false");
  const [aiOpen, setAiOpen] = useState(() => window.localStorage.getItem("reader.aiOpen") !== "false");
  const [aiWidth, setAiWidth] = useState(() => Math.max(320, Math.min(620, storedNumber("reader.aiWidth", 390))));
  const [readerPreferences, setReaderPreferences] = useState<ReaderPreferences>(() => ({
    theme: window.localStorage.getItem("reader.theme") === "night" ? "night" : "paper",
    brightness: Math.max(70, Math.min(115, storedNumber("reader.brightness", 100))),
    fontSize: Math.max(16, Math.min(28, storedNumber("reader.fontSize", 20))),
    lineHeight: Math.max(1.6, Math.min(2.4, storedNumber("reader.lineHeight", 2))),
    contentWidth: Math.max(560, Math.min(920, storedNumber("reader.contentWidth", 740)))
  }));
  const blockElements = useRef(new Map<string, HTMLElement>());
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const pendingEvidenceBlock = useRef<string | null>(null);
  const saveTimer = useRef<number | undefined>(undefined);
  const eventSourceRef = useRef<EventSource | null>(null);

  useEffect(() => { progressRef.current = progress; }, [progress]);

  useEffect(() => {
    window.localStorage.setItem("reader.tocOpen", String(tocOpen));
    window.localStorage.setItem("reader.aiOpen", String(aiOpen));
    window.localStorage.setItem("reader.aiWidth", String(aiWidth));
  }, [tocOpen, aiOpen, aiWidth]);

  useEffect(() => {
    for (const [key, value] of Object.entries(readerPreferences)) {
      window.localStorage.setItem(`reader.${key}`, String(value));
    }
  }, [readerPreferences]);

  const loadHistory = useCallback(async () => {
    const [result, loadedMemories] = await Promise.all([
      api<{ items: Answer[] }>(`/api/v1/books/${book.book_id}/answers`),
      api<ReadingMemory[]>(`/api/v1/books/${book.book_id}/memories`)
    ]);
    setAnswers(result.items);
    setMemories(loadedMemories);
    const latest = [...result.items].reverse().find((item) => item.status === "completed" && item.conversation_id);
    setConversationId(latest?.conversation_id ?? null);
  }, [book.book_id]);

  useEffect(() => {
    let active = true;
    Promise.all([
      apiAll<Chapter>(`/api/v1/books/${book.book_id}/chapters?limit=100`),
      api<{ items: Highlight[] }>(`/api/v1/books/${book.book_id}/highlights`),
      api<{ items: Answer[] }>(`/api/v1/books/${book.book_id}/answers`)
    ]).then(([loadedChapters, highlightPage, answerPage]) => {
      if (!active) return;
      setChapters(loadedChapters);
      setChapterId(loadedChapters[0]?.chapter_id ?? "");
      setHighlights(highlightPage.items);
      setAnswers(answerPage.items);
      const latest = [...answerPage.items].reverse().find((item) => item.status === "completed" && item.conversation_id);
      setConversationId(latest?.conversation_id ?? null);
    }).catch((reason) => setNotice(errorMessage(reason)));
    return () => { active = false; };
  }, [book.book_id]);

  useEffect(() => {
    Promise.all([
      api<Companion>(`/api/v1/books/${book.book_id}/companion`),
      api<ReadingMemory[]>(`/api/v1/books/${book.book_id}/memories`)
    ]).then(([value, loadedMemories]) => {
      setCompanion(value);
      setBookTypeChoice(value.is_overridden ? value.effective_book_type : "auto");
      setMemories(loadedMemories);
    }).catch((reason) => setNotice(errorMessage(reason)));
  }, [book.book_id]);

  useEffect(() => () => {
    recognitionRef.current?.stop();
    window.speechSynthesis?.cancel();
    eventSourceRef.current?.close();
  }, []);

  async function saveCompanion(event: React.FormEvent) {
    event.preventDefault();
    if (!companion || savingSkill) return;
    setSavingSkill(true);
    try {
      const value = await api<Companion>(`/api/v1/books/${book.book_id}/companion`, {
        method: "PUT",
        body: JSON.stringify({
          ...companion.user_skill,
          book_type: bookTypeChoice === "auto" ? null : bookTypeChoice
        })
      });
      setCompanion(value);
      setSettingsOpen(false);
      setNotice("阅读方式已保存，下一轮回答会使用新设置");
    } catch (reason) {
      setNotice(errorMessage(reason));
    } finally {
      setSavingSkill(false);
    }
  }

  function beginListening() {
    const SpeechRecognition = (window as SpeechWindow).webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setNotice("当前浏览器不支持语音转文字，请继续使用键盘输入");
      return;
    }
    const recognition = new SpeechRecognition();
    recognition.lang = "zh-CN";
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.onresult = (event) => {
      let transcript = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        if (event.results[index].isFinal) transcript += event.results[index][0].transcript;
      }
      if (transcript.trim()) setQuestion((current) => `${current}${current.trim() ? " " : ""}${transcript.trim()}`);
    };
    recognition.onerror = () => setNotice("没有听清，请重试或改用键盘输入");
    recognition.onend = () => setListening(false);
    recognitionRef.current = recognition;
    setListening(true);
    try { recognition.start(); } catch { setListening(false); }
  }

  function endListening() {
    recognitionRef.current?.stop();
    recognitionRef.current = null;
    setListening(false);
  }

  function speak(text: string) {
    if (!("speechSynthesis" in window)) {
      setNotice("当前浏览器不支持语音播放");
      return;
    }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "zh-CN";
    utterance.rate = companion?.user_skill.voice_rate ?? 0.95;
    window.speechSynthesis.speak(utterance);
  }

  useEffect(() => {
    if (!chapterId) return;
    let active = true;
    Promise.all([
      apiAll<Block>(`/api/v1/books/${book.book_id}/chapters/${chapterId}/blocks?limit=100`),
      api<Progress>(`/api/v1/books/${book.book_id}/progress?chapter_id=${chapterId}`)
    ]).then(([loadedBlocks, loadedProgress]) => {
      if (!active) return;
      setBlocks(loadedBlocks);
      setProgress(loadedProgress);
      window.setTimeout(() => {
        const target = pendingEvidenceBlock.current ?? loadedProgress.position.block_id;
        blockElements.current.get(target)?.scrollIntoView({ block: "center", behavior: pendingEvidenceBlock.current ? "smooth" : "auto" });
        pendingEvidenceBlock.current = null;
      }, 80);
    }).catch((reason) => setNotice(errorMessage(reason)));
    return () => { active = false; };
  }, [book.book_id, chapterId]);

  const savePosition = useCallback((block: Block) => {
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(async () => {
      const current = progressRef.current;
      if (!current || current.chapter_id !== block.chapter_id) return;
      const now = new Date().toISOString();
      try {
        const saved = await api<Progress>(`/api/v1/books/${book.book_id}/progress`, {
          method: "PUT",
          headers: { "If-Match": String(current.row_version) },
          body: JSON.stringify({
            chapter_id: block.chapter_id,
            last_chunk_index: block.ordinal,
            furthest_chunk_index: Math.max(current.furthest_chunk_index, block.ordinal),
            position: {
              chapter_id: block.chapter_id,
              block_id: block.block_id,
              block_offset: 0,
              updated_at: now,
              device_id: "web-preview",
              row_version: current.row_version
            },
            device_id: "web-preview"
          })
        });
        progressRef.current = saved;
        setProgress(saved);
      } catch (reason) {
        if (reason instanceof ApiError && reason.code === "version_conflict") {
          const fresh = await api<Progress>(`/api/v1/books/${book.book_id}/progress?chapter_id=${block.chapter_id}`);
          progressRef.current = fresh;
          setProgress(fresh);
        }
      }
    }, 350);
  }, [book.book_id]);

  useEffect(() => {
    if (!blocks.length) return;
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((item) => item.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;
      const block = blocks.find((item) => item.block_id === (visible.target as HTMLElement).dataset.blockId);
      if (block) savePosition(block);
    }, { root: document.querySelector(".reading-scroll"), threshold: [0.55, 0.8] });
    blockElements.current.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [blocks, savePosition]);

  function captureSelection() {
    const native = window.getSelection();
    if (!native || native.isCollapsed || native.rangeCount !== 1) return;
    const range = native.getRangeAt(0);
    const startElement = (range.startContainer.nodeType === Node.ELEMENT_NODE ? range.startContainer as Element : range.startContainer.parentElement)?.closest<HTMLElement>("[data-block-id]");
    const endElement = (range.endContainer.nodeType === Node.ELEMENT_NODE ? range.endContainer as Element : range.endContainer.parentElement)?.closest<HTMLElement>("[data-block-id]");
    if (!startElement || startElement !== endElement) {
      setNotice("第一版先支持同一段落内划线，请缩小选择范围");
      return;
    }
    const block = blocks.find((item) => item.block_id === startElement.dataset.blockId);
    if (!block) return;
    const start = offsetInside(startElement, range.startContainer, range.startOffset);
    const end = offsetInside(startElement, range.endContainer, range.endOffset);
    const quote = block.text.slice(start, end);
    if (!quote.trim()) return;
    setSelection({ block, start, end, quote });
    setAiOpen(true);
    setMobilePane("ai");
    window.getSelection()?.removeAllRanges();
  }

  async function ask(text: string) {
    const clean = text.trim();
    if (!clean || asking) return;
    const submittedSelection = selection;
    const draftBeforeSubmit = text;
    setAsking(true);
    setLiveAnswer("");
    setPendingQuestion(clean);
    setQuestion("");
    setAnswerPhase("正在理解你的问题");
    setNotice("");
    setAiOpen(true);
    setMobilePane("ai");
    try {
      const selectionContext = submittedSelection ? {
        chapter_id: submittedSelection.block.chapter_id,
        block_id: submittedSelection.block.block_id,
        start_offset: submittedSelection.start,
        end_offset: submittedSelection.end,
        exact_quote: submittedSelection.quote,
        text_sha256: await textHash(submittedSelection.quote)
      } : null;
      const run = await api<{ run_id: string; conversation_id: string }>(`/api/v1/books/${book.book_id}/questions`, {
        method: "POST",
        headers: { "Idempotency-Key": requestId() },
        body: JSON.stringify({
          question: clean,
          selection_context: selectionContext,
          current_chapter_id: chapterId || null,
          conversation_id: conversationId,
          client_request_id: requestId()
        })
      });
      setActiveRunId(run.run_id);
      if (submittedSelection === selection) setSelection(null);
      await new Promise<void>((resolve, reject) => {
        const source = new EventSource(`/api/v1/answer-runs/${run.run_id}/events`, { withCredentials: true });
        eventSourceRef.current = source;
        source.addEventListener("status", (event) => {
          const payload = JSON.parse((event as MessageEvent).data).payload;
          setAnswerPhase(payload.label);
        });
        source.addEventListener("tool_started", () => setAnswerPhase("正在查找相关原文"));
        source.addEventListener("evidence", () => setAnswerPhase("正在依据原文组织回答"));
        source.addEventListener("answer_delta", (event) => {
          const payload = JSON.parse((event as MessageEvent).data).payload;
          setLiveAnswer((current) => current + payload.text_delta);
        });
        source.addEventListener("completed", async () => {
          source.close();
          eventSourceRef.current = null;
          setConversationId(run.conversation_id);
          try { await loadHistory(); } finally { resolve(); }
        });
        source.addEventListener("cancelled", () => {
          source.close();
          eventSourceRef.current = null;
          resolve();
        });
        source.addEventListener("failed", (event) => {
          const payload = JSON.parse((event as MessageEvent).data).payload;
          source.close();
          eventSourceRef.current = null;
          reject(new ApiError(payload.error?.message ?? "回答失败", payload.error?.code));
        });
        source.onerror = () => {
          source.close();
          eventSourceRef.current = null;
          reject(new ApiError("回答连接中断，请重试", "stream_interrupted"));
        };
      });
    } catch (reason) {
      setNotice(errorMessage(reason));
      setQuestion((current) => current.trim() ? current : draftBeforeSubmit);
      if (submittedSelection) setSelection((current) => current ?? submittedSelection);
    } finally {
      setAsking(false);
      setLiveAnswer("");
      setPendingQuestion("");
      setActiveRunId(null);
    }
  }

  async function stopAnswer() {
    if (!activeRunId) return;
    try {
      setAnswerPhase("正在停止生成");
      await api(`/api/v1/answer-runs/${activeRunId}/cancel`, { method: "POST" });
    } catch (reason) {
      setNotice(errorMessage(reason));
    }
  }

  function beginPanelResize(event: React.PointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = aiWidth;
    const move = (moveEvent: PointerEvent) => {
      setAiWidth(Math.max(320, Math.min(620, startWidth + startX - moveEvent.clientX)));
    };
    const finish = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish);
  }

  async function clearMemories() {
    if (!window.confirm("清除这本书的全部长期记忆？原文、划线和对话不会删除。")) return;
    try {
      await api(`/api/v1/books/${book.book_id}/memories`, { method: "DELETE" });
      setMemories([]);
      setNotice("这本书的长期记忆已清除");
    } catch (reason) {
      setNotice(errorMessage(reason));
    }
  }

  function jumpToEvidence(evidence: Evidence) {
    if (evidence.chapter_id !== chapterId) {
      pendingEvidenceBlock.current = evidence.block_ids[0];
      setChapterId(evidence.chapter_id);
    } else {
      blockElements.current.get(evidence.block_ids[0])?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    setMobilePane("text");
  }

  const currentChapter = chapters.find((item) => item.chapter_id === chapterId);
  const highlightedBlocks = useMemo(() => new Set(highlights.flatMap((item) => [item.start.block_id, item.end.block_id])), [highlights]);
  const currentOrdinal = progress?.position.block_id ? blocks.findIndex((item) => item.block_id === progress.position.block_id) : 0;
  const percent = blocks.length ? Math.max(1, Math.round(((currentOrdinal + 1) / blocks.length) * 100)) : 0;
  const paperBase = readerPreferences.theme === "night" ? [31, 32, 29] : [251, 250, 246];
  const brightnessFactor = readerPreferences.brightness / 100;
  const readerPaper = `rgb(${paperBase.map((channel) => Math.max(0, Math.min(255, Math.round(channel * brightnessFactor)))).join(",")})`;
  const readerStyle = {
    "--toc-width": tocOpen ? "244px" : "0px",
    "--ai-width": aiOpen ? `${aiWidth}px` : "0px",
    "--reader-font-size": `${readerPreferences.fontSize}px`,
    "--reader-line-height": String(readerPreferences.lineHeight),
    "--reader-content-width": `${readerPreferences.contentWidth}px`,
    "--reader-paper": readerPaper
  } as CSSProperties;

  return (
    <div className={`reader-shell pane-${mobilePane} theme-${readerPreferences.theme} ${tocOpen ? "" : "toc-closed"} ${aiOpen ? "" : "ai-closed"}`} style={readerStyle}>
      <header className="reader-topbar">
        <button className="panel-toggle desktop-only" onClick={() => setTocOpen((value) => !value)} aria-label={tocOpen ? "收起目录" : "打开目录"}><PanelIcon /></button>
        <button className="icon-button back-button" onClick={onBack} aria-label="返回书架"><ArrowIcon /></button>
        <button className="mobile-menu" onClick={() => setMobilePane("toc")}><MenuIcon /></button>
        <div className="reader-title"><strong>{book.title}</strong><span>{currentChapter?.title ?? "正在载入"}</span></div>
        <div className="reading-progress"><span style={{ width: `${percent}%` }} /><small>{percent}%</small></div>
        <button className="panel-toggle desktop-only" onClick={() => setAiOpen((value) => !value)} aria-label={aiOpen ? "收起一起读" : "打开一起读"}><SparkIcon /></button>
        <button className="ai-mobile-button" onClick={() => setMobilePane("ai")}><SparkIcon />一起读</button>
      </header>
      <aside className={`toc-panel ${tocOpen ? "" : "collapsed"}`}>
        <div className="panel-head"><Logo /><button className="mobile-close" onClick={() => setMobilePane("text")}><CloseIcon /></button></div>
        <button className="back-to-library" onClick={onBack}><ArrowIcon />返回书架</button>
        <p className="panel-label">目录</p>
        <nav className="chapter-list">
          {chapters.map((chapter) => <button key={chapter.chapter_id} className={chapter.chapter_id === chapterId ? "active" : ""} onClick={() => { setChapterId(chapter.chapter_id); setMobilePane("text"); }}><span>{String(chapter.ordinal + 1).padStart(2, "0")}</span>{chapter.title}</button>)}
        </nav>
        <div className="toc-footer"><button onClick={() => setSettingsOpen(true)}><SettingsIcon />阅读设置</button></div>
      </aside>
      <main className="reading-scroll" onMouseUp={captureSelection}>
        <article className="reading-page">
          <header><p className="chapter-kicker">CHAPTER {String((currentChapter?.ordinal ?? 0) + 1).padStart(2, "0")}</p><h1>{currentChapter?.title}</h1><span className="chapter-rule" /></header>
          {blocks.map((block) => (
            <p
              key={block.block_id}
              ref={(element) => { if (element) blockElements.current.set(block.block_id, element); else blockElements.current.delete(block.block_id); }}
              data-block-id={block.block_id}
              className={`reader-block ${highlightedBlocks.has(block.block_id) ? "has-highlight" : ""}`}
              onClick={() => savePosition(block)}
            >{block.text}</p>
          ))}
          {!blocks.length && <div className="loading-copy">正在整理这一章…</div>}
        </article>
      </main>
      <aside className={`ai-panel ${aiOpen ? "" : "collapsed"}`}>
        <div className="panel-resizer" onPointerDown={beginPanelResize} aria-label="拖动调整一起读宽度" />
        <div className="ai-heading"><div><SparkIcon /><span><strong>一起读</strong><small>{companion?.book_type_label ?? "正在识别阅读方式"}</small></span></div><button className="mobile-close" onClick={() => setMobilePane("text")}><CloseIcon /></button></div>
        <div className="conversation">
          {!answers.length && !liveAnswer && <div className="ai-welcome"><span><QuoteIcon /></span><h2>直接和我聊聊</h2><p>不用先划线。你可以直接提问、追问，或把正文片段加入下一轮上下文。</p><div className="suggestions"><button onClick={() => setQuestion("这一章的核心论点是什么？")}>这一章的核心论点是什么？</button><button onClick={() => setQuestion("作者是怎么论证的？")}>作者是怎么论证的？</button></div></div>}
          {answers.map((answer, answerIndex) => <div className="exchange" key={answer.run_id}><div className="user-message">{answer.question}</div><div className="assistant-message"><div className="assistant-label"><SparkIcon />页伴<button className="speak-button" onClick={() => speak(answer.answer)}>播放</button></div><AnswerText text={answer.answer} />{answer.evidence.length > 0 && <div className="citations">{answer.evidence.map((item, index) => <button key={item.evidence_id} onClick={() => jumpToEvidence(item)}><QuoteIcon />原文 {index + 1} · {item.source_locator.kind === "page" ? `第 ${item.source_locator.value} 页` : "返回段落"}</button>)}</div>}{answerIndex === answers.length - 1 && answer.status === "completed" && !asking && <div className="learning-actions" aria-label="继续理解"><button type="button" onClick={() => ask("我还是不懂这个，请换一种更简单的方式解释。")}>我还是卡住</button><button type="button" onClick={() => ask("请结合刚才的内容举一个生活化的例子。")}>举个例子</button><button type="button" onClick={() => setQuestion("我试着复述：")}>我试着复述</button><button type="button" onClick={() => ask("请沿着刚才的内容继续深入一点。")}>继续深入</button></div>}</div></div>)}
          {pendingQuestion && <div className="exchange pending"><div className="user-message">{pendingQuestion}</div></div>}
          {liveAnswer && <div className="assistant-message live"><div className="assistant-label"><SparkIcon />页伴正在回答</div><AnswerText text={liveAnswer} /><i className="typing-caret" /></div>}
          {asking && <div className="thinking-row"><span /><span /><span />{answerPhase}<button type="button" onClick={stopAnswer}><StopIcon />停止</button></div>}
        </div>
        {notice && <div className="ai-notice"><span>{notice}</span><button onClick={() => setNotice("")}><CloseIcon /></button></div>}
        <form className="question-box" onSubmit={(event) => { event.preventDefault(); ask(question); }}>
          {selection && <div className="context-chip"><QuoteIcon /><span>本轮原文：{selection.quote}</span><button type="button" className="quick-ask" disabled={asking} onClick={() => ask("请帮我解释这段原文")}>解释这段</button><button type="button" onClick={() => setSelection(null)} aria-label="移除引用"><CloseIcon /></button></div>}
          <textarea rows={2} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="问问这本书…" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ask(question); } }} />
          <div><button type="button" className={`voice-button ${listening ? "listening" : ""}`} onPointerDown={beginListening} onPointerUp={endListening} onPointerLeave={endListening}>{listening ? "松开发送文字" : "按住说话"}</button><span>Enter 发送</span><button disabled={!question.trim() || asking} aria-label="发送"><SendIcon /></button></div>
        </form>
      </aside>
      {settingsOpen && companion && <div className="settings-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setSettingsOpen(false); }}>
        <form className="settings-drawer" onSubmit={saveCompanion}>
          <header><div><small>阅读偏好</small><h2>设置</h2></div><button type="button" onClick={() => setSettingsOpen(false)} aria-label="关闭设置"><CloseIcon /></button></header>
          <section><h3>阅读界面</h3>
            <label>主题<select value={readerPreferences.theme} onChange={(event) => setReaderPreferences({ ...readerPreferences, theme: event.target.value as ReaderPreferences["theme"] })}><option value="paper">纸张</option><option value="night">夜间</option></select></label>
            <label>亮度 <output>{readerPreferences.brightness}%</output><input type="range" min="70" max="115" value={readerPreferences.brightness} onChange={(event) => setReaderPreferences({ ...readerPreferences, brightness: Number(event.target.value) })} /></label>
            <label>字号 <output>{readerPreferences.fontSize}px</output><input type="range" min="16" max="28" value={readerPreferences.fontSize} onChange={(event) => setReaderPreferences({ ...readerPreferences, fontSize: Number(event.target.value) })} /></label>
            <label>行高 <output>{readerPreferences.lineHeight.toFixed(1)}</output><input type="range" min="1.6" max="2.4" step="0.1" value={readerPreferences.lineHeight} onChange={(event) => setReaderPreferences({ ...readerPreferences, lineHeight: Number(event.target.value) })} /></label>
            <label>正文宽度 <output>{readerPreferences.contentWidth}px</output><input type="range" min="560" max="920" step="20" value={readerPreferences.contentWidth} onChange={(event) => setReaderPreferences({ ...readerPreferences, contentWidth: Number(event.target.value) })} /></label>
            <button type="button" className="text-action" onClick={() => setReaderPreferences(DEFAULT_READER_PREFERENCES)}>恢复阅读默认值</button>
          </section>
          <section><h3>AI 与记忆</h3>
            <label className="switch-row"><span><strong>长期记忆</strong><small>只保存本书的简短讨论记忆，可随时清除</small></span><input type="checkbox" checked={companion.user_skill.long_term_memory_enabled} onChange={(event) => setCompanion({ ...companion, user_skill: { ...companion.user_skill, long_term_memory_enabled: event.target.checked } })} /></label>
            <label className="switch-row"><span><strong>防剧透</strong><small>小说默认不使用当前进度之后的内容</small></span><input type="checkbox" checked={companion.user_skill.spoiler_protection} onChange={(event) => setCompanion({ ...companion, user_skill: { ...companion.user_skill, spoiler_protection: event.target.checked } })} /></label>
            <label>语音速度 <output>{companion.user_skill.voice_rate.toFixed(2)}×</output><input type="range" min="0.7" max="1.4" step="0.05" value={companion.user_skill.voice_rate} onChange={(event) => setCompanion({ ...companion, user_skill: { ...companion.user_skill, voice_rate: Number(event.target.value) } })} /></label>
            <div className="memory-summary"><span>已保存 {memories.length} 条本书记忆</span><button type="button" onClick={clearMemories} disabled={!memories.length}>清除</button></div>
          </section>
          <details><summary>高级阅读方式</summary><section>
            <label>书籍类型<select value={bookTypeChoice} onChange={(event) => setBookTypeChoice(event.target.value as BookType | "auto")}><option value="auto">自动识别</option><option value="philosophy">哲学与思想</option><option value="history">历史</option><option value="social_science">人文社科</option><option value="science">科学与科普</option><option value="practical">方法与实用</option><option value="fiction">小说与叙事</option><option value="general">通用阅读</option></select></label>
            <label>回答风格<select value={companion.user_skill.tone} onChange={(event) => setCompanion({ ...companion, user_skill: { ...companion.user_skill, tone: event.target.value as Companion["user_skill"]["tone"] } })}><option value="gentle">温和清楚</option><option value="concise">简洁</option><option value="rigorous">严谨</option></select></label>
            <label>讲解深度<select value={companion.user_skill.depth} onChange={(event) => setCompanion({ ...companion, user_skill: { ...companion.user_skill, depth: event.target.value as Companion["user_skill"]["depth"] } })}><option value="quick">快速</option><option value="balanced">均衡</option><option value="deep">深入</option></select></label>
            <label>我的表达偏好<textarea maxLength={400} rows={3} value={companion.user_skill.custom_instructions} onChange={(event) => setCompanion({ ...companion, user_skill: { ...companion.user_skill, custom_instructions: event.target.value } })} placeholder="例如：先讲结论，再举生活化例子" /></label>
            <p>{companion.skill_summary}</p>
          </section></details>
          <footer><button type="button" onClick={() => setSettingsOpen(false)}>取消</button><button disabled={savingSkill}>{savingSkill ? "保存中…" : "保存设置"}</button></footer>
        </form>
      </div>}
      <nav className="mobile-tabs"><button className={mobilePane === "toc" ? "active" : ""} onClick={() => setMobilePane("toc")}><MenuIcon />目录</button><button className={mobilePane === "text" ? "active" : ""} onClick={() => setMobilePane("text")}><BookIcon />正文</button><button className={mobilePane === "ai" ? "active" : ""} onClick={() => setMobilePane("ai")}><SparkIcon />一起读</button></nav>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [books, setBooks] = useState<Book[]>([]);
  const [selectedBook, setSelectedBook] = useState<Book | null>(null);
  const [view, setView] = useState<ViewState>("library");

  const loadBooks = useCallback(async () => {
    const page = await api<{ items: Book[] }>("/api/v1/books");
    setBooks(page.items);
  }, []);

  useEffect(() => {
    api<Session>("/api/v1/session")
      .then(async (current) => { setSession(current); await loadBooks(); })
      .catch(() => setSession(null))
      .finally(() => setChecking(false));
  }, [loadBooks]);

  async function logout() {
    try { await api<void>("/api/v1/session", { method: "DELETE" }); } catch { /* local session may already be gone */ }
    setSession(null);
    setSelectedBook(null);
    setView("library");
  }

  if (checking) return <div className="app-loading"><span>页</span><p>正在打开你的阅读空间</p></div>;
  if (!session) return <LoginView onLogin={async (current) => { setSession(current); await loadBooks(); }} />;
  if (view === "reader" && selectedBook) return <ReaderView book={selectedBook} onBack={() => { setView("library"); setSelectedBook(null); loadBooks(); }} />;
  return <LibraryView books={books} onRefresh={loadBooks} onLogout={logout} onOpen={(book) => { setSelectedBook(book); setView("reader"); }} />;
}
