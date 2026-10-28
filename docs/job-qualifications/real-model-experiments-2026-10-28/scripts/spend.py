"""网关计量记录的汇总：调用数、花费、按模型与 role、失败的错误码。用法：spend.py <stdout 日志>…"""

import collections
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
for path in sys.argv[1:]:
    rs = [json.loads(line) for line in open(path, encoding="utf-8") if line.startswith("{")]
    rs = [r for r in rs if r.get("tenant_id") == "evals"]
    by = collections.defaultdict(lambda: [0, 0])
    for r in rs:
        k = (r["model_profile_id"].split("/")[1], r["role"])
        by[k][0] += 1
        by[k][1] += r["cost_usd_micros"]
    errors = collections.Counter((r["model_profile_id"].split("/")[1], r["role"], r.get("error_code")) for r in rs if r["status"] != "ok")
    print(f"{path[-45:]}: calls {len(rs)} spent ${sum(r['cost_usd_micros'] for r in rs) / 1e6:.2f} errors {dict(errors)}")
    for k, (n, c) in sorted(by.items(), key=lambda x: -x[1][1])[:6]:
        print(f"    {k} {n} ${c / 1e6:.3f}")
