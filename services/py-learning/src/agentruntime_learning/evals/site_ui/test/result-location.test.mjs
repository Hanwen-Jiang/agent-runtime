import test from "node:test";
import assert from "node:assert/strict";
import { canonicalTrendLocation, readResultLocation, readTrendLocation, resultLocation, trendLocation } from "../src/result-location.js";

test("shared links restore explicit views and default unknown views to recent", () => {
  assert.deepEqual(readResultLocation("https://evals.example/results?view=archive&q=KiMi#run-records"), { view: "archive", search: "KiMi" });
  for (const suffix of ["", "?view=oops", "?view=recent"])
    assert.deepEqual(readResultLocation(`https://evals.example/results${suffix}`), { view: "recent", search: "" });
});

test("search round trips URL syntax and Unicode while preserving unrelated filters", () => {
  const search = "Kimi + 中文 & /#?=100%";
  const url = resultLocation("https://evals.example/results?type=runtime&keep=yes#trends", { view: "archive", search });
  assert.deepEqual(readResultLocation(url.href), { view: "archive", search });
  assert.equal(url.searchParams.get("type"), "runtime");
  assert.equal(url.searchParams.get("keep"), "yes");
  assert.equal(url.hash, "#run-records");
  const cleared = resultLocation(url.href, { view: "recent", search: "" });
  assert.equal(cleared.searchParams.has("q"), false);
});

test("trend links round trip a batch restriction while preserving page filters", () => {
  const href = "https://evals.example/results?type=runtime&trend=swebench&metric=resolved_rate&batch=init-50#trend-swebench";
  assert.deepEqual(readTrendLocation(href), { suite: "swebench", metric: "resolved_rate", batch: "init-50" });
  const url = trendLocation("https://evals.example/results?type=runtime&keep=yes#trends", { suite: "swebench", metric: "resolved_rate", batch: "init-50" });
  assert.equal(url.searchParams.get("type"), "runtime");
  assert.equal(url.searchParams.get("keep"), "yes");
  assert.equal(url.searchParams.get("batch"), "init-50");
  assert.equal(url.hash, "#trend-swebench");
});

test("a retired Mini 50 metric restores the original SWE-bench metric and batch", () => {
  const old = "https://evals.example/results?type=runtime&trend=swebench&metric=cumulative_resolved_rate#trend-swebench";
  const canonical = canonicalTrendLocation(old);
  assert.equal(canonical.searchParams.get("type"), "runtime");
  assert.equal(canonical.searchParams.get("metric"), "resolved_rate");
  assert.equal(canonical.searchParams.get("batch"), "mini-50");
  assert.deepEqual(readTrendLocation(old), readTrendLocation(canonical.href));
  assert.equal(canonicalTrendLocation(canonical.href).href, canonical.href);
});
