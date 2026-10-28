"""全部网关的计量合计：按网关、按模型；不按租户过滤。用法：total.py <名字=文件>…"""

import collections
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
grand = 0
models: collections.Counter = collections.Counter()
calls: collections.Counter = collections.Counter()
fails: collections.Counter = collections.Counter()
for arg in sys.argv[1:]:
    name, _, path = arg.partition("=")
    rs = [json.loads(line) for line in open(path, encoding="utf-8") if line.startswith("{")]
    total = sum(r["cost_usd_micros"] for r in rs)
    grand += total
    for r in rs:
        m = r["model_profile_id"].split("/")[1]
        models[m] += r["cost_usd_micros"]
        calls[m] += 1
        if r["status"] != "ok":
            fails[(m, r.get("error_code"))] += 1
    print(f"{name:<12} calls {len(rs):>4}  ${total / 1e6:.2f}")
print(f"合计 ${grand / 1e6:.2f}")
for m, c in models.most_common():
    print(f"  {m:<32} {calls[m]:>4} 次  ${c / 1e6:.2f}")
print("失败：", dict(fails))
