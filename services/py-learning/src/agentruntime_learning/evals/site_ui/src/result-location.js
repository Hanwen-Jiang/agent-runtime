// Only these two parameters control the run-record list. Preserve other page
// filters and use URLSearchParams for spaces, Unicode, literal + and & characters.
export function readResultLocation(href) {
  const url = new URL(href);
  return { view: url.searchParams.get("view") === "archive" ? "archive" : "recent",
    search: url.searchParams.get("q") || "" };
}

export function resultLocation(href, { view, search }) {
  const url = new URL(href);
  url.searchParams.set("view", view === "archive" ? "archive" : "recent");
  if (search) url.searchParams.set("q", search); else url.searchParams.delete("q");
  url.hash = "run-records";
  return url;
}

// Trend selection is independent of the run-record search. Keep explicit IDs
// while data is loading; the view must not silently substitute another chart.
export function canonicalTrendLocation(href) {
  const url = new URL(href);
  if (url.searchParams.get("metric") === "cumulative_resolved_rate" && (!url.searchParams.get("trend") || url.searchParams.get("trend") === "swebench")) {
    url.searchParams.set("trend", "swebench");
    url.searchParams.set("metric", "resolved_rate");
    url.searchParams.set("batch", "mini-50");
    url.hash = "trend-swebench";
  }
  return url;
}

export function readTrendLocation(href) {
  const url = canonicalTrendLocation(href);
  const suite = url.searchParams.get("trend") || "";
  return { suite, metric: suite ? url.searchParams.get("metric") || "" : "", batch: url.searchParams.get("batch") || "" };
}

export function trendLocation(href, { suite, metric = "", batch = "" }) {
  const url = new URL(href);
  if (suite) url.searchParams.set("trend", suite); else url.searchParams.delete("trend");
  if (suite && metric) url.searchParams.set("metric", metric); else url.searchParams.delete("metric");
  if (batch) url.searchParams.set("batch", batch); else url.searchParams.delete("batch");
  url.hash = suite ? `trend-${suite}` : "trends";
  return url;
}
