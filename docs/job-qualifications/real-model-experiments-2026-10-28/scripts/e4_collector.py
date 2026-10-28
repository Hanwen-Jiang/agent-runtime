"""E4：从 Chart 里的 Collector 配置派生一份本机用的：只留 Learning 那一路（同样的 memory_limiter、脱敏、语义过滤、batch、kafka 导出器），
另把同一路写一份文件便于核对；指标与日志收下丢给 debug。用法：e4_collector.py <chart 配置> <输出> <OTLP HTTP 地址> <broker> <span 文件>"""

import sys
from pathlib import Path

import yaml

src, out, otlp, brokers, spans = sys.argv[1:6]
cfg = yaml.safe_load(Path(src).read_text("utf-8"))
cfg["receivers"]["otlp"]["protocols"] = {"http": {"endpoint": otlp}}
exporters = cfg["exporters"]
for name in ("otlp/tempo", "otlphttp/langfuse"):
    exporters.pop(name)
exporters["kafka"]["brokers"] = [brokers]
exporters["file/spans"] = {"path": spans}
pipelines = cfg["service"]["pipelines"]
for name in ("traces/tempo", "traces/langfuse"):
    pipelines.pop(name)
pipelines["traces/learning"]["exporters"] = ["kafka", "file/spans"]
cfg["extensions"]["health_check"]["endpoint"] = "127.0.0.1:13133"
Path(out).write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), "utf-8")
print(yaml.safe_dump(cfg["service"], sort_keys=False))
