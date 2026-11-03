# Workspace: hermes-duplex

Default branch: main   Remote: robsgreen/hermes-duplex   GitHub: yes

Notes: `origin/HEAD` is not set locally; `main` comes from the GitHub REST API (`default_branch`) and matches `origin/main`. The main checkout is on `claude/upbeat-clarke-1vcz7v` (same commit as `main`, 0fd20c6). The repo holds only `README.md` (one line: `# hermes-duplex`). Intended stack (from the dispatch, not from files): a Python plugin for Nous Research's Hermes Agent, likely uv + pytest + ruff. Nothing below is established until the first spec adds the files; re-run workspace setup after `pyproject.toml` and CI land.

## Capabilities

| Capability | Status |
|------------|--------|
| Install | ❌ Not yet established — no `pyproject.toml`, lockfile, `Makefile`, or CI workflow |
| Build | ❌ Not yet established — no build config |
| Type-check | ❌ Not yet established — no type-checker config |
| Lint/Format | ❌ Not yet established — no ruff/linter config |
| Unit Tests | ❌ Not yet established — no tests or test runner config |
| Integration Tests | ❌ Not supported — no suite, `docker-compose*.yml`, or `.env.example` |
| Local Dev | ❌ Not yet established — no dev scripts |
| Logging | ❌ Not established — no source code |
| Metrics | ❌ Not established — no source code |

## Modules
- `.` — hermes-duplex: planned Python plugin for Hermes Agent; currently only `README.md`  (build: not established, test: not established)

## Module Dependencies

No intra-repo dependencies — all modules are independent.

## Docs Layout
None yet. No `AGENTS.md`, `CLAUDE.md`, `docs/system/`, or `docs/specs/`. Use the `doc-standards` layout (`AGENTS.md` + `CLAUDE.md` importing it, `docs/system/architecture.md`, `docs/system/modules/`, `docs/specs/YYYYMM/`) once code exists.

## Integration Tests
NOT SUPPORTED — no integration/e2e suite, `docker-compose*.yml`, Testcontainers, or `.env.example` in the repo.

## Local Development
NOT SUPPORTED — no dev scripts or config yet.

## Observability
### Logging
NOT ESTABLISHED — no source code.

### Metrics
NOT ESTABLISHED — no source code.
