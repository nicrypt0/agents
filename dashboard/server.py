import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

OPENCLAW_DIR = Path("/Users/nicrypto/.openclaw")
WORKSPACE_DIR = OPENCLAW_DIR / "workspace"

CRON_JOBS_FILE = OPENCLAW_DIR / "cron" / "jobs.json"

GUYBOT_DIR = WORKSPACE_DIR / "agents" / "guybot"
GUYBOT_PID_FILE = GUYBOT_DIR / "guybot.pid"
GUYBOT_LOG_FILE = GUYBOT_DIR / "guybot.log"
GUYBOT_STATE_FILE = GUYBOT_DIR / "state.json"

MARKET_CONTEXT_DIR = WORKSPACE_DIR / "market_context"
CHROMA_STORE_DIR = MARKET_CONTEXT_DIR / "chroma_store"

CONTEXT_FILES = {
    "current.md": MARKET_CONTEXT_DIR / "current.md",
    "weekly_context.md": MARKET_CONTEXT_DIR / "weekly_context.md",
    "latest_digest.txt": WORKSPACE_DIR / "latest_digest.txt",
}

# Agents that are cron-driven (no persistent process)
CRON_AGENTS = [
    "x_digest",
    "market_context",
    "tweet_intel",
    "coinbureau_performance",
    "coinbureau_telegram_performance",
    "news_poller",
    "humor_digest",
]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="OpenClaw Agent Status Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ts() -> float:
    return time.time()


def _iso(ms: Optional[int]) -> Optional[str]:
    """Convert epoch milliseconds to ISO 8601 string, or None."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _age_minutes(mtime: float) -> float:
    return (_now_ts() - mtime) / 60


def _age_label(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h ago"
    days = hours / 24
    return f"{days:.1f}d ago"


def _file_info(path: Path) -> dict:
    """Return mtime ISO string and age label, or nulls if file missing."""
    try:
        mtime = path.stat().st_mtime
        minutes = _age_minutes(mtime)
        return {
            "last_modified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            "age_minutes": round(minutes, 1),
            "age_label": _age_label(minutes),
            "exists": True,
        }
    except (FileNotFoundError, PermissionError):
        return {
            "last_modified": None,
            "age_minutes": None,
            "age_label": None,
            "exists": False,
        }


def _read_json(path: Path) -> Any:
    """Read and parse a JSON file; return None on any error."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _read_log_tail(path: Path, lines: int = 10) -> Optional[list[str]]:
    """Return last N lines of a text file, or None if unreadable."""
    try:
        content = path.read_text(errors="replace")
        return content.splitlines()[-lines:]
    except Exception:
        return None


def _pid_alive(pid_file: Path) -> tuple[Optional[int], bool]:
    """Read PID file and check if the process is alive. Returns (pid, alive)."""
    try:
        pid = int(pid_file.read_text().strip())
    except Exception:
        return None, False
    try:
        os.kill(pid, 0)
        return pid, True
    except (ProcessLookupError, PermissionError):
        return pid, False


# ---------------------------------------------------------------------------
# Endpoint: /status
# ---------------------------------------------------------------------------


@app.get("/status")
def get_status() -> dict:
    """Overall health summary."""
    _, guybot_alive = _pid_alive(GUYBOT_PID_FILE)

    cron_data = _read_json(CRON_JOBS_FILE)
    cron_jobs: list = cron_data.get("jobs", []) if cron_data else []
    enabled_jobs = [j for j in cron_jobs if j.get("enabled", False)]
    last_statuses = [
        j.get("state", {}).get("lastRunStatus") for j in enabled_jobs
    ]
    cron_ok = all(s == "success" for s in last_statuses if s is not None)

    chroma_ok = CHROMA_STORE_DIR.exists()
    context_ok = all(p.exists() for p in CONTEXT_FILES.values())

    return {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "guybot_running": guybot_alive,
        "cron_jobs_healthy": cron_ok,
        "chroma_available": chroma_ok,
        "context_files_present": context_ok,
        "enabled_cron_jobs": len(enabled_jobs),
        "total_cron_jobs": len(cron_jobs),
    }


# ---------------------------------------------------------------------------
# Endpoint: /crons
# ---------------------------------------------------------------------------


@app.get("/crons")
def get_crons() -> dict:
    """All OpenClaw cron jobs with schedule, last run, last status, next run."""
    cron_data = _read_json(CRON_JOBS_FILE)
    if not cron_data:
        return {"jobs": [], "source": str(CRON_JOBS_FILE), "error": "File not found or unreadable"}

    jobs_out = []
    for job in cron_data.get("jobs", []):
        state = job.get("state", {})
        last_run_ms = state.get("lastRunAtMs")
        next_run_ms = state.get("nextRunAtMs")

        last_run_age: Optional[str] = None
        if last_run_ms:
            minutes = _age_minutes(last_run_ms / 1000)
            last_run_age = _age_label(minutes)

        jobs_out.append(
            {
                "id": job.get("id"),
                "name": job.get("name"),
                "enabled": job.get("enabled", False),
                "schedule_expr": job.get("schedule", {}).get("expr"),
                "timezone": job.get("schedule", {}).get("tz"),
                "last_run_at": _iso(last_run_ms),
                "last_run_age": last_run_age,
                "last_run_status": state.get("lastRunStatus"),
                "last_duration_ms": state.get("lastDurationMs"),
                "next_run_at": _iso(next_run_ms),
            }
        )

    return {"jobs": jobs_out, "count": len(jobs_out)}


