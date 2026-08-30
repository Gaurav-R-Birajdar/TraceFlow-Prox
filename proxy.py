"""
TraceFlow Proxy — Phase 2
=========================
Lightweight observability middleware that sits between any application
and a local Ollama instance (Llama 3.1).  Every generation request is
intercepted, timed at high precision, validated against a deterministic
Pydantic schema (MathResponse), and the complete trace — including the
schema_passed flag — is persisted asynchronously to a local SQLite database.

Author  : TraceFlow Proxy Project
Version : 0.3.0
Python  : >=3.11
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

import aiosqlite
import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL: str = "http://localhost:11434"
OLLAMA_GENERATE_ENDPOINT: str = f"{OLLAMA_BASE_URL}/api/generate"
SQLITE_DB_PATH: str = "trace_logs.db"
OLLAMA_REQUEST_TIMEOUT: float = 120.0  # seconds — generous for local LLM warm-up

# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------


class GenerateRequest(BaseModel):
    """Payload accepted by the /proxy/generate endpoint."""

    model: str = Field(
        default="llama3.1",
        description="Ollama model tag to target (e.g. 'llama3.1', 'llama3.1:8b').",
        examples=["llama3.1"],
    )
    system_prompt: str = Field(
        ...,
        description="System-level instruction injected before the user turn.",
        min_length=1,
    )
    user_prompt: str = Field(
        ...,
        description="User message forwarded to the model.",
        min_length=1,
    )


class GenerateResponse(BaseModel):
    """Structured response returned to the caller after interception."""

    trace_id: str = Field(description="UUID v4 uniquely identifying this trace.")
    model: str = Field(description="Model that produced the response.")
    raw_response: str = Field(description="Full text output from Ollama.")
    latency_ms: float = Field(description="End-to-end Ollama inference latency in milliseconds.")
    schema_passed: Optional[bool] = Field(
        default=None,
        description="Result of deterministic schema validation (Phase 2). None in Phase 1.",
    )
    timestamp: str = Field(description="UTC ISO-8601 timestamp of the completed trace.")


class TraceRecord(BaseModel):
    """Internal DTO written to the SQLite inference_traces table."""

    trace_id: str
    timestamp: str
    model: str
    system_prompt: str
    user_prompt: str
    raw_response: str
    latency_ms: float
    schema_passed: Optional[bool]


# ---------------------------------------------------------------------------
# Phase 2 — Deterministic Schema Validator
# ---------------------------------------------------------------------------


class MathResponse(BaseModel):
    """Ground-truth schema for math-related LLM responses.

    Ollama is instructed to emit JSON via ``"format": "json"``.
    This model is the contract against which the raw output is validated.
    A ``ValidationError`` is treated as a structural drift event (hallucination
    proxy) and recorded as ``schema_passed = False`` in the trace log.
    """

    answer: int = Field(description="The numeric answer to the math problem.")
    explanation: str = Field(
        description="A brief natural-language explanation of the answer.",
    )


# ---------------------------------------------------------------------------
# Database Layer
# ---------------------------------------------------------------------------

# Module-level connection handle, initialised during lifespan startup.
_db: Optional[aiosqlite.Connection] = None

_DDL_INFERENCE_TRACES: str = """
CREATE TABLE IF NOT EXISTS inference_traces (
    trace_id      TEXT    PRIMARY KEY,
    timestamp     TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    system_prompt TEXT    NOT NULL,
    user_prompt   TEXT    NOT NULL,
    raw_response  TEXT    NOT NULL,
    latency_ms    REAL    NOT NULL,
    schema_passed INTEGER             -- NULL: not yet evaluated; 1: pass; 0: fail
);
"""

_INDEX_DDL: str = """
CREATE INDEX IF NOT EXISTS idx_inference_traces_timestamp
    ON inference_traces (timestamp);
