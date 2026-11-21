const METHODS = {
  stratified_repo_difficulty: ["按仓库题量分配名额，再按难度等距取题", "Allocate by repository size, then sample across difficulty within each repository"],
  seeded_random: ["按随机种子抽样", "Random sampling with a seed"],
  explicit_ids: ["按明确列出的题号选取", "Explicit list of case IDs"],
  full: ["使用整个数据切分", "Entire dataset split"],
  filtered_all: ["使用满足筛选条件的全部题目", "All cases matching the filters"],
  per_evaluator: ["按各评测组件的规则选题", "Each evaluator selects its own cases"],
  seeded_hash_prefix: ["按带种子的哈希排序后取前缀", "Prefix of a seeded hash ordering"],
  seeded_round_robin: ["按带种子的轮转顺序取题", "Seeded round-robin sampling"],
  seeded_shuffle_prefix: ["按种子打乱后取前缀", "Prefix of a seeded shuffle"],
  predefined_split: ["使用预定义的数据切分", "Predefined dataset split"],
  frozen_cohort: ["使用预先冻结的题单", "Previously frozen case list"],
  supplied_cases: ["使用本次运行提供的用例", "Cases supplied to this run"],
};

const INIT_50_IDS = [
  "astropy__astropy-13033", "astropy__astropy-8707",
  "django__django-11163", "django__django-11276", "django__django-11292", "django__django-11490",
  "django__django-11740", "django__django-11885", "django__django-11951", "django__django-12262",
  "django__django-13033", "django__django-13741", "django__django-13810", "django__django-14155",
  "django__django-14765", "django__django-14771", "django__django-15503", "django__django-15561",
  "django__django-15569", "django__django-15814", "django__django-15851", "django__django-16145",
  "django__django-16595", "django__django-16661", "django__django-16877",
  "matplotlib__matplotlib-13989", "matplotlib__matplotlib-26342", "matplotlib__matplotlib-26466",
  "mwaskom__seaborn-3187", "pallets__flask-5014", "pydata__xarray-2905", "pydata__xarray-7393",
  "pylint-dev__pylint-6528", "pytest-dev__pytest-7205", "pytest-dev__pytest-7571",
  "scikit-learn__scikit-learn-10297", "scikit-learn__scikit-learn-13439", "scikit-learn__scikit-learn-14629",
  "sphinx-doc__sphinx-10673", "sphinx-doc__sphinx-8056", "sphinx-doc__sphinx-9367", "sphinx-doc__sphinx-9711",
  "sympy__sympy-12096", "sympy__sympy-13615", "sympy__sympy-14531", "sympy__sympy-15599",
  "sympy__sympy-15809", "sympy__sympy-17630", "sympy__sympy-23413", "sympy__sympy-24562",
];

export const PUBLIC_BATCHES = Object.freeze({
  "init-50": Object.freeze({
    batch_id: "init-50",
    label_zh: "init-50 · 50 题固定批次",
    label_en: "init-50 · frozen 50-case batch",
    dataset: "SWE-bench Verified",
    split: "verified",
    method: "stratified_repo_difficulty",
    method_detail_zh: "init-50 固定题单：seed=0，按仓库题量分配名额，再按难度等距取题；排除 psf/requests。",
    method_detail_en: "Frozen init-50 list: seed 0, allocate by repository size, then sample across difficulty; psf/requests excluded.",
    population_count: 500,
    eligible_count: 492,
    excluded_count: 8,
    excluded_repos: ["psf/requests"],
    selected_count: 50,
    seed: 0,
    cohort_sha256: "2d21d701620618b80532399f6e021eaa8ce227c3b5a813d72b85920f592c8f0f",
    dataset_sha256: "2d336d3f60d2a6496b21cf04ab73ed6cdd016eb52f19e1e4bb0328a278145d52",
    source: "gpt55-base50/instances.jsonl",
    reference_url: "https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified",
    selection_ids: INIT_50_IDS,
    repo_counts: {
      "astropy/astropy": 2, "django/django": 23, "matplotlib/matplotlib": 3,
      "mwaskom/seaborn": 1, "pallets/flask": 1, "pydata/xarray": 2,
      "pylint-dev/pylint": 1, "pytest-dev/pytest": 2, "scikit-learn/scikit-learn": 3,
      "sphinx-doc/sphinx": 4, "sympy/sympy": 8,
    },
  }),
  "longbench-40": Object.freeze({
    batch_id: "longbench-40", label_zh: "longbench-40 · LongBench v2 固定 40 题", label_en: "longbench-40 · fixed LongBench v2 40-case cohort",
    dataset: "LongBench v2", split: "development", selected_count: 40, method: "seeded_hash_prefix", seed: 0,
    cohort_sha256: "a50aeaac8abfd5aa69d00b4f31080ee11e15b5ac0d2b3c164bcca0456be83415",
  }),
  "longmemeval-50": Object.freeze({
    batch_id: "longmemeval-50", label_zh: "longmemeval-50 · 固定 50 题", label_en: "longmemeval-50 · fixed 50-case cohort",
    dataset: "LongMemEval_S", split: "development", selected_count: 50, method: "frozen_cohort",
  }),
  "mini-50": Object.freeze({
    batch_id: "mini-50", label_zh: "mini-50 · 新 50 题方案", label_en: "mini-50 · new 50-case scheme",
    dataset: "SWE-bench Verified", split: "verified", selected_count: 50, method: "unrecorded",
    method_detail_zh: "新 Mini 50 方案；题单方法等待运行清单发布。",
    method_detail_en: "New Mini 50 scheme; the selection manifest is pending publication.",
  }),
});

