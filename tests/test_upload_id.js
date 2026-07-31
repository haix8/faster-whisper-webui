const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const projectRoot = path.resolve(__dirname, "..");
const helperPath = path.join(projectRoot, "app/static/upload-id.js");
const appPath = path.join(projectRoot, "app/static/app.js");
const indexPath = path.join(projectRoot, "app/static/index.html");
const helperSource = fs.readFileSync(helperPath, "utf8");

function loadHelper({
  crypto,
  now = 1_753_860_000_000,
  random = 0.25,
  withoutGlobalThis = false,
} = {}) {
  const math = Object.create(Math);
  math.random = () => random;
  const browserWindow = { crypto };
  const sandbox = {
    crypto,
    Date: { now: () => now },
    Math: math,
    Uint8Array,
    window: browserWindow,
  };
  sandbox.globalThis = withoutGlobalThis ? undefined : sandbox;
  vm.runInNewContext(helperSource, sandbox);
  return withoutGlobalThis
    ? browserWindow.WhisperUploadId
    : sandbox.WhisperUploadId;
}

test("uses crypto.randomUUID when the secure-context API exists", () => {
  const helper = loadHelper({
    crypto: { randomUUID: () => "native-random-uuid" },
  });

  assert.equal(helper.create(), "native-random-uuid");
});

test("creates an RFC 4122 version 4 UUID from getRandomValues", () => {
  const helper = loadHelper({
    crypto: {
      getRandomValues(bytes) {
        bytes.set(Array.from({ length: 16 }, (_, index) => index));
        return bytes;
      },
    },
  });

  assert.equal(helper.create(), "00010203-0405-4607-8809-0a0b0c0d0e0f");
});

test("creates unique upload keys when Web Crypto is unavailable", () => {
  const helper = loadHelper();
  const first = helper.create();
  const second = helper.create();

  assert.match(first, /^upload-[a-z0-9]+-1-[a-z0-9]{12}$/);
  assert.match(second, /^upload-[a-z0-9]+-2-[a-z0-9]{12}$/);
  assert.notEqual(first, second);
  assert.ok(first.length < 200);
});

test("loads in browser environments that do not expose globalThis", () => {
  const helper = loadHelper({ withoutGlobalThis: true });

  assert.match(helper.create(), /^upload-[a-z0-9]+-1-[a-z0-9]{12}$/);
});

test("loads the helper before the application entrypoint", () => {
  const html = fs.readFileSync(indexPath, "utf8");
  const appSource = fs.readFileSync(appPath, "utf8");

  assert.ok(html.indexOf("/static/upload-id.js") >= 0);
  assert.ok(html.indexOf("/static/upload-id.js") < html.indexOf("/static/app.js"));
  assert.match(appSource, /window\.WhisperUploadId\.create\(\)/);
});