"""


async def _init_db() -> aiosqlite.Connection:
    """Open the SQLite connection and provision the schema if absent."""
    db = await aiosqlite.connect(SQLITE_DB_PATH)
    # WAL mode: concurrent readers do not block a writer.
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")
    await db.execute(_DDL_INFERENCE_TRACES)
    await db.execute(_INDEX_DDL)
    await db.commit()
    return db


async def _persist_trace(record: TraceRecord) -> None:
    """
    Insert a single TraceRecord into inference_traces.

    Fire-and-forget: failures are logged to stderr but never propagate
    to the client-facing response path — observability infra must not
    crash the hot path.
    """
    global _db
    if _db is None:
        # Safety guard — should not happen after successful lifespan init.
        return

    schema_flag: Optional[int]
    if record.schema_passed is None:
        schema_flag = None
    else:
        schema_flag = 1 if record.schema_passed else 0

    try:
        await _db.execute(
            """
            INSERT INTO inference_traces
                (trace_id, timestamp, model, system_prompt,
                 user_prompt, raw_response, latency_ms, schema_passed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.trace_id,
                record.timestamp,
                record.model,
                record.system_prompt,
                record.user_prompt,
                record.raw_response,
                record.latency_ms,
                schema_flag,
            ),
        )
        await _db.commit()
    except Exception as exc:  # noqa: BLE001
        import sys
        print(
            f"[TraceFlow] DB write error for trace {record.trace_id}: {exc}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Application Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage SQLite connection lifecycle tied to FastAPI startup/shutdown."""
    global _db
    _db = await _init_db()
    print(f"[TraceFlow] SQLite initialised  -> {SQLITE_DB_PATH}")
    print(f"[TraceFlow] Ollama target        -> {OLLAMA_GENERATE_ENDPOINT}")

    yield  # application runs here

    if _db:
        await _db.close()
        print("[TraceFlow] SQLite connection closed cleanly.")


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="TraceFlow Proxy",
    description=(
        "Lightweight observability middleware that intercepts Ollama generation "
        "requests, measures inference latency, validates responses against "
        "deterministic schemas, and logs complete traces to a local SQLite database."
    ),
    version="0.3.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", tags=["ops"], summary="Liveness probe")
async def health_check() -> dict[str, str]:
    """Returns 200 OK when the proxy is running."""
    return {"status": "ok", "version": "0.3.0"}


@app.post(
    "/proxy/generate",
    response_model=GenerateResponse,
    status_code=status.HTTP_200_OK,
    tags=["proxy"],
    summary="Intercept & forward a generation request to the local Ollama instance",
)
async def proxy_generate(payload: GenerateRequest, request: Request) -> JSONResponse:
    """
    Core interception route.

    Flow
    ----
    1. Construct the Ollama-native request body.
    2. Start a high-precision timer (time.perf_counter).
    3. Forward the request to Ollama via an async httpx client.
    4. Stop the timer the exact moment the response completes.
    5. Parse the response text.
    6. Dispatch an async background task to persist the trace to SQLite.
    7. Return the structured GenerateResponse to the caller immediately.

    Error handling
    --------------
    - 502 Bad Gateway     -- Ollama is unreachable or returns an HTTP error.
    - 504 Gateway Timeout -- Ollama did not respond within OLLAMA_REQUEST_TIMEOUT.
    """
    trace_id: str = str(uuid.uuid4())
    timestamp: str = datetime.now(timezone.utc).isoformat()

    # Concatenate system + user prompts into a single structured prompt string
    # compatible with the vanilla Ollama /api/generate endpoint (no chat API needed).
    ollama_prompt: str = (
        f"<|system|>\n{payload.system_prompt}\n"
        f"<|user|>\n{payload.user_prompt}\n"
        f"<|assistant|>"
    )

    ollama_body: dict = {
        "model": payload.model,
        "prompt": ollama_prompt,
        # stream=False: consume the full response atomically for latency accuracy.
        "stream": False,
        # format=json: instructs Ollama to guarantee a valid JSON object output.
        # Phase 2 requirement — prerequisite for deterministic schema validation.
        "format": "json",
    }

    # ── High-precision timer starts here ────────────────────────────────────
    t_start: float = time.perf_counter()

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_REQUEST_TIMEOUT) as client:
            ollama_response = await client.post(OLLAMA_GENERATE_ENDPOINT, json=ollama_body)
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"TraceFlow Proxy could not connect to Ollama at {OLLAMA_BASE_URL}. "
                f"Ensure Ollama is running locally (`ollama serve`). Error: {exc}"
            ),
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                f"Ollama did not respond within {OLLAMA_REQUEST_TIMEOUT}s. "
                f"The model may still be loading. Error: {exc}"
            ),
        ) from exc

    # ── Timer stops the exact millisecond the full response arrives ──────────
    t_end: float = time.perf_counter()
    latency_ms: float = (t_end - t_start) * 1_000.0

    if ollama_response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Ollama returned HTTP {ollama_response.status_code}. "
                f"Body: {ollama_response.text[:512]}"
            ),
        )

    # Parse Ollama JSON response — the "response" key holds generated text.
    try:
        ollama_data: dict = ollama_response.json()
        raw_response: str = ollama_data.get("response", "").strip()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to parse Ollama JSON response: {exc}",
        ) from exc

    # ── Phase 2: Deterministic schema validation ─────────────────────────────
    # Validate the raw Ollama output against MathResponse.
    #   schema_passed = True  → structural conformance confirmed
    #   schema_passed = False → structural drift detected (hallucination proxy)
    # The flawed raw_response is always retained in full for audit purposes.
    schema_passed: Optional[bool]
    try:
        MathResponse.model_validate_json(raw_response)
        schema_passed = True
    except ValidationError:
        schema_passed = False

    # ── Async fire-and-forget DB write (does NOT block the response) ─────────
    trace_record = TraceRecord(
        trace_id=trace_id,
        timestamp=timestamp,
        model=payload.model,
        system_prompt=payload.system_prompt,
        user_prompt=payload.user_prompt,
        raw_response=raw_response,
        latency_ms=round(latency_ms, 4),
        schema_passed=schema_passed,
    )
    asyncio.create_task(_persist_trace(trace_record))

    # ── Build and return structured response to the caller ───────────────────
    response_body = GenerateResponse(
        trace_id=trace_id,
        model=payload.model,
        raw_response=raw_response,
        latency_ms=round(latency_ms, 4),
        schema_passed=schema_passed,
        timestamp=timestamp,
    )

    return JSONResponse(
        content=response_body.model_dump(),
        status_code=status.HTTP_200_OK,
    )


