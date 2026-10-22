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
   - Option A (preferred, minimal): set an explicit project version (e.g. `0.2.0` via `sonar.projectVersion` in `sonar-project.properties` or via `api/project_analyses/search` version event) and use `previous_version` — next analysis will have a valid period with `new_lines_to_cover` and gate conditions will be computed. Reversible, no file exclusions.
   - Option B: use a reference-branch or N-day leak period if versioning is not wanted. Requires project-specific new-code definition via SonarCloud project settings (not org default).
   - In either case, document the chosen baseline and date; keep historical overall issues (305 smells, 59.2% overall) visible as debt, not hidden by exclusions or fake coverage.
4. **Fix the CI wait semantics** if `NONE` should not fail the pipeline: consider `sonar.qualitygate.wait=false` for the first main analysis or make the workflow tolerate `NONE` (check `alert_status` instead of scanner exit) until a baseline exists. Do not change branch protection until verified — keep `Python tests and coverage` required, `SonarQube quality gate` conditional on non-fork (`if: github.actor != 'dependabot[bot]' && (event_name != 'pull_request' || head.repo.full_name == repository)`).
5. **Proceed to Mission 2/3** on the 8 BUG+VULN findings regardless of gate not-computed.

**Gate A:** cause established — gate not-computed due to missing new-code baseline (`previous_version` + `not provided`), not an overall 80% coverage failure. Evidence preserved above.

---
*Generated from live SonarCloud APIs 2026-10-22T10:5xZ; dashboard remains at https://sonarcloud.io/dashboard?id=mitchins_yt2wiki&branch=main .*
