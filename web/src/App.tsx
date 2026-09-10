import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, apiAll, requestId } from "./api";
import { ArrowIcon, BookIcon, CheckIcon, CloseIcon, MenuIcon, QuoteIcon, SendIcon, SparkIcon, UploadIcon } from "./icons";
import type { Answer, Block, Book, Chapter, Evidence, Highlight, Progress, Session } from "./types";

type ViewState = "library" | "reader";
type SelectionDraft = {
  block: Block;
  start: number;
  end: number;
  quote: string;
  x: number;
  y: number;
};

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
  const [contextHighlight, setContextHighlight] = useState<Highlight | null>(null);
  const [asking, setAsking] = useState(false);
  const [liveAnswer, setLiveAnswer] = useState("");
  const [selection, setSelection] = useState<SelectionDraft | null>(null);
  const [notice, setNotice] = useState("");
  const [mobilePane, setMobilePane] = useState<"toc" | "text" | "ai">("text");
  const blockElements = useRef(new Map<string, HTMLElement>());
  const pendingEvidenceBlock = useRef<string | null>(null);
  const saveTimer = useRef<number | undefined>(undefined);

  useEffect(() => { progressRef.current = progress; }, [progress]);

  const loadHistory = useCallback(async () => {
    const result = await api<{ items: Answer[] }>(`/api/v1/books/${book.book_id}/answers`);
    setAnswers(result.items);
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
    const rect = range.getBoundingClientRect();
    setSelection({ block, start, end, quote, x: Math.min(window.innerWidth - 240, Math.max(16, rect.left)), y: Math.max(70, rect.top - 54) });
  }

  async function ask(text: string, highlightId?: string) {
    const clean = text.trim();
    if (!clean || asking) return;
    const submittedHighlightId = highlightId ?? contextHighlight?.highlight_id ?? null;
    setAsking(true);
    setLiveAnswer("");
    setNotice("");
    setMobilePane("ai");
    try {
      const run = await api<{ run_id: string; conversation_id: string }>(`/api/v1/books/${book.book_id}/questions`, {
        method: "POST",
        headers: { "Idempotency-Key": requestId() },
        body: JSON.stringify({
          question: clean,
          highlight_id: submittedHighlightId,
          current_chapter_id: chapterId || null,
          conversation_id: conversationId,
          client_request_id: requestId()
        })
      });
      await new Promise<void>((resolve, reject) => {
        const source = new EventSource(`/api/v1/answer-runs/${run.run_id}/events`, { withCredentials: true });
        source.addEventListener("answer_delta", (event) => {
          const payload = JSON.parse((event as MessageEvent).data).payload;
          setLiveAnswer((current) => current + payload.text_delta);
        });
        source.addEventListener("completed", async () => {
          source.close();
          setConversationId(run.conversation_id);
          setQuestion((current) => current.trim() === clean ? "" : current);
          setContextHighlight((current) => current?.highlight_id === submittedHighlightId ? null : current);
          try { await loadHistory(); } finally { resolve(); }
        });
        source.addEventListener("failed", (event) => {
          const payload = JSON.parse((event as MessageEvent).data).payload;
          source.close();
          reject(new ApiError(payload.error?.message ?? "回答失败", payload.error?.code));
        });
        source.onerror = () => {
          source.close();
          reject(new ApiError("回答连接中断，请重试", "stream_interrupted"));
        };
      });
    } catch (reason) {
      setNotice(errorMessage(reason));
    } finally {
      setAsking(false);
      setLiveAnswer("");
    }
  }

  async function useSelection() {
    if (!selection) return;
    try {
      const quoteHash = await textHash(selection.quote);
      const anchor = await api<Highlight>(`/api/v1/books/${book.book_id}/highlights`, {
        method: "POST",
        headers: { "Idempotency-Key": requestId() },
        body: JSON.stringify({
          chapter_id: selection.block.chapter_id,
          start: { block_id: selection.block.block_id, offset: selection.start },
          end: { block_id: selection.block.block_id, offset: selection.end },
          exact_quote: selection.quote,
          prefix: selection.block.text.slice(Math.max(0, selection.start - 48), selection.start),
          suffix: selection.block.text.slice(selection.end, selection.end + 48),
          text_sha256: quoteHash
        })
      });
      setHighlights((current) => [...current, anchor]);
      setContextHighlight(anchor);
      setSelection(null);
      window.getSelection()?.removeAllRanges();
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

  return (
    <div className={`reader-shell pane-${mobilePane}`}>
      <header className="reader-topbar">
        <button className="icon-button back-button" onClick={onBack} aria-label="返回书架"><ArrowIcon /></button>
        <button className="mobile-menu" onClick={() => setMobilePane("toc")}><MenuIcon /></button>
        <div className="reader-title"><strong>{book.title}</strong><span>{currentChapter?.title ?? "正在载入"}</span></div>
        <div className="reading-progress"><span style={{ width: `${percent}%` }} /><small>{percent}%</small></div>
        <button className="ai-mobile-button" onClick={() => setMobilePane("ai")}><SparkIcon />一起读</button>
      </header>
      <aside className="toc-panel">
        <div className="panel-head"><Logo /><button className="mobile-close" onClick={() => setMobilePane("text")}><CloseIcon /></button></div>
        <button className="back-to-library" onClick={onBack}><ArrowIcon />返回书架</button>
        <p className="panel-label">目录</p>
        <nav className="chapter-list">
          {chapters.map((chapter) => <button key={chapter.chapter_id} className={chapter.chapter_id === chapterId ? "active" : ""} onClick={() => { setChapterId(chapter.chapter_id); setMobilePane("text"); }}><span>{String(chapter.ordinal + 1).padStart(2, "0")}</span>{chapter.title}</button>)}
        </nav>
        <div className="toc-footer"><span><CheckIcon />进度已保存</span><small>本地开发预览</small></div>
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
      <aside className="ai-panel">
        <div className="ai-heading"><div><SparkIcon /><span><strong>一起读</strong><small>回答只基于你的书</small></span></div><button className="mobile-close" onClick={() => setMobilePane("text")}><CloseIcon /></button></div>
        <div className="conversation">
          {!answers.length && !liveAnswer && <div className="ai-welcome"><span><QuoteIcon /></span><h2>直接和我聊聊</h2><p>不用先划线。你可以直接提问、追问，或把正文片段加入下一轮上下文。</p><div className="suggestions"><button onClick={() => setQuestion("这一章的核心论点是什么？")}>这一章的核心论点是什么？</button><button onClick={() => setQuestion("作者是怎么论证的？")}>作者是怎么论证的？</button></div></div>}
          {answers.map((answer) => <div className="exchange" key={answer.run_id}><div className="user-message">{answer.question}</div><div className="assistant-message"><div className="assistant-label"><SparkIcon />页伴</div><AnswerText text={answer.answer} />{answer.evidence.length > 0 && <div className="citations">{answer.evidence.map((item, index) => <button key={item.evidence_id} onClick={() => jumpToEvidence(item)}><QuoteIcon />原文 {index + 1} · {item.source_locator.kind === "page" ? `第 ${item.source_locator.value} 页` : "返回段落"}</button>)}</div>}</div></div>)}
          {liveAnswer && <div className="assistant-message live"><div className="assistant-label"><SparkIcon />页伴正在回答</div><AnswerText text={liveAnswer} /><i className="typing-caret" /></div>}
          {asking && !liveAnswer && <div className="thinking-row"><span /><span /><span />正在理解你的问题</div>}
        </div>
        {notice && <div className="ai-notice"><span>{notice}</span><button onClick={() => setNotice("")}><CloseIcon /></button></div>}
        <form className="question-box" onSubmit={(event) => { event.preventDefault(); ask(question); }}>
          {contextHighlight && <div className="context-chip"><QuoteIcon /><span>引用：{contextHighlight.exact_quote}</span><button type="button" onClick={() => setContextHighlight(null)} aria-label="移除引用"><CloseIcon /></button></div>}
          <textarea rows={2} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="问问这本书…" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ask(question); } }} />
          <div><span>Enter 发送 · Shift + Enter 换行</span><button disabled={!question.trim() || asking} aria-label="发送"><SendIcon /></button></div>
        </form>
      </aside>
      {selection && <div className="selection-menu" style={{ left: selection.x, top: selection.y }}><button onClick={() => useSelection()}>加入对话</button><button className="menu-close" onClick={() => setSelection(null)} aria-label="关闭"><CloseIcon /></button></div>}
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