# ---------------------------------------------------------------------------
# Endpoint: /agents
# ---------------------------------------------------------------------------


@app.get("/agents")
def get_agents() -> dict:
    """All agents with running status, last output file age, log tail."""
    agents_out = []

    # --- GuyBot (long-running process) ---
    pid, alive = _pid_alive(GUYBOT_PID_FILE)
    log_tail = _read_log_tail(GUYBOT_LOG_FILE, lines=10)
    state = _read_json(GUYBOT_STATE_FILE)

    pid_file_info = _file_info(GUYBOT_PID_FILE)
    log_file_info = _file_info(GUYBOT_LOG_FILE)

    agents_out.append(
        {
            "name": "guybot",
            "type": "long-running",
            "running": alive,
            "pid": pid,
            "pid_file": {
                "path": str(GUYBOT_PID_FILE),
                **pid_file_info,
            },
            "log_file": {
                "path": str(GUYBOT_LOG_FILE),
                **log_file_info,
                "tail": log_tail,
            },
            "state": state,
        }
    )

    # --- Cron-based agents ---
    cron_data = _read_json(CRON_JOBS_FILE)
    cron_map: dict[str, dict] = {}
    if cron_data:
        for job in cron_data.get("jobs", []):
            name = job.get("name", "")
            # Match by lowercased name fragment
            for agent_name in CRON_AGENTS:
                if agent_name.lower() in name.lower():
                    cron_map[agent_name] = job
                    break

    for agent_name in CRON_AGENTS:
        agent_dir = WORKSPACE_DIR / "agents" / agent_name
        job = cron_map.get(agent_name)

        # Try to find output files (any .json, .txt, .log in agent dir)
        output_files: list[dict] = []
        if agent_dir.exists():
            for ext in ("*.json", "*.txt", "*.log", "*.md"):
                for f in sorted(agent_dir.glob(ext)):
                    output_files.append({"name": f.name, **_file_info(f)})

        state_info = None
        state_file = agent_dir / "state.json"
        if state_file.exists():
            state_info = _read_json(state_file)

        log_file = agent_dir / f"{agent_name}.log"
        log_tail_out = _read_log_tail(log_file) if log_file.exists() else None

        cron_state = job.get("state", {}) if job else {}
        last_run_ms = cron_state.get("lastRunAtMs")
        last_run_age: Optional[str] = None
        if last_run_ms:
            last_run_age = _age_label(_age_minutes(last_run_ms / 1000))

        agents_out.append(
            {
                "name": agent_name,
                "type": "cron",
                "running": False,  # cron agents don't persist
                "cron": {
                    "enabled": job.get("enabled", False) if job else None,
                    "schedule_expr": job.get("schedule", {}).get("expr") if job else None,
                    "last_run_at": _iso(last_run_ms),
                    "last_run_age": last_run_age,
                    "last_run_status": cron_state.get("lastRunStatus"),
                    "next_run_at": _iso(cron_state.get("nextRunAtMs")),
                }
                if job
                else None,
                "output_files": output_files,
                "state": state_info,
                "log_tail": log_tail_out,
            }
        )

    return {"agents": agents_out, "count": len(agents_out)}


# ---------------------------------------------------------------------------
# Endpoint: /chroma
# ---------------------------------------------------------------------------


@app.get("/chroma")
def get_chroma() -> dict:
    """ChromaDB collection sizes for tweets, articles, my_tweets."""
    base: dict[str, Any] = {
        "store_path": str(CHROMA_STORE_DIR),
        "store_exists": CHROMA_STORE_DIR.exists(),
        "collections": {},
        "error": None,
    }

    if not CHROMA_STORE_DIR.exists():
        base["error"] = "Chroma store directory not found"
        return base

    try:
        import chromadb  # type: ignore

        client = chromadb.PersistentClient(path=str(CHROMA_STORE_DIR))

        target_collections = ["tweets", "articles", "my_tweets"]
        existing = {c.name for c in client.list_collections()}

        collections_out = {}
        for name in target_collections:
            if name in existing:
                col = client.get_collection(name)
                collections_out[name] = {
                    "exists": True,
                    "count": col.count(),
                }
            else:
                collections_out[name] = {"exists": False, "count": 0}

        # Also surface any other collections present
        for col_name in existing:
            if col_name not in target_collections:
                col = client.get_collection(col_name)
                collections_out[col_name] = {
                    "exists": True,
                    "count": col.count(),
                    "extra": True,
                }

        base["collections"] = collections_out

    except ImportError:
        base["error"] = "chromadb package not installed"
    except Exception as exc:
        base["error"] = str(exc)

    return base


# ---------------------------------------------------------------------------
# Endpoint: /context
# ---------------------------------------------------------------------------


@app.get("/context")
def get_context() -> dict:
    """Market context file freshness."""
    files_out = {}
    for label, path in CONTEXT_FILES.items():
        files_out[label] = {
            "path": str(path),
            **_file_info(path),
        }

    # Overall freshness: all files present and none older than 24 hours
    all_present = all(v["exists"] for v in files_out.values())
    all_fresh = all(
        v["age_minutes"] is not None and v["age_minutes"] < 1440
        for v in files_out.values()
    )

    return {
        "files": files_out,
        "all_present": all_present,
        "all_fresh_24h": all_fresh,
        "market_context_dir": str(MARKET_CONTEXT_DIR),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