# ---------------------------------------------------------------------------
# Trace Query Route  (diagnostic / offline evaluation)
# ---------------------------------------------------------------------------


@app.get(
    "/traces",
    tags=["observability"],
    summary="Retrieve the N most recent inference traces from SQLite",
)
async def list_traces(limit: int = 20) -> JSONResponse:
    """
    Returns the ``limit`` most recent rows from inference_traces,
    ordered by timestamp descending.  Used for offline evaluation.
    """
    global _db
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised.")

    limit = max(1, min(limit, 500))  # Clamp to a sane range.

    async with _db.execute(
        """
        SELECT trace_id, timestamp, model, system_prompt, user_prompt,
               raw_response, latency_ms, schema_passed
        FROM   inference_traces
        ORDER  BY timestamp DESC
        LIMIT  ?
        """,
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()

    columns = [
        "trace_id", "timestamp", "model", "system_prompt",
        "user_prompt", "raw_response", "latency_ms", "schema_passed",
    ]
    traces = [dict(zip(columns, row)) for row in rows]

    return JSONResponse(content={"count": len(traces), "traces": traces})


# ---------------------------------------------------------------------------
# Analytics Route  (aggregate performance metrics)
# ---------------------------------------------------------------------------


@app.get(
    "/analytics",
    tags=["observability"],
    summary="Aggregate performance metrics from the inference_traces log",
)
async def get_analytics() -> JSONResponse:
    """
    Returns aggregate metrics computed directly from the ``inference_traces``
    SQLite table.  All three aggregates are resolved in a single SQL query
    to minimise DB round-trips.

    Metrics
    -------
    total_inferences : int
        Count of all logged requests.
    average_latency_ms : float | None
        Arithmetic mean of all ``latency_ms`` values.
        ``null`` when the table is empty.
    schema_pass_rate : float | None
        Percentage of requests where ``schema_passed = 1`` (True), computed
        as ``(passed / evaluated) * 100``.  Rows with ``schema_passed IS NULL``
        are excluded from the denominator.  ``null`` when no evaluated rows exist.
    schema_pass_count : int
        Absolute number of traces that passed schema validation.
    schema_fail_count : int
        Absolute number of traces that failed schema validation.
    schema_null_count : int
        Traces where schema validation was not run (schema_passed IS NULL).
    """
    global _db
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised.")

    async with _db.execute(
        """
        SELECT
            COUNT(*)                                        AS total_inferences,
            AVG(latency_ms)                                 AS average_latency_ms,
            SUM(CASE WHEN schema_passed = 1 THEN 1 ELSE 0 END) AS pass_count,
            SUM(CASE WHEN schema_passed = 0 THEN 1 ELSE 0 END) AS fail_count,
            SUM(CASE WHEN schema_passed IS NULL THEN 1 ELSE 0 END) AS null_count
        FROM inference_traces
        """
    ) as cursor:
        row = await cursor.fetchone()

    # Unpack — row is always returned (COUNT never returns NULL even on empty table)
    total: int           = row[0] or 0
    avg_latency: float | None = row[1]          # None if table is empty
    pass_count: int      = row[2] or 0
    fail_count: int      = row[3] or 0
    null_count: int      = row[4] or 0

    # Evaluated = rows where schema validation was actually run
    evaluated: int = pass_count + fail_count
    schema_pass_rate: float | None = (
        round((pass_count / evaluated) * 100.0, 2) if evaluated > 0 else None
    )

    payload: dict = {
        "total_inferences": total,
        "average_latency_ms": round(avg_latency, 4) if avg_latency is not None else None,
        "schema_pass_rate": schema_pass_rate,   # percentage, e.g. 87.5
        "schema_pass_count": pass_count,
        "schema_fail_count": fail_count,
        "schema_null_count": null_count,         # not-yet-evaluated (non-math routes)
    }

    return JSONResponse(content=payload)


# ---------------------------------------------------------------------------
# Entry-point (development server)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "proxy:app",
        host="0.0.0.0",
        port=8000,
        reload=False,       # Disable reload in production; enable manually for dev.
        log_level="info",
        access_log=True,
    )
