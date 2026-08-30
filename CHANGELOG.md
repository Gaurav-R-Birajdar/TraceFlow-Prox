# Changelog

All notable changes to TraceFlow Proxy will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [v0.5] — 2026-08-30

### Added
- `README.md` fully rewritten with complete project documentation including:
  - Mermaid architecture diagrams (system overview, request lifecycle, component map)
  - Full incident log: what broke in each phase, root cause, and exact fix applied
  - PowerShell vs Unix curl compatibility guide with side-by-side comparison table
  - Data flow deep dive: latency measurement, async DB write architecture, three-state schema model
  - Complete API reference for all four endpoints with request/response examples
  - SQLite schema documentation with PRAGMA explanations
  - Step-by-step setup guide targeting Windows/PowerShell
  - Design principles table and full project roadmap

---

## [v0.4] — 2026-08-30

### Added
- `GET /analytics` endpoint: single-query SQLite aggregation returning `total_inferences`, `average_latency_ms`, `schema_pass_rate`, `schema_pass_count`, `schema_fail_count`, `schema_null_count`
- Graceful empty-database fallback — `average_latency_ms` and `schema_pass_rate` return `null` when no traces exist
- `schema_null_count` field separates unevaluated rows from pass/fail denominator — prevents Phase 1 `NULL` rows from corrupting the pass-rate metric
- `Reports/0.3.1.md` Phase 3 session report (git-ignored)

### Changed
- Version bumped to `0.3.0` in module docstring, FastAPI constructor, and `/health` route

---

## [v0.3] — 2026-08-30

### Added
- `MathResponse` Pydantic model (`answer: int`, `explanation: str`) as the deterministic output schema for LLM validation
- Phase 2 validation block in `proxy_generate`: `MathResponse.model_validate_json(raw_response)` runs between timer stop and DB write
- `ValidationError` imported from `pydantic` for semantically precise exception handling
- `Reports/0.2.1.md` Phase 2 session report (git-ignored)

### Changed
- Ollama request body now includes `"format": "json"` — constrains the sampler at the runtime level to emit valid JSON
- `schema_passed` field in `TraceRecord` and `GenerateResponse` now populated with `True`/`False` (was always `None` in Phase 1)
- Version bumped to `0.2.0` in module docstring, FastAPI constructor, and `/health` route

---

## [v0.2] — 2026-08-30

### Added
- `.venv/` Python virtual environment via `python -m venv .venv` — isolates project from global Python install
- `Reports/0.2v.md` session report documenting environment setup, Ollama verification, and PowerShell curl fix

### Changed
- Dependency installation target moved from global Python to `.venv\Scripts\pip`

### Fixed
- PowerShell `curl` alias conflict — documented three working alternatives: `curl.exe`, `Invoke-RestMethod`, and `ConvertTo-Json` hashtable pattern

---

## [v0.1] — 2026-08-29

### Added
- `proxy.py`: Single-file FastAPI application implementing TraceFlow Proxy Phase 1
- `POST /proxy/generate` route: async interceptor that forwards requests to local Ollama instance
- `GET /health` liveness probe returning version and status
- `GET /traces` route for offline evaluation — returns N most recent traces ordered by timestamp
- SQLite database `trace_logs.db` with `inference_traces` table (auto-provisioned on startup)
- WAL journal mode + `synchronous=NORMAL` PRAGMA for concurrent-safe writes
- `idx_inference_traces_timestamp` index for efficient time-ordered queries
- High-precision latency measurement via `time.perf_counter()` around the full Ollama round-trip
- Async fire-and-forget DB logging via `asyncio.create_task()` — never blocks the response path
- Pydantic v2 models: `GenerateRequest`, `GenerateResponse`, `TraceRecord`
- `lifespan` context manager for clean SQLite connection open/close tied to FastAPI startup/shutdown
- `requirements.txt` with pinned dependencies (fastapi, uvicorn, httpx, aiosqlite, pydantic)
- `.gitignore` excluding SQLite runtime files, venv, Reports folder, and Python cache
- `Reports/0.1v.md` session report documenting architecture, schema, routes, and Phase 2 roadmap
- `CHANGELOG.md` (this file)
