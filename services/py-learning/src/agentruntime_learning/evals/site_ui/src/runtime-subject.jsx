import React from "react";
import { Chip } from "@heroui/react";
import { displayGroup, reasoningLabel, routingLabel, runtimeEvidence, subjectLabel } from "./runtime-identity.js";

export function DisplayGroup({row, t}) {
  const group = displayGroup(row);
  return group && <>{" "}<Chip size="sm" variant="soft" data-runtime-group={group} title={t("展示分组", "Display group")}
    aria-label={t(`展示分组 ${group}`, `Display group ${group}`)}><Chip.Label>{group}</Chip.Label></Chip></>;
}

export function RuntimeSubject({row, t, fallback}) {
  return <>{subjectLabel(row, t, fallback)}<DisplayGroup row={row} t={t}/></>;
}

export function RuntimeRouting({row, t}) {
  const evidence = runtimeEvidence(row);
  if (!evidence) return null;
  const effort = reasoningLabel(evidence,t);
  return <>
    <dt>{t("路由方式","Routing")}</dt><dd>{routingLabel(evidence,t)}</dd>
    {effort && <><dt>{t("实测思考档","Observed reasoning effort")}</dt><dd>{effort}{evidence.effort === "mixed" && evidence.efforts?.length > 1 && ` (${evidence.efforts.join(" / ")})`}</dd></>}
    {evidence.models?.length > 0 && <><dt>{t("实测模型","Observed models")}</dt><dd>{evidence.models.join(" · ")}</dd></>}
    {typeof evidence.source === "string" && evidence.source && <><dt>{t("运行证据","Evidence")}</dt><dd>{evidence.source}</dd></>}
    {(row.model || row.subject) && <><dt>{t("原运行标签","Original run label")}</dt><dd>{row.model || row.subject}</dd></>}
  </>;
}