export function batchForRow(row = {}) {
  const info = row.selection_info || {};
  const cohort = row.cohort_sha256 || info.cohort_sha256;
  if (/^mini50[-_]/i.test(row.run_id || row.id || "")) return "mini-50";
  return Object.values(PUBLIC_BATCHES).find((batch) => cohort && batch.cohort_sha256 && batch.cohort_sha256 === cohort)?.batch_id || info.batch_id || row.batch_id || null;
}

export function selectionInfoForRow(row = {}) {
  const batchID = batchForRow(row);
  const known = batchID ? PUBLIC_BATCHES[batchID] : null;
  if (!known) return row.selection_info || {};
  return { ...known, ...(row.selection_info || {}), batch_id: batchID };
}

export function selectionMethod(info, t) {
  const method = info.method;
  const detail = t(info.method_detail_zh, info.method_detail_en);
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Object.hasOwn(METHODS, method)) return t(...METHODS[method]);
  if (typeof method === "string" && method && !["unrecorded", "unknown", "historical_unrecorded"].includes(method)) return method;
  return t("本次运行未记录选题方法。", "No selection method was recorded for this run.");
}

export function hidesCaseIds(...rows) {
  return rows.some((row) => row && (
    row.ids_hidden === true || row.holdout_hidden === true || row.visibility === "private" ||
    [row.split, row.dataset, row.eval_set_name].some((s) =>
      typeof s === "string" && /hold[- _]?out|held[- _]?out|private|hidden/i.test(s))
  ));
}

export function selectionReference(value) {
  if (typeof value !== "string" || !value.startsWith("https://")) return null;
  try {
    const url = new URL(value);
    return !url.username && !url.password ? url.href : null;
  } catch {
    return null;
  }
}

export function matchingSelection(row, info) {
  if (!info || !row) return null;
  if (hidesCaseIds(row, row.selection_info) && !hidesCaseIds(info)) return null;
  for (const recorded of [row, row.selection_info || {}]) {
    if (recorded.cohort_sha256 && recorded.cohort_sha256 !== info.cohort_sha256) return null;
    if (recorded.dataset_sha256 && recorded.dataset_sha256 !== info.dataset_sha256) return null;
    const selected = recorded.selected_count ?? recorded.selected;
    if (selected != null && selected !== info.selected_count) return null;
  }
  return info;
}

// Historical summaries and chart feeds may be older than their run manifests.
// Only borrow provenance when BOTH the benchmark and exact run ID match.
export function archivedSelections(archive, benchmarks) {
  if (!archive?.reports || !benchmarks?.runs) return archive;
  const byId = new Map(benchmarks.runs.filter(r => r.run_id && r.selection_info)
    .map(r => [r.suite + ":" + r.run_id, r.selection_info]));
  const reports = { ...archive.reports };
  for (const key of ["benchmarks", "tuning"]) {
    if (!Array.isArray(reports[key])) continue;
    reports[key] = reports[key].map(row => {
      const info = row.run_id && matchingSelection(row, byId.get((row.suite || "swebench") + ":" + row.run_id));
      return info ? { ...row, selection_info: info } : row;
    });
  }
  return { ...archive, reports };
}
