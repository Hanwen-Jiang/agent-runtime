import { matchesRunSearch } from "./runtime-identity.js";

/** Only authoritative campaign membership may hide an independent run. */
export function campaignMemberIds(campaigns = []) {
  return new Set(campaigns.flatMap(c => c.member_run_ids || []));
}

export function separateCampaignRuns(live, archive, benchmarks) {
  const members = campaignMemberIds(benchmarks?.campaigns);
  if (!members.size) return {live, archive, benchmarks};
  const standalone = (row) => (row.suite && row.suite !== "swebench") || !members.has(row.run_id);
  const reports = archive?.reports || archive;
  const filteredReports = reports && {...reports,
    runs:(reports.runs || []).filter(standalone),
    benchmarks:(reports.benchmarks || []).filter(standalone), tuning:(reports.tuning || []).filter(standalone),
  };
  return {
    live: Array.isArray(live) ? live.filter(standalone) : live && {...live, runs:(live.runs || []).filter(standalone)},
    benchmarks: {...benchmarks, runs:(benchmarks.runs || []).filter(standalone)},
    archive: archive?.reports ? {...archive, reports:filteredReports} : filteredReports,
  };
}

export function campaignRunIdsMatching(campaign, rows, search) {
  const query = search.trim().toLowerCase();
  if (!query) return [];
  return (campaign.member_run_ids || []).filter(id => {
    const row = rows.get(id);
    return matchesRunSearch({suite:"swebench", ...row, run_id:id}, query);
  });
}

export function exactCampaignRunId(campaign, search = "") {
  const query = search.trim().toLowerCase();
  return campaign.member_run_ids?.find(id => id.toLowerCase() === query);
}

export function campaignMatches(campaign, rows, {search = "", status = "", split = ""}) {
  if (split && split !== campaign.split) return false;
  if (status) {
    const complete = campaign.arms?.length > 0 && campaign.arms.every(arm => arm.graded_count === campaign.selected);
    if (status === "finished") {
      if (!complete) return false;
    } else if (status === "partial") {
      if (complete || !campaign.arms?.some(arm => arm.graded_count > 0)) return false;
    } else if (complete || !campaign.arms?.some(arm => {
      const latest = rows.get(arm.infra_run_id) || rows.get(arm.recovery_run_id) || rows.get(arm.initial_run_id);
      return latest?.status === status;
    })) return false;
  }
  const query = search.trim().toLowerCase();
  return !query || [campaign.campaign_id, campaign.title, "mini-50", "Mini 50", "SWE-bench Verified"]
    .some(s => s?.toLowerCase().includes(query)) || campaignRunIdsMatching(campaign, rows, query).length > 0;
}

export function campaignHref(campaign) {
  return "/results?q=" + encodeURIComponent(campaign.campaign_id) + "#run-records";
}
