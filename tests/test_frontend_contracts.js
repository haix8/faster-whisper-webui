const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const projectRoot = path.resolve(__dirname, "..");
const appSource = fs.readFileSync(
  path.join(projectRoot, "app/static/app.js"),
  "utf8",
);
const cssSource = fs.readFileSync(
  path.join(projectRoot, "app/static/styles.css"),
  "utf8",
);
const htmlSource = fs.readFileSync(
  path.join(projectRoot, "app/static/index.html"),
  "utf8",
);

test("clipboard supports both secure and intranet HTTP contexts", () => {
  assert.match(appSource, /navigator\.clipboard\?\.writeText/);
  assert.match(appSource, /document\.execCommand\("copy"\)/);
  assert.match(
    appSource,
    /elements\.dialog\.open \? elements\.dialog : document\.body/,
  );
});

test("modal locks background scrolling and keeps notifications in its layer", () => {
  assert.match(cssSource, /body\.dialog-open\s*\{\s*overflow: hidden;/);
  assert.match(cssSource, /\.task-dialog[\s\S]*?overscroll-behavior: contain;/);
  assert.match(appSource, /function syncDialogLayer\(\)/);
  assert.match(appSource, /const toastHost = dialogOpen \? elements\.dialog : document\.body;/);
});

test("primary task action retains contrast on hover", () => {
  assert.match(
    cssSource,
    /\.task-actions \.primary:hover\s*\{[\s\S]*?color: var\(--ink\);[\s\S]*?background: var\(--mint\);/,
  );
});

test("workspace keeps body copy readable while tightening display scale", () => {
  assert.match(
    cssSource,
    /\.hero\s*\{[\s\S]*?min-height: 360px;[\s\S]*?padding: 48px 0 44px;/,
  );
  assert.match(
    cssSource,
    /\.hero h1\s*\{[\s\S]*?font-size: clamp\(52px, 5\.2vw, 78px\);/,
  );
  assert.match(
    cssSource,
    /\.task-row\s*\{[\s\S]*?min-height: 84px;[\s\S]*?padding: 14px 16px;/,
  );
  assert.match(
    cssSource,
    /\.transcript-text\s*\{[\s\S]*?font-size: 16px;[\s\S]*?line-height: 1\.8;/,
  );
});

test("polling keeps unchanged task nodes and detail scroll state", () => {
  assert.match(appSource, /nextSignature !== state\.tasksSignature/);
  assert.match(
    appSource,
    /requestVersion !== state\.tasksRequestVersion/,
  );
  assert.match(
    appSource,
    /sameTask && taskSignature === state\.currentDetailSignature/,
  );
  assert.match(appSource, /captureDetailScroll\(\)/);
  assert.match(appSource, /restoreDetailScroll\(scrollState\)/);
});

test("long timelines use a bounded, independently scrollable region", () => {
  assert.match(htmlSource, /class="segment-list"/);
  assert.match(htmlSource, /id="segmentCount"/);
  assert.match(
    cssSource,
    /\.segment-list\s*\{[\s\S]*?max-height: min\(360px, 42vh\);[\s\S]*?overflow-y: auto;[\s\S]*?overscroll-behavior: contain;/,
  );
});

test("index loads the coordinated 0.1.6 static asset revision", () => {
  assert.match(htmlSource, /styles\.css\?v=0\.1\.6/);
  assert.match(htmlSource, /upload-id\.js\?v=0\.1\.6/);
  assert.match(htmlSource, /app\.js\?v=0\.1\.6/);
});

test("link source mode creates a douyin task without a file", () => {
  assert.match(htmlSource, /id="tabUpload"/);
  assert.match(htmlSource, /id="tabLink"/);
  assert.match(htmlSource, /id="linkInput"/);
  assert.match(htmlSource, /id="createLinkTask"/);
  assert.match(appSource, /function switchSourceMode\(mode\)/);
  assert.match(appSource, /function createLinkTaskFlow\(\)/);
  assert.match(appSource, /function extractLinkUrl\(text\)/);
  assert.match(
    appSource,
    /JSON\.stringify\(\{\s*source_url: url,\s*model: elements\.modelSelect\.value/,
  );
  assert.match(appSource, /downloading: "下载视频"/);
});
