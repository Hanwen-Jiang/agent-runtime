# Evaluation website

The three public routes (`/`, `/results`, `/datasets`) share a React 19 / HeroUI 3 application. The structure, palette and typography follow `SITE-UI.fig`; component composition and restrained surfaces follow the HeroUI v3 dashboard template. It uses HeroUI Button, Card, Chip, Input and Skeleton, and the Pro NativeSelect web component.

## Build

Install the exact dependencies from `package.json` using an account with access to HeroUI Pro, then run `npm run build`. The committed output lives in `../site_pages/assets/`; the learning image only needs Python to serve it. IBM Plex font files are self-hosted under their included OFL license. Production never downloads packages, fonts or scripts from a third party.

When licensed packages already exist in a separate local installation, set `HEROUI_MODULES` to that `node_modules` directory; `BUILD_MODULES` and `FONT_MODULES` can independently point to build tools and Fontsource packages. These are build-time paths only and are not written into the output.

## Data

- `/api/live.json`: live VictoriaMetrics evaluation records, polled every 20 seconds while the page is visible. Returning to the page triggers an immediate refresh. Event timestamps and last fetch timestamps are displayed separately. Counts are formatted as counts; only explicitly identified ratio metrics become percentages.
- `/api/site.json` and `/api/datasets/<name>.json`: catalog, question contents, scoring views and provenance; refreshed every minute. Held-out questions remain filtered by the existing backend.
- `/api/reports.json`: public historical report summaries, refreshed every minute. The archive is separate from recent metrics to avoid presenting the same evaluation as two new runs. Existing contexts, benchmarks, tuning confidence intervals and failure summaries remain accessible.

`python scripts/eval_dashboard.py --public ... --out DIR/index.html` now writes `DIR/reports.json` atomically alongside the compatibility HTML. Publish `reports.json` in the evaluation archive volume. Existing deployments that mount only `index.html` continue to work through strict JSON extraction; the application never serves that HTML as the results page. Archive refreshes do not require rebuilding the UI.

The server only serves explicitly allowlisted assets and JSON routes. There are no analytics, third-party requests, or embedded report values in the application bundle.

## 每次运行的选题说明（ⓘ）

结果页按运行读取 `selection_info`，数据集概览不能替代本次运行的清单。新评测在**选题时**记录数据集版本、原始池和筛选后数量、最终选中数量，以及实际使用的方法（全量、指定 ID、预定义划分、带种子抽样、分层或冻结题单）。记录适用的种子、分层规则、排除条件、题单指纹和可公开的来源。多模型、策略和重复执行要说明是几道不同的题、几次执行；异质能力矩阵按评测组件拆开。

例如历史 SWE Verified 的一个协议是 500 题中排除 `psf/requests` 的 8 题，从 492 题按仓库配额、仓内难度抽到 50 题，种子 0。只有协议及数据指纹吻合的运行才可引用这些数字。Terminal-Bench 的固定清单如果没有记录如何抽得，列出已知题号并标明抽法未记录。留出或私有集仅展示公开统计和指纹，不发原题、答案、私有题号或文件路径。

旧记录缺少协议时显示已知字段和“本次运行未记录选题方法”。当前目录大小、完成数、每题多个策略的执行数都不能反推一个历史运行的入选题数。新增数据源时用一份已知清单和一份缺失/封存记录验证服务端 JSON 及 ⓘ 弹窗。

## Validation

Run the existing `test_eval_dashboard_server.py`, `test_eval_site_server.py`, `test_dashboard_live.py`, `test_eval_dashboard.py` and `test_eval_site_pages.py` suites. Set `AR_TEST_BROWSER` and `AR_REQUIRE_BROWSER=1` for required browser coverage. The page suite exercises nonempty historical reports, catalog counts, search, grouping, pairwise question expansion, language switching and desktop/mobile overflow against the actual HTTP service.
