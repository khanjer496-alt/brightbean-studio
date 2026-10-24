const test = require("node:test");
const assert = require("node:assert");
const { execFileSync } = require("node:child_process");
const { readFileSync } = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const Pd = require(path.join(root, "integrations", "chatgpt", "widgets", "src", "shared.js"));

test("the committed widget bundles match their sources", () => {
  execFileSync("node", [path.join(root, "integrations", "chatgpt", "build.mjs"), "--check"], { stdio: "pipe" });
});

test("bundles are self-contained: no external loads, no inline style attributes", () => {
  for (const name of ["post-preview", "week-calendar", "approval-list"]) {
    const html = readFileSync(path.join(root, "apps", "mcp", "widgets", `${name}.html`), "utf8");
    assert.ok(html.toLowerCase().startsWith("<!doctype html>"));
    assert.ok(!/(src|href)=["']https?:/i.test(html), `${name} loads an external file`);
    assert.ok(!/\sstyle=/i.test(html), `${name} has an inline style attribute`);
    assert.ok(!/innerHTML|outerHTML|document\.write|eval\(/.test(html), `${name} writes untrusted markup`);
    assert.ok(!html.includes("/*__"), `${name} has an unfilled placeholder`);
  }
});

test("weekStart is the Monday of the week", () => {
  assert.strictEqual(Pd.weekStart(new Date(2026, 9, 25)).getDay(), 1); // a Sunday
  assert.strictEqual(Pd.weekStart(new Date(2026, 9, 25)).getDate(), 19);
  assert.strictEqual(Pd.weekStart(new Date(2026, 9, 19)).getDate(), 19);
});

test("groupByWeek places posts on their day, sorted, and counts the rest", () => {
  const start = new Date(2026, 9, 19);
  const posts = [
    { id: "b", scheduled_at: new Date(2026, 9, 21, 15).toISOString() },
    { id: "a", scheduled_at: new Date(2026, 9, 21, 9).toISOString() },
    { id: "c", scheduled_at: new Date(2026, 10, 30, 9).toISOString() },
    { id: "d" },
  ];
  const { days, outside } = Pd.groupByWeek(posts, start);
  assert.strictEqual(days.length, 7);
  assert.deepStrictEqual(days[2].posts.map((e) => e.post.id), ["a", "b"]);
  assert.strictEqual(outside, 2);
});

test("pickWeek opens on the week with the most posts", () => {
  const posts = [
    { scheduled_at: new Date(2026, 9, 5, 9).toISOString() },
    { scheduled_at: new Date(2026, 9, 21, 9).toISOString() },
    { scheduled_at: new Date(2026, 9, 22, 9).toISOString() },
  ];
  assert.strictEqual(Pd.pickWeek(posts).getDate(), 19);
});

test("status tones", () => {
  assert.strictEqual(Pd.tone("published"), "ok");
  assert.strictEqual(Pd.tone("pending_review"), "warn");
  assert.strictEqual(Pd.tone("failed"), "bad");
  assert.strictEqual(Pd.label("pending_review"), "pending review");
});
