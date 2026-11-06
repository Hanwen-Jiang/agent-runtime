# 页面真实浏览器验收（2026-11-06）

独立 worktree `page-live`，基线 `c3660e33`。复用 `test_eval_site_pages.py`，浏览器为独立临时容器中的 Chromium 156.0.8078.79。原始真实报告 `/opt/evals/reports/20261102-1231` 只读挂载，六份 task-loop 报告的路径和摘要另存执行证据。没有模型调用或部署，费用 $0。

## 发现与修复

原有六项测试在真实浏览器中通过且零 skip，但它只检查数据集页的手机宽度，未断言桌面宽度。对实际部署 `https://evals.cgent.link` 的原生 CDP 验证发现：1400px 桌面窗口，数据集页 clientWidth=1385、scrollWidth=1828，横向溢出443px。来源标记中真实 JudgeBench 等长题号触发第三列 auto 扩展；这不是测试生成的假数据。

修复将数据集摘要第三列改为有限比例列，来源标记可在所有视口宽度换行，保留完整文字而非裁切隐藏。补桌面初始/展开后溢出断言。新增断言在修改CSS前真实失败，修改后六项全部通过（16.82s、0 skip）。

## 证据与部署限制

证据根目录：`/root/proj/agent-projs/.goal-evidence/page-live-20261106`。

- `local-pages.log/xml`：原六项通过；`local-desktop-regression-before.log`：新增桌面回归真实失败；`local-pages-fixed.log/xml`：修复后六项通过。
- `deployed-before-fix/live-browser.json` 和六张截图：直接访问部署的主页、数据集页、结果页，桌面1400×900与手机390×844，共六组；五过、一失败（数据集桌面溢出）。没有HTTP代理重写页面或证书绕过。
- 线上实际19个数据集、517个公开题；检查页面计数与 `/api/site.json` 一致，分组/搜索/成对题展开/语言切换正常。结果页检查实际报告行数及每个可用分诊分数与页面内已发布数据一致，表格可展开。
- `live-browser-smoke.py`：原生Chromium CDP pipe驱动，复用现有测试探针，每页独立浏览器上下文，保留JS异常、HTTP状态、真实数据摘要和截图；不新增浏览器自动化依赖。
- `manifest.json`：输入与结果摘要；`deployment-before.json`：实际部署镜像 `ghcr.io/agentruntime/learning:809cb41`。

修复后的本地验收不代表旧部署已修好。需要主集成负责人发布包含此修改的learning镜像，再对部署三页重跑。保留此前失败产物，不覆盖原始评测数据。临时容器 `goal-page-browser-20261106` 留到最终部署复验后清理。

本地复验环境：`AR_TEST_BROWSER=/usr/bin/chromium AR_REQUIRE_BROWSER=1 AR_REQUIRE_REAL_REPORTS=1 AR_EVAL_SITE_REPORTS=/reports PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，源码PYTHONPATH指向当前worktree；运行 `python -m pytest -p pytest_asyncio.plugin -p no:cacheprovider services/py-learning/tests/test_eval_site_pages.py -q`。
