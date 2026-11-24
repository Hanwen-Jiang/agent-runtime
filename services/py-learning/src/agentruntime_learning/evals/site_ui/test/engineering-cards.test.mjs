import test from "node:test";
import assert from "node:assert/strict";
import { PUBLISHED_ENGINEERING_CARDS, mergeEngineeringExperiments } from "../src/engineering-cards.js";

test("publishes sandbox warm-pool and 200-concurrency workspace cards with verified metrics", () => {
  const ids = PUBLISHED_ENGINEERING_CARDS.map((c) => c.id);
  assert.ok(ids.includes("engineering-20261107-sandbox-warmpool"));
  assert.ok(ids.includes("engineering-20261125-workspace-concurrency"));
  const sandbox = PUBLISHED_ENGINEERING_CARDS.find((c) => c.id === "engineering-20261107-sandbox-warmpool");
  assert.equal(sandbox.sample_count, 20);
  const conc = PUBLISHED_ENGINEERING_CARDS.find((c) => c.id === "engineering-20261125-workspace-concurrency");
  assert.equal(conc.sample_count, 14000);
});

test("live backend cards override published fallbacks by ID without duplication", () => {
  const live = [{ id: "engineering-20261107-sandbox-warmpool", title_zh: "live-version" }];
  const merged = mergeEngineeringExperiments(live);
  const matching = merged.filter((c) => c.id === "engineering-20261107-sandbox-warmpool");
  assert.equal(matching.length, 1);
  assert.equal(matching[0].title_zh, "live-version");
  assert.ok(merged.some((c) => c.id === "engineering-20261125-workspace-concurrency"));
});
