# SonarQube Cloud Baseline — first main analysis

**Project:** `mitchins_yt2wiki` (`mitchins` org) `https://sonarcloud.io/dashboard?id=mitchins_yt2wiki&branch=main`
**Main revision:** `625aa2e93b89e86268e65b987dd0309cd62f219b` (PR #5 squash)
**GitHub Actions run:** `39766235084` — `CI` `push: main` `2026-10-22T10:51:33Z` — jobs `Python tests and coverage: success` + `SonarQube quality gate: failure` (job `119782765681` step `Analyze with SonarQube and enforce quality gate` exit 3)
**Sonar analysis:** `f2255b38-d61d-4e9b-b859-bfefa0dad05a` `2026-10-22T10:53:05+0000` `revision 625aa2e` `branch main (LONG)` `CE task AaFjPvMBrrDFyHa4C9TU SUCCESS` — `branch list` shows 1 branch `main` with `bugs 3 vulnerabilities 5 codeSmells 305`
**PR #5 reference:** `4ba9ddd` `pullRequests[0].key 5` `qualityGateStatus OK` (`bugs 0 vulnerabilities 0 codeSmells 0`) — PR gate computed, main not.

Retrieved `2026-10-22` via `sonar-project.properties` (`sonar.projectKey=mitchins_yt2wiki`, `sonar.organization=mitchins`, `sonar.sources=src/yt2wiki`, `sonar.tests=tests`, `sonar.python.version=3.12`, `sonar.python.coverage.reportPaths=coverage.xml`), `gh run view 39766235084 --log`, `gh api repos/mitchins/yt2wiki/commits/625aa2e/check-runs`, `https://sonarcloud.io/api/qualitygates/get_by_project?project=mitchins_yt2wiki&organization=mitchins`, `https://sonarcloud.io/api/qualitygates/list?organization=mitchins`, `https://sonarcloud.io/api/measures/component?component=mitchins_yt2wiki&branch=main&metricKeys=...`, `https://sonarcloud.io/api/measures/search_history`, `https://sonarcloud.io/api/qualitygates/project_status?projectKey=...&branch=main` (+ `&pullRequest=5`, `&analysisId=...`) authenticated with `SONAR_TOKEN` (`114f7...`), `https://sonarcloud.io/api/ce/task?id=AaFjPvMBrrDFyHa4C9TU`, `https://sonarcloud.io/api/project_branches/list`, `https://sonarcloud.io/api/project_analyses/search`, `https://sonarcloud.io/api/issues/search?types=BUG,VULNERABILITY&ps=100`, `https://sonarcloud.io/api/settings/values?keys=sonar.leak.period*` and dashboard `neutral` check title.

## Measurements (overall code)

| Metric | Value | Source |
|---|---|---|
| Coverage (overall) | 59.2% | `measures/component` `coverage` |
| Line coverage | 61.9% | `line_coverage` |
| Branch coverage | 50.0% | `branch_coverage` |
| Lines to cover | 8,076 | `lines_to_cover` |
| ncloc | 13,119 | `ncloc` |
| Bugs | 3 | `bugs` / `project_branches/list` |
| Vulnerabilities | 5 | `vulnerabilities` |
| Code smells | 305 | `code_smells` (314 total issues, 8 BUG+VULN) |
| Duplication | 0.1% | `duplicated_lines_density` |
| Security hotspots | 0 | `security_hotspots` |
| Scan scope | `79` source files, `46` py + `15` js analysed, `3` CPD-excluded | scanner log `SCM Publisher 79/79`, `CPD 58 files`, `Architecture 46 py / 3 js` |

Coverage import succeeded: `Sensor Cobertura Sensor for Python coverage [python] Parsing report '/home/runner/work/yt2wiki/yt2wiki/coverage.xml' (done)` + `Sensor Zero Coverage Sensor (done)` — not a 0% import problem. Value 59.2% tracks CI's `7733` coverable / `5001` covered, `2454` branches / `1228` covered (≈61.2% combined) within line/branch weighting.

## Assigned quality gate

`api/qualitygates/get_by_project?project=mitchins_yt2wiki&organization=mitchins` →

```json
{"qualityGate": {"id": 9, "name": "Sonar way", "default": true}}
```

`api/qualitygates/show?organization=mitchins&id=9`:

| id | metric | op | error threshold |
|---|---|---|---|
| 35 | `new_security_rating` | GT | 1 |
| 36 | `new_reliability_rating` | GT | 1 |
| 37 | `new_maintainability_rating` | GT | 1 |
| 38 | `new_coverage` | LT | 80 |
| 39 | `new_duplicated_lines_density` | GT | 3 |
| 76287 | `new_security_hotspots_reviewed` | LT | 100 |

`isBuiltIn: true`, `isCleanAsYouCode: true`. No overall-code conditions. The `80` you see is **new-code** coverage, not overall.

PR #5's `project_status?pullRequest=5` returns `status OK` with 4 conditions actually evaluated:

```
new_reliability_rating GT 1 → actual 1 OK
new_security_rating GT 1 → actual 1 OK
new_maintainability_rating GT 1 → actual 1 OK
new_security_hotspots_reviewed LT 100 → actual 100.0 OK
```

`new_coverage` and `new_duplicated_lines_density` absent because that PR added no new lines to cover (`new_lines_to_cover` empty) — expected for a `ci/`-only change.

## Current new-code definition and baseline validity

- Instance/org default: `sonar.leak.period.type: previous_version`, `sonar.leak.period: previous_version` (`api/settings/values?keys=sonar.leak.period*` `parentOrigin: INSTANCE`). No project-specific override.
- `project_analyses/search?branch=main` → `projectVersion: "not provided"`, `manualNewCodePeriodBaseline: false`.
- `measures/search_history?metrics=new_coverage,new_lines_to_cover,...` → history entries for `2026-10-22T10:53:05+0000` have **no value** (empty) for all `new_*`.
- `measures/component?branch=main&metricKeys=new_coverage,…` → `measures: []`.

→ First main analysis has **no valid new-code baseline**. With `previous_version` and version `not provided`, SonarCloud has nothing to diff against, so the leak period is empty.

## Exact quality-gate failure

`api/qualitygates/project_status?projectKey=mitchins_yt2wiki&branch=main` (and with `&analysisId=f2255...`, with or without token):

```json
{"projectStatus": {"status": "NONE", "conditions": [], "periods": []}}
```

`measures/component?branch=main&metricKeys=alert_status` → `measures: []`. `search_history?metrics=alert_status` → history entry with **no value** (empty). `ce/task?id=AaFjPvMBrrDFyHa4C9TU` → `status SUCCESS`, `warnings []`.

GitHub check runs for `625aa2e`:

- `SonarCloud Code Analysis: neutral` — `output.title: "Quality Gate not computed"`, `summary: "See analysis details on SonarQube Cloud …"` 
- `SonarQube quality gate: failure` — workflow job failure, annotation `Action failed: The process '/opt/.../sonar-scanner' failed with exit code 3` — scanner step `Analyze with SonarQube and enforce quality gate` with `args: -Dsonar.python.coverage.reportPaths=coverage.xml -Dsonar.qualitygate.wait=true -Dsonar.qualitygate.timeout=300` waited `10:53:55.348 max 300s` then `10:53:56.267 ERROR QUALITY GATE STATUS: FAILED - View details on https://sonarcloud.io/dashboard?id=mitchins_yt2wiki&branch=main` + `EXECUTION FAILURE Total time: 1:02.229s`.

So the SonarCloud service reports **Quality Gate not computed (NONE)**, which the SonarCloud GitHub App correctly surfaces as neutral. The workflow's `sonarqube-scan-action` with `sonar.qualitygate.wait=true` treats the not-computed state as `FAILED` (exit 3). This is **not** an 80% overall-coverage failure:

- The gate has no overall condition to fail.
- `new_coverage LT 80` was not evaluated (`periods: []`, no `actualValue`), so 59.2% did not trigger it.
- `overall` vs `new` distinction: PR #5 OK proves new-code gate passes when there is nothing new; main NONE proves there is no new-code window to evaluate.

Do **not** assume `coverage 59.2% < 80` caused the failure. The evidence is `conditions: []`.

If the API had been inaccessible we would note it; it was accessible and returned NONE consistently (unauthenticated and with `114f7...` bearer).

## Proposed disposition (no threshold change in this phase)

1. **Preserve the report** — keep run `39766235084`, analysis `f2255b38`, and dashboard link as baseline.
2. **Do not raise/lower thresholds** — `Sonar way` new-code thresholds (especially `new_coverage 80`) remain as intended Clean as You Code.
3. **Establish a baseline for main** so new-code gates can be evaluated:
   - Option A (preferred, minimal): set an explicit project version (e.g. `0.2.0` via `sonar.projectVersion` in `sonar-project.properties` or via `api/project_analyses/search` version event) and use `previous_version` — the first analysis at that version establishes the baseline and will still report `NONE` (no new code to compare); the *subsequent* analysis with changed code will then have a valid period with `new_lines_to_cover` and gate conditions will be computed. Require a subsequent analysis before expecting the new-code gate to be evaluated. Reversible, no file exclusions.
   - Option B: use a reference-branch or N-day leak period if versioning is not wanted. Requires project-specific new-code definition via SonarCloud project settings (not org default).
   - In either case, document the chosen baseline and date; keep historical overall issues (305 smells, 59.2% overall) visible as debt, not hidden by exclusions or fake coverage.
4. **Fix the CI wait semantics** if `NONE` should not fail the pipeline: consider `sonar.qualitygate.wait=false` for the first main analysis or make the workflow tolerate `NONE` (check `alert_status` instead of scanner exit) until a baseline exists. Do not change branch protection until verified — keep `Python tests and coverage` required, `SonarQube quality gate` conditional on non-fork (`if: github.actor != 'dependabot[bot]' && (event_name != 'pull_request' || head.repo.full_name == repository)`).
5. **Proceed to Mission 2/3** on the 8 BUG+VULN findings regardless of gate not-computed.

**Gate A:** cause established — gate not-computed due to missing new-code baseline (`previous_version` + `not provided`), not an overall 80% coverage failure. Evidence preserved above.

## Post-merge verification — `edb56f9` (`2026-10-22T22:09:01+0000`)

**PR #9 merged:** `edb56f9df053b306e130febdf58ea69eeeb79a9b` — `fix/sonar: harden upload path, async file, frame_out, test stability (#9)` squash merge `2026-10-23T09:07:34+1100`.
**Second analysis:** `d42c4ef8-6979-4251-9cf4-47baadfc593a` `2026-10-22T22:09:01+0000` `revision edb56f9` (previous `f2255b38` `625aa2e` `2026-10-22T10:53:05+0000`).
**Project version still `not provided`** (`sonar-project.properties` had no `sonar.projectVersion`).

Current live Sonar main status (`2026-10-22T22:29Z`, `api/qualitygates/project_status?projectKey=mitchins_yt2wiki&branch=main`):

- `status: OK` (was `NONE` on first analysis)
- 6 conditions all `OK`: `new_reliability_rating 1`, `new_security_rating 1`, `new_maintainability_rating 1`, `new_coverage 90.0 LT 80 OK`, `new_duplicated_lines_density 0.0 GT 3 OK`, `new_security_hotspots_reviewed 100.0 LT 100 OK`
- `periods: [{index:1, mode:"previous_version", date:"2026-10-22T10:53:05+0000"}]` — new-code window now defined against first analysis even with `not provided` versions; `measures/component` `coverage 59.3%` `new_coverage 90.0%` `new_lines_to_cover 32` `bugs 0` `vulnerabilities 4` `code_smells 306`.
- Gate remains `Sonar way` id 9 `isCleanAsYouCode:true` — the six `new_*` conditions unchanged (thresholds `1/1/1/80/3/100`), no overall condition.
- `project_branches/list` `main` `qualityGateStatus OK` `analysisDate 2026-10-22T22:09:01+0000`.
- `measures/search_history` still shows empty `new_coverage` history values (SonarCloud internal), but `project_status` period is authoritative and proves evaluation.

This confirms the earlier `NONE` was due to a single-analysis `previous_version` with no predecessor to diff against, not a persistent misconfiguration. However relying on `not provided` versioning is not reproducible — the next version event would otherwise inherit `not provided` and make history fragile.

### Baseline now established (tracked and reproducible)

To make the baseline reproducible and align with package versioning:

- `sonar-project.properties:8` now sets `sonar.projectVersion=0.1.0` — matching `pyproject.toml:3` `version = "0.1.0"`. Prefer existing package version over inventing `0.2.0`; no documented release-version reason to diverge.
- Existing `previous_version` new-code policy retained (`api/settings/values` `sonar.leak.period.type=previous_version` `INSTANCE` origin, no project override). No threshold lowering, no coverage exclusions, no historical-issue suppression.
- **Effective version:** `0.1.0` from this PR onward. The first analysis after merge will carry `projectVersion 0.1.0` and establish the version baseline; it may report `NONE`/empty new-code until a subsequent changed-code analysis provides `new_lines_to_cover` to evaluate — a later analysis with changes is required to verify `new_coverage LT 80` etc. Do not suppress scanner `qualitygate.wait` failure to obtain green; keep wait semantics.
- **Baseline commits/analyses:** `625aa2e`/`f2255b38` (initial), `edb56f9`/`d42c4ef8` (PR #9 merge) as historical; next `p2a`+`p2b` PRs will validate against `0.1.0`.
- Overall coverage and historical issues remain visible: `59.3%` overall, `306` smells etc. are not excluded; `80%` applies only to new code as per Clean as You Code.

Acceptance per P2 PR A: sonar config tracked (`sonar-project.properties`), baseline identifiable (`edb56f9`/`d42c4ef8` + `0.1.0`), subsequent changed-code gate verifies conditions, `80%` new-code intact, overall visible.

---
*Generated from live SonarCloud APIs 2026-10-22T10:53:05Z and 2026-10-22T22:29Z; dashboard remains at https://sonarcloud.io/dashboard?id=mitchins_yt2wiki&branch=main .*
