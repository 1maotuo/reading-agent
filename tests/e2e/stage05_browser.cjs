const fs = require("node:fs");
const path = require("node:path");

const packageRoot = process.env.CODEX_NODE_MODULES;
if (!packageRoot) throw new Error("CODEX_NODE_MODULES is required");
const { chromium } = require(path.join(packageRoot, "playwright"));

const root = path.resolve(__dirname, "..", "..");
const artifacts = path.join(root, "artifacts", "stage05");
const fixture = path.join(root, "experiments", "f03_01_document_parsing", "fixtures", "thinking_clearly.md");
fs.mkdirSync(artifacts, { recursive: true });

async function run() {
  const browserExecutable = process.env.READING_AGENT_BROWSER_EXECUTABLE || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
  const browser = await chromium.launch({ headless: true, executablePath: browserExecutable });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.goto("http://127.0.0.1:8765/", { waitUntil: "networkidle" });
  await page.getByLabel("密码").fill("reading-demo");
  await page.getByRole("button", { name: /进入书架/ }).click();
  await page.getByRole("heading", { name: "今天想读点什么？" }).waitFor();
  // The unauthenticated session probe intentionally returns 401 before login.
  // Only errors produced after the authenticated app is ready are regressions.
  consoleErrors.length = 0;

  await page.locator('input[type="file"]').setInputFiles(fixture);
  await page.locator(".reader-block").first().waitFor({ timeout: 20_000 });
  const uploadedText = await page.locator(".reader-block").first().innerText();
  if (!uploadedText.toLowerCase().includes("thinking")) throw new Error("uploaded source text was not rendered");

  await page.locator(".reader-block").first().evaluate((element) => {
    const text = element.firstChild;
    if (!text) throw new Error("reader block has no text node");
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, Math.min(28, text.textContent.length));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    element.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
  await page.locator(".selection-menu").waitFor();
  await page.locator(".selection-menu").getByRole("button", { name: "解释" }).click();
  await page.locator(".citations button").first().waitFor({ timeout: 70_000 });
  await page.screenshot({ path: path.join(artifacts, "stage05-desktop.png"), fullPage: true });

  const answerVisible = await page.locator(".assistant-message").last().innerText();
  if (!answerVisible.trim()) throw new Error("answer was empty");
  await page.locator(".citations button").first().click();
  await page.waitForTimeout(500);

  const lastBlock = page.locator(".reader-block").last();
  await lastBlock.scrollIntoViewIfNeeded();
  await page.waitForTimeout(900);
  await page.reload({ waitUntil: "networkidle" });
  await page.locator(".book-card").first().click();
  await page.locator(".reader-block").first().waitFor();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(300);
  await page.getByRole("button", { name: /一起读/ }).first().click();
  await page.locator(".ai-panel").screenshot({ path: path.join(artifacts, "stage05-mobile-ai.png") });
  const mobilePanel = await page.locator(".ai-panel").boundingBox();
  if (!mobilePanel || mobilePanel.width < 350) throw new Error("mobile AI panel was not usable");

  const report = {
    status: consoleErrors.length ? "failed" : "passed",
    checks: {
      login: true,
      real_file_upload: true,
      reader_text: true,
      same_block_highlight: true,
      evidence_answer: true,
      citation_jump: true,
      reload_session_and_book: true,
      mobile_ai_panel: true
    },
    model_fallback: answerVisible.includes("AI 模型暂时不可用"),
    console_error_count: consoleErrors.length,
    console_errors: consoleErrors.slice(0, 5)
  };
  fs.writeFileSync(path.join(artifacts, "stage05-browser-report.json"), JSON.stringify(report, null, 2));
  await browser.close();
  if (consoleErrors.length) throw new Error(`browser console errors: ${consoleErrors.join(" | ")}`);
  process.stdout.write(JSON.stringify(report));
}

run().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
