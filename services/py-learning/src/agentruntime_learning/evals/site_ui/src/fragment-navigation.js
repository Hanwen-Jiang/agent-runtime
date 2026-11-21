// Only navigate to sections the results page owns. A missing asynchronous
// target stays pending; no selector is built from user-supplied fragment text.
const SECTIONS = new Set(["trends", "capability-matrix", "run-records", "experiments", "runs", "contexts", "tuning"]);
export function resultFragmentTarget(hash) {
  if (typeof hash !== "string" || !hash.startsWith("#")) return null;
  let id;
  try { id = decodeURIComponent(hash.slice(1)); } catch { return null; }
  return SECTIONS.has(id) || /^(?:experiment|trend)-[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(id) ? id : null;
}
export function scrollToResultFragment(hash, page = window, doc = document) {
  const id = resultFragmentTarget(hash);
  const target = id ? doc.getElementById(id) : null;
  if (!target) return false;
  const headerHeight = doc.querySelector(".topbar")?.getBoundingClientRect().height || 0;
  const top = Math.max(0, page.scrollY + target.getBoundingClientRect().top - headerHeight - 12);
  page.scrollTo({ top, behavior: "auto" });
  return true;
}

// Called for a pending navigation only, never for a feed refresh. Checking the
// target's document position also catches layout changes in preceding sections.
export function afterResultLayout(hash, done, page = window, doc = document) {
  const id = resultFragmentTarget(hash);
  if (!id) return () => {};
  let frame, cancelled = false, previous = null, stableSince = 0;
  const started = page.performance.now();
  function measure(now) {
    if (cancelled) return;
    const target = doc.getElementById(id);
    if (target && doc.fonts?.status !== "loading") {
      const box = target.getBoundingClientRect();
      const geometry = [box.top + page.scrollY, box.height,
        doc.querySelector(".topbar")?.getBoundingClientRect().height || 0,
        doc.documentElement.scrollHeight, doc.documentElement.clientWidth];
      if (!previous || geometry.some((n, i) => Math.abs(n - previous[i]) > 1)) stableSince = now;
      previous = geometry;
      if (now - started >= 500 && now - stableSince >= 250) {
        if (scrollToResultFragment(hash, page, doc)) done();
        return;
      }
    } else { previous = null; stableSince = now; }
    // Do not leave a scroll watcher behind for an absent section or a page
    // which keeps changing. Another explicit navigation can try again.
    if (now - started >= 5000) { done(); return; }
    frame = page.requestAnimationFrame(measure);
  }
  frame = page.requestAnimationFrame(measure);
  return () => { cancelled = true; page.cancelAnimationFrame(frame); };
}
