import React, { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { hidesCaseIds, selectionInfoForRow, selectionMethod, selectionReference } from "./selection-info.js";

const strings = (value) => Array.isArray(value) ? value.filter(v => typeof v === "string") : [];
const count = (n) => Number.isInteger(n) && n >= 0 ? n : null;

function Field({ label, value }) {
  if (value == null || value === "" || value === false) return null;
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function Distribution({ title, counts }) {
  const values = counts && typeof counts === "object" && !Array.isArray(counts)
    ? Object.entries(counts).filter(([, value]) => count(value) !== null) : [];
  return values.length ? <details><summary>{title}</summary><ul>
    {values.map(([label, n]) => <li key={label}>{label} · {n}</li>)}
  </ul></details> : null;
}

function SelectionFields({ row = {}, info = {}, t, hidden = false }) {
  const selected = count(info.selected_count) ?? count(row.selected);
  const restricted = hidden || hidesCaseIds(info, row);
  const ids = restricted ? [] : strings(info.selection_ids);
  const ref = selectionReference(info.reference_url);
  return <>
    <p className="selection-method">{selectionMethod(info, t)}</p>
    <dl className="selection-fields">
      <Field label={t("数据集", "Dataset")} value={info.dataset || row.dataset_name || row.eval_set_name || row.suite} />
      <Field label={t("版本", "Version")} value={info.version || row.dataset_version || row.eval_set_version} />
      <Field label={t("数据切分", "Split")} value={info.split || row.split} />
      <Field label={t("候选总数", "Population")} value={count(info.population_count)} />
      <Field label={t("筛选后可选", "Eligible cases")} value={count(info.eligible_count)} />
      <Field label={t("本次选中", "Selected cases")} value={selected} />
      <Field label={t("选取上限", "Selection limit")} value={count(info.requested_count) ?? count(info.limit)} />
      <Field label={t("已观测题数", "Observed cases")} value={count(info.observed_count)} />
      {selected === null && <Field label={t("报告样本数", "Reported samples")} value={count(row.sample_count)} />}
      <Field label={t("随机种子", "Seed")} value={Number.isSafeInteger(info.seed) ? info.seed : null} />
      <Field label={t("排除仓库", "Excluded repositories")} value={strings(info.excluded_repos).join(", ")} />
      <Field label={t("排除总数", "Excluded cases")} value={count(info.excluded_count)} />
      <Field label={t("按题号排除", "Excluded by ID")} value={count(info.excluded_id_count)} />
      <Field label={t("难度筛选", "Difficulty filter")} value={strings(info.difficulties).join(", ")} />
      <Field label={t("类别筛选", "Category filter")} value={strings(info.categories).join(", ")} />
      <Field label={t("仅限可移植任务", "Portable tasks only")} value={typeof info.portable_only === "boolean" ? (info.portable_only ? t("是", "Yes") : t("否", "No")) : null} />
      <Field label={t("题单指纹 SHA-256", "Cohort SHA-256")} value={info.cohort_sha256 || row.cohort_sha256} />
      <Field label={t("数据集指纹 SHA-256", "Dataset SHA-256")} value={info.dataset_sha256 || row.dataset_sha256} />
      <Field label={t("来源", "Source")} value={info.source} />
      <Field label={t("数据集参考", "Dataset reference")} value={ref && <a href={ref} target="_blank" rel="noopener noreferrer">{ref}</a>} />
    </dl>
    <Distribution title={t("按仓库分布", "Cases by repository")} counts={info.repo_counts} />
    <Distribution title={t("按难度分布", "Cases by difficulty")} counts={info.difficulty_counts} />
    {restricted ? <p className="muted">{t("留出或非公开切分不展示题号。", "Case IDs are withheld for held-out or private splits.")}</p>
      : ids.length > 0 && <details><summary>{t("本次公开题号", "Public case IDs")} ({ids.length})</summary><ul className="selection-ids">{ids.map(id => <li key={id}><code>{id}</code></li>)}</ul></details>}
    {Array.isArray(info.groups) && info.groups.map((group, index) => <details key={index}>
      <summary>{[group.dataset, group.role, group.tier, group.split].filter(Boolean).join(" · ") || t("分组", "Group")}</summary>
      <SelectionFields info={group} t={t} hidden={restricted} />
    </details>)}
  </>;
}

function SelectionDialog({ row, t, close, trigger }) {
  const dialog = useRef(null);
  const title = useId();
  useEffect(() => {
    const node = dialog.current;
    node.showModal();
    return () => { node.close(); trigger?.focus({ preventScroll: true }); };
  }, [trigger]);
  return createPortal(<dialog className="selection-dialog" ref={dialog} aria-labelledby={title}
    onCancel={close} onClick={event => { if (event.target === dialog.current) close(); }}>
    <div className="selection-dialog-body">
      <header><h2 id={title}>{t("本次运行的选题方案", "Test selection for this run")}</h2>
        <button autoFocus type="button" onClick={close}>{t("关闭", "Close")}</button></header>
      <p className="selection-run-id">{row.run_id || row.id || row.group}</p>
    <SelectionFields row={row} info={selectionInfoForRow(row)} t={t} />
    </div>
  </dialog>, document.body);
}

export function SelectionInfo({ row, t }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef(null);
  if (!row) return null;
  const id = row.run_id || row.id || row.group || row.suite || row.selection_info?.dataset;
  const title = t("查看本次运行的选题方案", "View test selection for this run");
  return <>
    <button className="selection-info" type="button" ref={trigger} aria-haspopup="dialog" aria-expanded={open}
      aria-label={id ? title + " · " + id : title} title={title}
      onClick={event => { event.preventDefault(); event.stopPropagation(); setOpen(true); }}>
      <span aria-hidden="true">ⓘ</span>
    </button>
    {open && <SelectionDialog row={row} t={t} trigger={trigger.current} close={() => setOpen(false)} />}
  </>;
}
