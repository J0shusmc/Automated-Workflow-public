"""Small local web service for the Automated Workflow editor.

This intentionally uses only the standard library so a new checkout can run
with its existing virtual environment.  The API and persistence layer are
kept separate from the browser UI to make a later FastAPI/worker upgrade easy.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import mimetypes
import os
import pty
import queue
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "web"
DATA_DIR = PROJECT_ROOT / "data"
WORKFLOW_DIR = DATA_DIR / "workflows"
ARTIFACT_DIR = DATA_DIR / "artifacts"
DATA_FILE_DIR = DATA_DIR / "files"
WORKFLOW_FILE_NAME = "workflow.json"
WORKFLOW_STORAGE_VERSION = 2
GENERATED_PROJECTS_DIR = PROJECT_ROOT / "generated-projects"
DEFAULT_PROJECTS_DIR = PROJECT_ROOT.parent
DEFAULT_IMAGE_GENERATION_SCRIPT = DEFAULT_PROJECTS_DIR / "Image Gen" / "Image-Generation.py"
DEFAULT_PRODUCT_ROOT = Path(os.environ.get("AUTOMATED_WORKFLOW_PRODUCT_ROOT") or PROJECT_ROOT / "data" / "products")
DEFAULT_IMAGE_BACKEND_PORT = 8090
LLM_SERVICE_NAME = os.environ.get("AUTOMATED_WORKFLOW_LLM_SERVICE", "llama-server")
LLM_KILL_PATTERN = os.environ.get("AUTOMATED_WORKFLOW_LLM_KILL_PATTERN", "llama-server")
DEFAULT_NODE_TIMEOUT_SECONDS = int(os.environ.get("AUTOMATED_WORKFLOW_NODE_TIMEOUT_SECONDS", "0") or "0")
RUNS: dict[str, dict] = {}
WORKFLOW_SERVICES: dict[str, dict] = {}
WORKFLOW_SERVICE_EVENTS: list[dict] = []
WORKFLOW_SERVICE_LOCK = threading.Lock()
WORKFLOW_STORAGE_LOCK = threading.RLock()
SCHEDULER_STARTED = False
TERMINAL_SESSIONS: dict[str, dict] = {}
LLM_ACTIVITY_EVENTS: list[dict] = []
LLM_ACTIVITY_CONDITION = threading.Condition()
LLM_ACTIVITY_NEXT_ID = 1
DEFAULT_RECURRING_SECONDS = 300
MIN_RECURRING_SECONDS = 5
MAX_RECURRING_SECONDS = 86400
DAILY_RECURRING_GRACE_SECONDS = 120
DEFAULT_CLAUDIA_SYSTEM_PROMPT = """You are Claudia, the local workflow agent for Automated Workflow.
You receive upstream workflow input and turn it into clear, structured work orders for downstream automation nodes.
Preserve the user's intent. Be specific, practical, and concise."""

WORKFLOW_AGENT_GUARDRAILS = """Workflow agent operating rules:
- Treat the upstream input as the source of truth.
- Preserve the subject, action, setting, mood, and constraints from the upstream input.
- Do not introduce new subjects, locations, styles, themes, facts, or story elements unless the upstream input explicitly asks for them.
- If improving a prompt, make the smallest useful improvement: clarify, tighten, and add only directly relevant quality details.
- Keep the output concise and ready for the next workflow node.
- Return only the downstream value. Do not explain your changes."""


class WorkflowCancelled(Exception):
    pass

AGENT_BUILDER_SYSTEM_PROMPT = """You are Claudia, an expert agent architect inside Automated Workflow.
The user will provide a rough description of what a new specialist agent should do.
Convert that brief into a strong, reusable system prompt for the specialist agent.

Requirements:
- Preserve the user's requested agent purpose exactly.
- Design conservative agents that stay tightly anchored to upstream workflow input.
- The generated agent must not invent new subjects, settings, styles, facts, themes, or requirements.
- The generated agent may clarify, compress, format, or add directly relevant execution details only when they serve the user's stated purpose.
- Define the agent's role, responsibilities, input expectations, output expectations, quality bar, and constraints.
- Include a strict instruction to return only the value that should pass to the next node.
- Do not include markdown fences.
- Return JSON only with keys "name" and "systemPrompt".
- The name must be short and UI-friendly.
- The systemPrompt must be directly usable as the agent's system prompt."""

SCRIPT_BUILDER_SYSTEM_PROMPT = """You are Claudia, a conservative Python project builder inside Automated Workflow.
The user will describe a script or small automation project.

Return JSON only with these keys:
- name: short project name.
- files: array of objects with path and content. Include main.py unless another entry point is clearly better.
- requirements: array of pip package names, empty when the standard library is enough.
- runCommand: concise command to run the project from its folder.
- notes: short setup or safety notes.

Rules:
- Keep the project small, practical, and runnable.
- Do not ask questions unless the request cannot be implemented safely without credentials or missing external details.
- Use placeholders for secrets and explain them in notes.
- Do not include markdown fences."""

SCRIPT_EDITOR_SYSTEM_PROMPT = """You are Claudia, a conservative Python code editor inside Automated Workflow.
The user will provide an existing Python script and a requested change.

Return JSON only with these keys:
- code: the complete updated Python file content.
- notes: a short summary of what changed.

Rules:
- Preserve existing behavior unless the user specifically asks to change it.
- Keep imports, configuration loading, and command-line behavior compatible with the existing script.
- Do not omit unchanged parts of the file.
- Do not include markdown fences."""


def project_path(value: str) -> Path:
    """Resolve workflow paths; shared workflows store them relative to the project."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_env_file(path: Path = PROJECT_ROOT / ".env") -> None:
    """Load KEY=value pairs from the project .env without overriding the environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def ensure_storage() -> None:
    WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_FILE_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


def validate_workflow_id(workflow_id: str) -> str:
    if not workflow_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in workflow_id):
        raise ValueError("Invalid workflow id")
    return workflow_id


def slugify(text: str, fallback: str = "codex-script") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or fallback)[:52].strip("-") or fallback


def workflow_folder_name(workflow: dict) -> str:
    workflow_id = validate_workflow_id(str(workflow.get("id") or ""))
    short_id = workflow_id[:8]
    name = slugify(str(workflow.get("name") or "untitled-workflow"), "untitled-workflow")
    suffix = f"-{short_id}"
    return f"{name[: max(1, 64 - len(suffix))].strip('-')}{suffix}"


def iter_workflow_files() -> list[Path]:
    candidates = [
        *(path for path in WORKFLOW_DIR.glob(f"*/{WORKFLOW_FILE_NAME}") if path.is_file()),
        *(path for path in WORKFLOW_DIR.glob("*.json") if path.is_file()),
    ]

    def freshness(path: Path) -> tuple[int, float, int]:
        try:
            workflow = json.loads(path.read_text(encoding="utf-8"))
            updated_at = int(workflow.get("updatedAt") or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            updated_at = 0
        # A canonical file wins only when timestamps tie. A newer legacy stop
        # must never lose merely because it uses the old storage layout.
        return updated_at, path.stat().st_mtime, int(path.name == WORKFLOW_FILE_NAME)

    return sorted(candidates, key=freshness, reverse=True)


def workflow_id_from_file(path: Path, workflow: dict | None = None) -> str:
    if workflow and workflow.get("id"):
        return str(workflow["id"])
    return path.parent.name if path.name == WORKFLOW_FILE_NAME else path.stem


def workflow_path(workflow_id: str) -> Path:
    workflow_id = validate_workflow_id(workflow_id)
    for path in iter_workflow_files():
        try:
            workflow = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if workflow_id_from_file(path, workflow) == workflow_id:
            return path
    legacy_path = WORKFLOW_DIR / f"{workflow_id}.json"
    if legacy_path.exists():
        return legacy_path
    raise FileNotFoundError(workflow_id)


def canonical_workflow_path(workflow: dict) -> Path:
    workflow_id = validate_workflow_id(str(workflow.get("id") or ""))
    for existing_path in iter_workflow_files():
        if existing_path.name != WORKFLOW_FILE_NAME:
            continue
        try:
            existing_workflow = json.loads(existing_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if workflow_id_from_file(existing_path, existing_workflow) == workflow_id:
            return existing_path
    directory = WORKFLOW_DIR / workflow_folder_name(workflow)
    return directory / WORKFLOW_FILE_NAME


def atomic_write_workflow(path: Path, workflow: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".workflow-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(workflow, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def archive_duplicate_workflow_files(workflow_id: str, canonical_path: Path) -> None:
    backup_dir = WORKFLOW_DIR / ".legacy-backups"
    for candidate in iter_workflow_files():
        if candidate.resolve() == canonical_path.resolve():
            continue
        try:
            candidate_workflow = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if workflow_id_from_file(candidate, candidate_workflow) != workflow_id:
            continue
        backup_dir.mkdir(parents=True, exist_ok=True)
        source_label = candidate.parent.name if candidate.name == WORKFLOW_FILE_NAME else candidate.stem
        backup_path = backup_dir / f"{source_label}-{time.time_ns()}.json"
        shutil.move(str(candidate), backup_path)


def workflow_runtime_dir(workflow: dict, name: str) -> Path:
    path = canonical_workflow_path(workflow).parent / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_workflow(workflow_id: str) -> dict:
    path = workflow_path(workflow_id)
    if not path.exists():
        raise FileNotFoundError(workflow_id)
    return json.loads(path.read_text(encoding="utf-8"))


def saved_artifact_path(value: dict) -> Path | None:
    path_text = value.get("path")
    if not path_text and isinstance(value.get("url"), str):
        parsed = urlparse(value["url"])
        if parsed.path == "/api/artifacts":
            path_values = parse_qs(parsed.query).get("path", [])
            path_text = unquote(path_values[0]) if path_values else ""
    if not path_text:
        return None
    path = Path(str(path_text)).expanduser().resolve()
    return path if path.is_file() else None


def persist_workflow_artifacts(value, workflow_id: str):
    if isinstance(value, list):
        return [persist_workflow_artifacts(item, workflow_id) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") in {"image", "video", "file"}:
        source = saved_artifact_path(value)
        if source:
            try:
                source.relative_to(DATA_DIR.resolve())
                if value.get("type") == "file":
                    return {key: persist_workflow_artifacts(item, workflow_id) for key, item in value.items()}
            except ValueError:
                pass
            try:
                workflow = read_workflow(workflow_id)
                target_dir = workflow_runtime_dir(workflow, "artifacts")
            except (FileNotFoundError, ValueError):
                target_dir = ARTIFACT_DIR / workflow_id
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            if target.exists() and source.resolve() != target.resolve():
                target = target_dir / f"{source.stem}-{uuid.uuid4().hex[:8]}{source.suffix}"
            if source.resolve() != target.resolve():
                shutil.copy2(source, target)
            value = {**value, "path": str(target), "url": artifact_url(target), "name": value.get("name") or target.name}
    return {key: persist_workflow_artifacts(item, workflow_id) for key, item in value.items()}


def save_workflow(workflow: dict, *, recurring_authoritative: bool = False) -> dict:
    with WORKFLOW_STORAGE_LOCK:
        workflow_id = workflow.get("id") or uuid.uuid4().hex
        workflow["id"] = workflow_id
        existing_workflow = None
        try:
            existing_workflow = read_workflow(workflow_id)
        except FileNotFoundError:
            pass

        # Recurring state is runtime control state. Ordinary dashboard autosaves
        # may carry an old copy, so only the dedicated recurring endpoints are
        # allowed to change it.
        if existing_workflow is not None and not recurring_authoritative:
            existing_recurring = existing_workflow.get("recurring")
            if isinstance(existing_recurring, dict):
                workflow["recurring"] = dict(existing_recurring)

        workflow["updatedAt"] = int(time.time())
        workflow.setdefault("name", "Untitled workflow")
        workflow.setdefault("nodes", [])
        workflow.setdefault("edges", [])
        if "activeOrder" in workflow:
            try:
                workflow["activeOrder"] = int(workflow["activeOrder"])
            except (TypeError, ValueError):
                workflow.pop("activeOrder", None)
        recurring = workflow.get("recurring") if isinstance(workflow.get("recurring"), dict) else {}
        schedule_mode = recurring.get("scheduleMode") if recurring.get("scheduleMode") in {"interval", "dailyTime"} else "interval"
        time_of_day = normalize_time_of_day(recurring.get("timeOfDay"))
        weekdays = normalize_weekdays(recurring.get("weekdays"))
        workflow["recurring"] = {
            **recurring,
            "enabled": bool(recurring.get("enabled", False)),
            "scheduleMode": schedule_mode,
            "frequencySeconds": clamp_recurring_seconds(recurring.get("frequencySeconds", DEFAULT_RECURRING_SECONDS)),
            "timeOfDay": time_of_day,
            "weekdays": weekdays,
            "displayMode": recurring.get("displayMode") if recurring.get("displayMode") in {"verbose", "silent"} else "verbose",
        }
        if workflow["recurring"]["enabled"]:
            workflow["recurring"]["nextRunAt"] = effective_next_recurring_run_at(workflow["recurring"])
        else:
            workflow["recurring"]["nextRunAt"] = None
        workflow["storage"] = {
            **(workflow.get("storage") if isinstance(workflow.get("storage"), dict) else {}),
            "version": WORKFLOW_STORAGE_VERSION,
        }
        target_path = canonical_workflow_path(workflow)
        workflow["storage"]["root"] = str(target_path.parent.relative_to(PROJECT_ROOT))
        for folder in ("inputs", "scripts", "artifacts", "runs"):
            (target_path.parent / folder).mkdir(parents=True, exist_ok=True)
        workflow = persist_workflow_artifacts(workflow, workflow_id)
        atomic_write_workflow(target_path, workflow)
        archive_duplicate_workflow_files(workflow_id, target_path)
        sync_workflow_service_state(workflow)
        return workflow


def delete_workflow(workflow_id: str) -> None:
    path = workflow_path(workflow_id)
    if not path.exists():
        raise FileNotFoundError(workflow_id)
    with WORKFLOW_SERVICE_LOCK:
        WORKFLOW_SERVICES.pop(workflow_id, None)
    for candidate in iter_workflow_files():
        try:
            workflow = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if workflow_id_from_file(candidate, workflow) != workflow_id:
            continue
        if candidate.name == WORKFLOW_FILE_NAME:
            shutil.rmtree(candidate.parent)
        else:
            candidate.unlink()


def list_workflows() -> list[dict]:
    result = []
    seen = set()
    for path in iter_workflow_files():
        try:
            workflow = json.loads(path.read_text(encoding="utf-8"))
            workflow_id = workflow_id_from_file(path, workflow)
            if workflow_id in seen:
                continue
            seen.add(workflow_id)
            workflow["id"] = workflow_id
            service = WORKFLOW_SERVICES.get(workflow_id, {"status": "stopped"})
            recurring = workflow.get("recurring") if isinstance(workflow.get("recurring"), dict) else {}
            next_run_at = effective_next_recurring_run_at(recurring, service) if recurring.get("enabled") else None
            result.append({
                "id": workflow_id,
                "name": workflow.get("name", "Untitled workflow"),
                "updatedAt": workflow.get("updatedAt", 0),
                "nodeCount": len(workflow.get("nodes", [])),
                "active": workflow.get("active", False),
                "activeOrder": workflow.get("activeOrder"),
                "favorite": bool(workflow.get("favorite", False)),
                "recurring": {
                    "enabled": bool(recurring.get("enabled", False)),
                    "scheduleMode": recurring.get("scheduleMode", "interval"),
                    "frequencySeconds": clamp_recurring_seconds(recurring.get("frequencySeconds", DEFAULT_RECURRING_SECONDS)),
                    "timeOfDay": normalize_time_of_day(recurring.get("timeOfDay")),
                    "weekdays": normalize_weekdays(recurring.get("weekdays")),
                    "displayMode": recurring.get("displayMode") if recurring.get("displayMode") in {"verbose", "silent"} else "verbose",
                    "nextRunAt": next_run_at,
                    "lastRunAt": service.get("lastRunAt") or recurring.get("lastRunAt"),
                    "lastStatus": service.get("lastStatus") or recurring.get("lastStatus", ""),
                    "lastError": service.get("lastError") or recurring.get("lastError", ""),
                },
                "serviceStatus": service.get("status", "stopped"),
                "serviceStartedAt": service.get("startedAt"),
            })
        except (json.JSONDecodeError, KeyError):
            continue
    return result


def update_active_workflow_order(workflow_ids: list[str]) -> dict:
    if not isinstance(workflow_ids, list):
        raise ValueError("workflowIds must be a list.")

    ordered_ids = []
    seen = set()
    for workflow_id in workflow_ids:
        workflow_id = str(workflow_id)
        if workflow_id and workflow_id not in seen:
            ordered_ids.append(workflow_id)
            seen.add(workflow_id)

    workflows = {}
    for path in iter_workflow_files():
        try:
            workflow = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        workflow_id = workflow_id_from_file(path, workflow)
        workflows.setdefault(workflow_id, path)
    for index, workflow_id in enumerate(ordered_ids):
        path = workflows.get(workflow_id)
        if not path:
            continue
        workflow = json.loads(path.read_text(encoding="utf-8"))
        workflow["id"] = workflow_id
        workflow["active"] = True
        workflow["activeOrder"] = index
        save_workflow(workflow)

    return {"workflowIds": ordered_ids}


def project_details(script_name: str) -> dict:
    """Return the task configuration for a selected local Python script."""
    script_path = Path(script_name).expanduser().resolve()
    if not script_path.is_file() or script_path.suffix != ".py":
        raise ValueError("Please select a Python (.py) file.")
    working_directory = script_path.parent
    for directory in (script_path.parent, *script_path.parents):
        if (directory / ".venv").is_dir() or (directory / "venv").is_dir() or (directory / "pyproject.toml").is_file():
            working_directory = directory
            break
    python_executable = None
    for environment in (working_directory / ".venv", working_directory / "venv"):
        candidate = environment / "bin" / "python"
        if candidate.is_file():
            python_executable = str(candidate)
            break
    return {
        "scriptPath": str(script_path),
        "workingDirectory": str(working_directory),
        "pythonExecutable": python_executable or sys.executable,
        "usesDetectedVenv": python_executable is not None,
        "code": script_path.read_text(encoding="utf-8"),
    }


def choose_task_script() -> dict:
    """Open the desktop file chooser on this local machine via Zenity."""
    initial_directory = DEFAULT_PROJECTS_DIR if DEFAULT_PROJECTS_DIR.is_dir() else PROJECT_ROOT
    selection = subprocess.run(
        [
            "zenity", "--file-selection", "--title=Select a Python task script",
            f"--filename={initial_directory}/", "--file-filter=Python scripts | *.py",
        ],
        text=True, capture_output=True,
    )
    if selection.returncode == 1:
        return {"cancelled": True}
    if selection.returncode:
        message = selection.stderr.strip() or "The native file picker could not be opened."
        raise RuntimeError(message)
    return project_details(selection.stdout.strip())


def image_generation_task() -> dict:
    details = project_details(str(DEFAULT_IMAGE_GENERATION_SCRIPT))
    details["displayName"] = "Image Generation"
    details["taskKind"] = "image-generation"
    return details


def topological_nodes(workflow: dict) -> list[dict]:
    nodes = {node["id"]: node for node in workflow.get("nodes", [])}
    incoming = {node_id: 0 for node_id in nodes}
    children = {node_id: [] for node_id in nodes}
    for edge in workflow.get("edges", []):
        source, target = edge.get("source"), edge.get("target")
        if source in nodes and target in nodes:
            incoming[target] += 1
            children[source].append(target)
    def position(node_id: str) -> tuple[float, float]:
        node = nodes[node_id]
        return (float(node.get("x", 0)), float(node.get("y", 0)))

    ready = sorted((node_id for node_id, count in incoming.items() if count == 0), key=position)
    ordered = []
    while ready:
        node_id = ready.pop(0)
        ordered.append(nodes[node_id])
        for child in children[node_id]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
        ready.sort(key=position)
    if len(ordered) != len(nodes):
        raise ValueError("Workflow contains a cycle")
    return ordered


def incoming_edges_by_target(workflow: dict) -> dict[str, list[dict]]:
    node_ids = {node["id"] for node in workflow.get("nodes", [])}
    incoming = {node_id: [] for node_id in node_ids}
    for edge in workflow.get("edges", []):
        source, target = edge.get("source"), edge.get("target")
        if source in node_ids and target in node_ids:
            incoming[target].append(edge)
    return incoming


def node_accepts_standalone_run(node: dict) -> bool:
    node_type = node.get("type")
    if node_type in {"agent", "output", "breakpoint"}:
        return not (node.get("inputs") or [])
    return True


def list_products(root: Path = DEFAULT_PRODUCT_ROOT) -> dict:
    products = []
    if root.exists():
        for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if child.is_dir():
                products.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "fileCount": sum(1 for item in child.rglob("*") if item.is_file()),
                    }
                )
    return {"root": str(root), "products": products}


def upload_product_file(payload: dict, root: Path = DEFAULT_PRODUCT_ROOT) -> dict:
    product_name = str(payload.get("product") or "").strip()
    file_name = Path(str(payload.get("fileName") or "")).name
    content_base64 = str(payload.get("contentBase64") or "")
    if not product_name:
        raise ValueError("Product name is required.")
    if not file_name:
        raise ValueError("File name is required.")
    if not content_base64:
        raise ValueError("File content is required.")

    product_dir = (root / product_name).resolve()
    root_resolved = root.resolve()
    if root_resolved != product_dir and root_resolved not in product_dir.parents:
        raise ValueError("Product folder is outside the allowed product root.")
    product_dir.mkdir(parents=True, exist_ok=True)

    target = (product_dir / file_name).resolve()
    if product_dir != target.parent:
        raise ValueError("File name is not allowed.")
    target.write_bytes(base64.b64decode(content_base64))
    return {
        "uploaded": True,
        "product": product_name,
        "fileName": file_name,
        "path": str(target),
        "products": list_products(root)["products"],
    }


def data_file_allowed(path: Path) -> bool:
    if path.name == WORKFLOW_FILE_NAME:
        return False
    if path.suffix.lower() not in {".csv", ".json", ".jsonl", ".txt", ".md"}:
        return False
    try:
        relative = path.resolve().relative_to(DATA_DIR.resolve())
    except ValueError:
        return False
    parts = set(relative.parts)
    if {"artifacts", "runs", "workflows"} & parts:
        return False
    return True


def data_file_payload(path: Path, root_label: str = "data") -> dict:
    stat = path.stat()
    relative_path = str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "relativePath": relative_path,
        "root": root_label,
        "size": stat.st_size,
        "modifiedAt": int(stat.st_mtime),
        "extension": path.suffix.lower(),
    }


def list_data_files(workflow_id: str | None = None) -> dict:
    roots: list[tuple[Path, str]] = [
        (DATA_FILE_DIR, "data files"),
    ]

    files = []
    seen = set()
    for root, label in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or not data_file_allowed(path):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(data_file_payload(path, label))
    files.sort(key=lambda item: (0 if item["root"] == "data files" else 1, item["name"].lower()))
    return {"root": str(DATA_DIR), "workflowId": workflow_id, "files": files}


def upload_data_file(payload: dict) -> dict:
    file_name = Path(str(payload.get("fileName") or "")).name
    content_base64 = str(payload.get("contentBase64") or "")
    if not file_name:
        raise ValueError("File name is required.")
    if not content_base64:
        raise ValueError("File content is required.")

    target_dir = DATA_FILE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = (target_dir / file_name).resolve()
    if target.parent != target_dir.resolve():
        raise ValueError("File name is not allowed.")
    target.write_bytes(base64.b64decode(content_base64))
    if not data_file_allowed(target):
        target.unlink(missing_ok=True)
        raise ValueError("Only CSV, JSON, JSONL, TXT, and Markdown files can be added as data files.")
    return {"uploaded": True, "file": data_file_payload(target, "data files"), "files": list_data_files()["files"]}


def product_node_outputs(node: dict) -> dict:
    product = node.get("product") if isinstance(node.get("product"), dict) else {}
    products = product.get("products") if isinstance(product.get("products"), list) else []
    selected = str(product.get("selected") or "").strip()
    selected_product = next((item for item in products if isinstance(item, dict) and item.get("name") == selected), None)
    if not selected_product and selected and DEFAULT_PRODUCT_ROOT.exists():
        path = DEFAULT_PRODUCT_ROOT / selected
        if path.is_dir():
            selected_product = {"name": selected, "path": str(path), "fileCount": sum(1 for item in path.rglob("*") if item.is_file())}
    if not selected_product and products:
        first = products[0]
        if isinstance(first, dict):
            selected_product = first
    if not selected_product:
        raise ValueError("Product node has no selected product folder.")

    product_name = str(selected_product.get("name") or "")
    product_path = str(selected_product.get("path") or "")
    payload = {
        "product": product_name,
        "product_name": product_name,
        "product_path": product_path,
        "product_file": product_path,
        "fileCount": selected_product.get("fileCount", 0),
    }
    return {**payload, "result": payload}


def file_node_outputs(node: dict) -> dict:
    data_file = node.get("file") if isinstance(node.get("file"), dict) else {}
    files = data_file.get("files") if isinstance(data_file.get("files"), list) else []
    selected = str(data_file.get("selected") or "").strip()
    selected_file = {"path": selected} if selected else None
    if not selected_file and files:
        first = files[0]
        if isinstance(first, dict):
            selected_file = first
    if not selected_file:
        raise ValueError("Data file node has no selected file.")

    path = Path(str(selected_file.get("path") or "")).expanduser().resolve()
    if not path.is_file() or not data_file_allowed(path):
        raise ValueError(f"Data file is not available: {path}")
    if path.stat().st_size > 2_000_000:
        raise ValueError(f"Data file is too large to load into a workflow node: {path.name}")
    text = path.read_text(encoding="utf-8")
    payload = {
        "type": "file",
        "name": path.name,
        "path": str(path),
        "relativePath": str(path.relative_to(PROJECT_ROOT)),
        "url": artifact_url(path),
        "content": text,
        "text": text,
    }
    if path.suffix.lower() == ".csv":
        payload["csv"] = text
    return {**payload, "result": payload}


def connected_node_ids(workflow: dict) -> set[str]:
    node_ids = {node["id"] for node in workflow.get("nodes", [])}
    connected = set()
    for edge in workflow.get("edges", []):
        source, target = edge.get("source"), edge.get("target")
        if source in node_ids and target in node_ids:
            connected.update((source, target))
    for node in workflow.get("nodes", []):
        if node.get("includeInWorkflow"):
            connected.add(node["id"])
    if not connected:
        first_node = next(iter(topological_nodes(workflow)), None)
        if first_node:
            connected.add(first_node["id"])
    return connected


def cached_breakpoint_outputs(node: dict) -> dict | None:
    if node.get("type") != "breakpoint":
        return None
    outputs = node.get("breakpoint", {}).get("cachedOutputs")
    return outputs if isinstance(outputs, dict) and outputs else None


def first_uncached_breakpoint(workflow: dict) -> dict | None:
    breakpoints = [
        node for node in workflow.get("nodes", [])
        if node.get("type") == "breakpoint" and not cached_breakpoint_outputs(node)
    ]
    if not breakpoints:
        return None
    return sorted(breakpoints, key=lambda node: (float(node.get("x", 0)), float(node.get("y", 0))))[0]


def first_breakpoint(workflow: dict) -> dict | None:
    breakpoints = [node for node in workflow.get("nodes", []) if node.get("type") == "breakpoint"]
    if not breakpoints:
        return None
    return sorted(breakpoints, key=lambda node: (float(node.get("x", 0)), float(node.get("y", 0))))[0]


def upstream_node_ids(workflow: dict, node_ids: set[str]) -> set[str]:
    parents: dict[str, list[str]] = {}
    valid_nodes = {node["id"] for node in workflow.get("nodes", [])}
    for edge in workflow.get("edges", []):
        source, target = edge.get("source"), edge.get("target")
        if source in valid_nodes and target in valid_nodes:
            parents.setdefault(target, []).append(source)
    upstream = set()
    stack = list(node_ids)
    while stack:
        node_id = stack.pop()
        for parent in parents.get(node_id, []):
            if parent not in upstream:
                upstream.add(parent)
                stack.append(parent)
    return upstream


def edge_output_value(outputs: dict[str, dict], edge: dict) -> tuple[object, bool]:
    source_outputs = outputs.get(edge.get("source"))
    if not isinstance(source_outputs, dict):
        return None, False
    source_handle = edge.get("sourceHandle", "result")
    if source_handle in source_outputs:
        value = source_outputs[source_handle]
        return value, value is not None
    if len(source_outputs) == 1:
        value = next(iter(source_outputs.values()))
        return value, value is not None
    if "result" in source_outputs:
        value = source_outputs["result"]
        return value, value is not None
    return None, False


def first_input_value(inputs: dict) -> object:
    for key in ("prompt", "input", "value", "text"):
        value = inputs.get(key)
        if value not in (None, ""):
            return value
    for value in inputs.values():
        if value not in (None, ""):
            return value
    return None


def condition_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, sort_keys=True)


def condition_matches(value: object, node: dict) -> bool:
    condition = node.get("condition") if isinstance(node.get("condition"), dict) else {}
    match_text = str(condition.get("matchText") or node.get("code") or "").strip()
    if not match_text:
        return bool(value)
    haystack = condition_text(value)
    if condition.get("caseSensitive"):
        return match_text in haystack
    return match_text.lower() in haystack.lower()


def breakpoint_outputs_from_inputs(inputs: dict) -> dict:
    output_value = first_input_value(inputs)
    result = {"input": output_value, "result": output_value}
    if isinstance(output_value, dict):
        artifact_type = output_value.get("type")
        if artifact_type in {"image", "video", "file"}:
            result[artifact_type] = output_value
    return result


def saved_node_outputs(node: dict) -> dict | None:
    outputs = node.get("lastOutputs")
    if isinstance(outputs, dict) and outputs:
        return outputs
    last_run = node.get("lastRun") if isinstance(node.get("lastRun"), dict) else {}
    outputs = last_run.get("outputs")
    return outputs if isinstance(outputs, dict) and outputs else None


def string_input_value(inputs: dict) -> str | None:
    for key in ("prompt", "video_prompt", "input", "value", "text"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in inputs.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def input_path_value(inputs: dict, preferred_keys: tuple[str, ...] = ("reference_image", "image", "init_image")) -> str | None:
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
    for key in preferred_keys:
        value = inputs.get(key)
        if isinstance(value, dict) and value.get("path"):
            return str(value["path"])
        if isinstance(value, str) and value.strip():
            path = Path(value.strip()).expanduser()
            if path.suffix.lower() in image_suffixes:
                return value.strip()
    for value in inputs.values():
        if isinstance(value, dict) and value.get("type") == "image" and value.get("path"):
            return str(value["path"])
        if isinstance(value, str) and value.strip():
            path = Path(value.strip()).expanduser()
            if path.suffix.lower() in image_suffixes:
                return value.strip()
    return None


def artifact_url(path: Path) -> str:
    return f"/api/artifacts?path={quote(str(path))}"


def media_artifact(path_text: str, working_directory: Path) -> dict | None:
    media_path = Path(path_text.strip()).expanduser()
    if not media_path.is_absolute():
        media_path = working_directory / "outputs" / media_path
    media_path = media_path.resolve()
    if not media_path.is_file():
        return None
    suffix = media_path.suffix.lower()
    artifact_type = "image" if suffix in {".png", ".jpg", ".jpeg", ".webp"} else "video" if suffix in {".mp4", ".mov", ".webm", ".mkv"} else "file"
    return {
        "type": artifact_type,
        "path": str(media_path),
        "url": artifact_url(media_path),
        "name": media_path.name,
    }


def artifact_from_logs(logs: str, working_directory: Path) -> dict | None:
    patterns = [
        r"\bSaved\s+(.+?\.(?:png|jpe?g|webp))\b",
        r"\bvideo is ready:\s*(.+?\.(?:mp4|mov|webm|mkv))\b",
    ]
    for pattern in patterns:
        last_match = None
        for match in re.finditer(pattern, logs, re.IGNORECASE):
            last_match = match
        if last_match:
            artifact = media_artifact(last_match.group(1), working_directory)
            if artifact:
                return artifact
    return None


def structured_outputs_from_logs(logs: str) -> dict:
    orders = []
    for line in logs.splitlines():
        clean = line.strip()
        if not clean.startswith("ORDER "):
            continue
        try:
            value = json.loads(clean[len("ORDER "):])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            orders.append(value)
    if not orders:
        return {}
    return {
        "result": orders[-1] if len(orders) == 1 else orders,
        "order": orders[-1],
        "orders": orders,
    }


def workflow_visible_logs(logs: str) -> str:
    lines = [line for line in logs.splitlines() if not line.startswith("@@WORKFLOW_RESULT@@")]
    return "\n".join(lines).strip()


def work_order_args_for_script(source_code: str, inputs: dict, working_directory: Path, node_id: str) -> list[str]:
    if "--work-order" not in source_code:
        return []
    prompt = string_input_value(inputs)
    reference_image = input_path_value(inputs)
    if not prompt:
        raise ValueError("Video work-order task needs a linked prompt input.")
    if not reference_image:
        raise ValueError("Video work-order task needs a linked reference image input.")
    duration_value = inputs.get("duration_seconds", 5)
    try:
        duration = float(duration_value)
    except (TypeError, ValueError):
        raise ValueError("Video duration must be a positive number of seconds.") from None
    if isinstance(duration_value, bool) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video duration must be a positive number of seconds.")
    output_dir = working_directory / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    work_order_dir = working_directory / "work-orders"
    work_order_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    work_order = {
        "id": f"workflow-{node_id}-{stamp}",
        "reference_image": reference_image,
        "prompt": prompt,
        "target_duration_seconds": duration,
        "seed": 42,
        "output": str(output_dir / f"workflow-{node_id}-{stamp}.mp4"),
    }
    work_order_path = work_order_dir / f"{work_order['id']}.json"
    work_order_path.write_text(json.dumps(work_order, indent=2), encoding="utf-8")
    return ["--work-order", str(work_order_path)]


def local_sd_api_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            pass
    except OSError:
        return False
    for endpoint in ("options", "sd-models"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/sdapi/v1/{endpoint}", timeout=1) as response:
                return response.status < 400
        except Exception:
            continue
    return False


def first_free_local_port(start: int = 8090, stop: int = 8120) -> int:
    for port in range(start, stop + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"No free local port found between {start} and {stop}")


def action_args_for_script(source_code: str, inputs: dict) -> list[str]:
    """Bridge notebook-style node inputs into common script CLI flags."""
    args = []
    input_value = string_input_value(inputs) or first_input_value(inputs)
    if input_value is not None and "--input" in source_code:
        args.extend(["--input", json.dumps(input_value) if isinstance(input_value, (dict, list)) else str(input_value)])
    if input_value is not None and "--prompt" in source_code:
        args.extend(["--prompt", str(input_value)])
    image_value = input_path_value(inputs)
    if image_value and "--image" in source_code:
        args.extend(["--image", image_value])
    if "--no-open-output" in source_code or "--open-output" in source_code:
        args.append("--no-open-output")
    if "--order-json" in source_code:
        order_payload = order_json_payload(inputs)
        if order_payload:
            args.extend(["--order-json", json.dumps(order_payload)])
    if "--port" in source_code and local_sd_api_ready(DEFAULT_IMAGE_BACKEND_PORT):
        args.extend(["--port", str(DEFAULT_IMAGE_BACKEND_PORT)])
    elif "--launch-server" in source_code:
        args.append("--launch-server")
        if "--port" in source_code:
            args.extend(["--port", str(first_free_local_port())])
    return args


def order_json_payload(inputs: dict) -> dict | None:
    payload: dict = {}
    for value in inputs.values():
        if isinstance(value, dict):
            payload.update(value)
    if not payload:
        first = first_input_value(inputs)
        if isinstance(first, dict):
            payload.update(first)
    product_value = inputs.get("product")
    if isinstance(product_value, dict):
        payload.update({key: value for key, value in product_value.items() if value not in (None, "")})
    elif isinstance(product_value, str) and product_value.strip():
        payload["product"] = product_value.strip()
    return payload or None


def llm_auth_headers() -> dict:
    """Bearer auth for hosted OpenAI-compatible APIs; local servers ignore it."""
    api_key = os.environ.get("AUTOMATED_WORKFLOW_LLM_API_KEY", "").strip()
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def post_json(url: str, payload: dict, timeout: float = 120, headers: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def llm_activity_emit(kind: str, *, run_id: str | None = None, title: str = "", text: str = "", metadata: dict | None = None, usage: dict | None = None) -> dict:
    global LLM_ACTIVITY_NEXT_ID
    with LLM_ACTIVITY_CONDITION:
        event = {
            "id": LLM_ACTIVITY_NEXT_ID,
            "createdAt": time.time(),
            "runId": run_id,
            "kind": kind,
            "title": title,
            "text": text,
            "metadata": metadata or {},
            "usage": usage or {},
        }
        LLM_ACTIVITY_NEXT_ID += 1
        LLM_ACTIVITY_EVENTS.append(event)
        del LLM_ACTIVITY_EVENTS[:-500]
        LLM_ACTIVITY_CONDITION.notify_all()
        return event


def llm_activity_events(after: int = 0, wait: bool = False) -> dict:
    deadline = time.monotonic() + 25
    with LLM_ACTIVITY_CONDITION:
        while wait and not any(event["id"] > after for event in LLM_ACTIVITY_EVENTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            LLM_ACTIVITY_CONDITION.wait(timeout=remaining)
        return {"events": [event for event in LLM_ACTIVITY_EVENTS if event["id"] > after]}


def usage_payload(usage: dict | None, *, elapsed: float | None = None, estimated_output_tokens: int | None = None) -> dict:
    usage = usage or {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or estimated_output_tokens or 0)
    payload = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": input_tokens + output_tokens,
        "estimated": not bool(usage),
    }
    if elapsed and elapsed > 0:
        payload["elapsedSeconds"] = elapsed
        payload["tokensPerSecond"] = output_tokens / elapsed if output_tokens else 0
    return payload


def stream_openai_chat_completion(endpoint: str, payload: dict, timeout: float, on_delta) -> tuple[str, dict]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream", **llm_auth_headers()},
        method="POST",
    )
    text = ""
    usage = {}
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: "):].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            choices = chunk.get("choices") or []
            if choices:
                content = (choices[0].get("delta") or {}).get("content")
                if content:
                    text += content
                    on_delta(content)
            if chunk.get("usage"):
                usage = chunk["usage"]
    return text, usage


def local_llm_candidates(agent: dict) -> list[str]:
    candidates = []
    for value in (agent.get("endpoint"), os.environ.get("AUTOMATED_WORKFLOW_LLM_URL")):
        if value:
            candidates.append(value.rstrip("/"))
    candidates.extend([
        "http://127.0.0.1:8080/v1/chat/completions",
        "http://127.0.0.1:8080/completion",
        "http://127.0.0.1:8081/v1/chat/completions",
        "http://127.0.0.1:8081/completion",
        "http://127.0.0.1:11434/api/chat",
        "http://127.0.0.1:1234/v1/chat/completions",
        "http://127.0.0.1:8000/v1/chat/completions",
    ])
    return list(dict.fromkeys(candidates))


def call_local_llm(agent: dict, user_prompt: str, *, activity_title: str = "", activity_kind: str = "llm") -> tuple[str, str]:
    system_prompt = f"{WORKFLOW_AGENT_GUARDRAILS}\n\n{agent.get('systemPrompt') or DEFAULT_CLAUDIA_SYSTEM_PROMPT}"
    model = agent.get("model") or os.environ.get("AUTOMATED_WORKFLOW_LLM_MODEL") or "local-model"
    temperature = min(float(agent.get("temperature", 0.35)), 0.45)
    last_error = ""
    activity_run_id = uuid.uuid4().hex if activity_title else None
    started = time.monotonic()
    estimated_output_tokens = 0
    if activity_run_id:
        llm_activity_emit("start", run_id=activity_run_id, title=activity_title, text="Local model request started.", metadata={"activity": activity_kind, "model": model})
        llm_activity_emit("prompt", run_id=activity_run_id, title="Prompt", text=f"System prompt:\n{system_prompt}\n\nUser prompt:\n{user_prompt}", metadata={"model": model})
    for endpoint in local_llm_candidates(agent):
        try:
            if activity_run_id:
                llm_activity_emit("status", run_id=activity_run_id, title="Connecting", text=f"Trying {endpoint}", metadata={"endpoint": endpoint})
            if endpoint.endswith("/api/chat"):
                payload = {
                    "model": model if model != "local-model" else "llama3.1",
                    "stream": False,
                    "options": {"temperature": temperature},
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                }
                data = post_json(endpoint, payload, headers=llm_auth_headers())
                text = data.get("message", {}).get("content", "")
                if activity_run_id:
                    elapsed = time.monotonic() - started
                    estimated_output_tokens = max(1, len(text) // 4) if text else 0
                    llm_activity_emit("text", run_id=activity_run_id, title="Model output", text=text)
                    llm_activity_emit("usage", run_id=activity_run_id, title="Usage", text="Generation complete.", usage=usage_payload(None, elapsed=elapsed, estimated_output_tokens=estimated_output_tokens), metadata={"endpoint": endpoint})
                    llm_activity_emit("complete", run_id=activity_run_id, title="Complete", text="Local model request finished.", metadata={"endpoint": endpoint})
                return text, endpoint
            if endpoint.endswith("/completion"):
                payload = {
                    "prompt": f"{system_prompt}\n\nUser request:\n{user_prompt}\n\nClaudia response:",
                    "temperature": temperature,
                    "n_predict": int(agent.get("nPredict", 1024)),
                }
                data = post_json(endpoint, payload, headers=llm_auth_headers())
                text = data.get("content") or data.get("completion") or data.get("response") or ""
                if activity_run_id:
                    elapsed = time.monotonic() - started
                    estimated_output_tokens = max(1, len(text) // 4) if text else 0
                    llm_activity_emit("text", run_id=activity_run_id, title="Model output", text=text)
                    llm_activity_emit("usage", run_id=activity_run_id, title="Usage", text="Generation complete.", usage=usage_payload(None, elapsed=elapsed, estimated_output_tokens=estimated_output_tokens), metadata={"endpoint": endpoint})
                    llm_activity_emit("complete", run_id=activity_run_id, title="Complete", text="Local model request finished.", metadata={"endpoint": endpoint})
                return text, endpoint
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
            }
            if activity_run_id:
                streamed_payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
                streamed_text = ""
                streamed_code = ""
                def on_delta(delta: str) -> None:
                    nonlocal estimated_output_tokens, streamed_text, streamed_code
                    streamed_text += delta
                    estimated_output_tokens += max(1, len(delta) // 4)
                    llm_activity_emit("text_delta", run_id=activity_run_id, title="Streaming output", text=delta, metadata={"endpoint": endpoint})
                    if activity_kind.startswith("script-"):
                        code_text = partial_json_string(streamed_text, "code") or partial_json_string(streamed_text, "content")
                        if code_text.startswith(streamed_code) and len(code_text) > len(streamed_code):
                            code_delta = code_text[len(streamed_code):]
                            streamed_code = code_text
                            llm_activity_emit("code_delta", run_id=activity_run_id, title="Streaming code", text=code_delta, metadata={"endpoint": endpoint})
                text, usage = stream_openai_chat_completion(endpoint, streamed_payload, 120, on_delta)
                elapsed = time.monotonic() - started
                llm_activity_emit("usage", run_id=activity_run_id, title="Usage", text="Generation complete.", usage=usage_payload(usage, elapsed=elapsed, estimated_output_tokens=estimated_output_tokens), metadata={"endpoint": endpoint})
                llm_activity_emit("complete", run_id=activity_run_id, title="Complete", text="Local model request finished.", metadata={"endpoint": endpoint})
                return text, endpoint
            data = post_json(endpoint, payload, headers=llm_auth_headers())
            return data["choices"][0]["message"]["content"], endpoint
        except Exception as error:
            last_error = f"{endpoint}: {error}"
            if activity_run_id:
                llm_activity_emit("status", run_id=activity_run_id, title="Endpoint failed", text=last_error, metadata={"endpoint": endpoint})
            continue
    if activity_run_id:
        llm_activity_emit("error", run_id=activity_run_id, title="Local LLM error", text=f"Claudia could not reach a local LLM. Last error: {last_error}")
    raise RuntimeError(f"Claudia could not reach a local LLM. Last error: {last_error}")


def build_agent_prompt(brief: str) -> dict:
    brief = (brief or "").strip()
    placeholder = "Tell me what the agent should do"
    if not brief or brief.startswith(placeholder):
        raise ValueError("Type what the agent should do before building it with Claudia.")
    response_text, endpoint = call_local_llm(
        {"systemPrompt": AGENT_BUILDER_SYSTEM_PROMPT, "temperature": 0.25, "nPredict": 1200},
        brief,
        activity_title="Build agent prompt",
        activity_kind="agent-builder",
    )
    try:
        parsed = json.loads(response_text.strip())
    except json.JSONDecodeError:
        parsed = {"name": "Specialist Agent", "systemPrompt": response_text.strip()}
    name = str(parsed.get("name") or "Specialist Agent").strip()[:80] or "Specialist Agent"
    system_prompt = str(parsed.get("systemPrompt") or "").strip()
    if not system_prompt:
        system_prompt = response_text.strip()
    return {"name": name, "systemPrompt": system_prompt, "endpoint": endpoint}


def control_llm(action: str, sudo_password: str | None = None) -> dict:
    if action in {"start", "stop"}:
        command = ["sudo", "-S", "-p", "", "systemctl", action, LLM_SERVICE_NAME] if sudo_password else ["sudo", "-n", "systemctl", action, LLM_SERVICE_NAME]
        result = subprocess.run(
            command,
            input=f"{sudo_password}\n" if sudo_password else None,
            text=True,
            capture_output=True,
            timeout=30,
        )
        output = (result.stdout.strip() or result.stderr.strip())
        if result.returncode:
            combined = f"{result.stdout}\n{result.stderr}".lower()
            if not sudo_password and ("password" in combined or "sudo" in combined):
                return {"requiresSudo": True, "action": action, "message": "Sudo password required."}
            raise RuntimeError(output or f"Failed to {action} {LLM_SERVICE_NAME}.")
        return {"status": "started" if action == "start" else "stopped", "command": f"sudo systemctl {action} {LLM_SERVICE_NAME}", "output": output}
    raise ValueError("Unknown LLM action.")


def kill_claudia_llm() -> dict:
    command = ["pkill", "-f", LLM_KILL_PATTERN]
    result = subprocess.run(command, text=True, capture_output=True, timeout=15)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Failed to kill Claudia LLM.")
    return {
        "killed": result.returncode == 0,
        "command": f"pkill -f '{LLM_KILL_PATTERN}'",
        "output": result.stdout.strip() or result.stderr.strip(),
    }


def run_interactive_python_node(
    command: list[str],
    source_code: str,
    source_path: Path,
    cwd: Path,
    node_id: str,
    emit=lambda event: None,
    timeout_seconds: int = DEFAULT_NODE_TIMEOUT_SECONDS,
) -> tuple[str, int]:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".py", delete=False) as handle:
        handle.write(source_code)
        temp_source_path = Path(handle.name)

    session_id = uuid.uuid4().hex
    master_fd, slave_fd = pty.openpty()
    record = {"events": queue.Queue(), "done": False, "kind": "workflow-node", "cwd": str(cwd)}
    process = subprocess.Popen(
        [*command, str(temp_source_path), str(source_path)],
        cwd=str(cwd),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        text=True,
        close_fds=True,
    )
    os.close(slave_fd)
    record["process"] = process
    record["master_fd"] = master_fd
    TERMINAL_SESSIONS[session_id] = record
    emit({"type": "terminal_session", "nodeId": node_id, "sessionId": session_id})

    logs = ""
    started = time.monotonic()
    try:
        while True:
            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    text = chunk.decode("utf-8", errors="replace")
                    logs += text
                    emit({"type": "log", "nodeId": node_id, "text": text})
            if process.poll() is not None:
                while True:
                    ready, _, _ = select.select([master_fd], [], [], 0)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    logs += text
                    emit({"type": "log", "nodeId": node_id, "text": text})
                return logs, process.returncode or 0
            if timeout_seconds and time.monotonic() - started > timeout_seconds:
                process.terminate()
                raise subprocess.TimeoutExpired(command[0], timeout_seconds)
    finally:
        record["done"] = True
        try:
            os.close(master_fd)
        except OSError:
            pass
        TERMINAL_SESSIONS.pop(session_id, None)
        try:
            temp_source_path.unlink()
        except OSError:
            pass


def execute_workflow(workflow: dict, emit=lambda event: None, mode: str = "whole", cancel_requested=lambda: False) -> dict:
    """Execute each Python node in dependency order in a fresh Python process.

    Values flow through named edge handles as JSON. This is intended for local,
    trusted scripts; production execution should move to isolated workers.
    """
    outputs: dict[str, dict] = {}
    run_nodes = []
    incoming_by_target = incoming_edges_by_target(workflow)
    connected_nodes = connected_node_ids(workflow)
    boundary_breakpoint = first_breakpoint(workflow)
    boundary_breakpoint_x = float(boundary_breakpoint.get("x", 0)) if boundary_breakpoint else None
    cached_breakpoints = {
        node["id"]
        for node in workflow.get("nodes", [])
        if cached_breakpoint_outputs(node)
    }
    active_breakpoint = first_uncached_breakpoint(workflow) if mode == "whole" else boundary_breakpoint if mode == "before-break" else None
    active_breakpoint_x = float(active_breakpoint.get("x", 0)) if active_breakpoint else None
    skip_cached_upstream = upstream_node_ids(workflow, cached_breakpoints) if mode != "before-break" else set()
    if mode == "after-break" and boundary_breakpoint_x is not None:
        for node in workflow.get("nodes", []):
            if float(node.get("x", 0)) >= boundary_breakpoint_x:
                continue
            saved_outputs = saved_node_outputs(node)
            if saved_outputs:
                outputs[node["id"]] = saved_outputs
    for node in topological_nodes(workflow):
        if cancel_requested():
            raise WorkflowCancelled("Workflow run stopped.")
        if mode == "after-break":
            if not boundary_breakpoint:
                emit({"type": "log", "nodeId": node["id"], "text": "No workflow break exists; nothing to resume.\n"})
                break
            if float(node.get("x", 0)) < boundary_breakpoint_x and node["id"] != boundary_breakpoint["id"]:
                continue
        if active_breakpoint_x is not None and node.get("type") != "breakpoint" and float(node.get("x", 0)) > active_breakpoint_x:
            emit({"type": "log", "nodeId": node["id"], "text": f"Workflow paused at break before: {node.get('name', 'Node')}\n"})
            break
        if node["id"] in skip_cached_upstream:
            emit({"type": "log", "nodeId": node["id"], "text": f"Skipping cached breakpoint upstream node: {node.get('name', 'Node')}\n"})
            continue
        if node["id"] not in connected_nodes:
            emit({"type": "log", "nodeId": node["id"], "text": f"Skipping unlinked node: {node.get('name', 'Node')}\n"})
            continue
        incoming_edges = incoming_by_target.get(node["id"], [])
        if not incoming_edges and not node.get("includeInWorkflow") and not node_accepts_standalone_run(node):
            skipped = {"id": node["id"], "status": "skipped", "logs": "", "error": "Node has no incoming connection.", "outputs": {}}
            run_nodes.append(skipped)
            emit({"type": "log", "nodeId": node["id"], "text": f"Skipping disconnected node: {node.get('name', 'Node')}\n"})
            emit({"type": "node_complete", "nodeId": node["id"], "status": "skipped", "outputs": {}, "error": skipped["error"]})
            continue
        emit({"type": "node_start", "nodeId": node["id"], "name": node.get("name", "Node")})
        if node.get("type") == "input":
            output_name = (node.get("outputs") or ["value"])[0]
            result = {"id": node["id"], "status": "success", "logs": "", "error": "", "outputs": {output_name: node.get("inputValue", "")}}
            outputs[node["id"]] = result["outputs"]
            run_nodes.append(result)
            emit({"type": "log", "nodeId": node["id"], "text": f"User input: {node.get('inputValue', '')}\n"})
            emit({"type": "node_complete", "nodeId": node["id"], "status": "success", "outputs": result["outputs"]})
            continue
        inputs = {}
        supplied_inputs = 0
        for edge in incoming_edges:
            value, supplied = edge_output_value(outputs, edge)
            if supplied:
                inputs[edge.get("targetHandle", "input")] = value
                supplied_inputs += 1
        if node.get("type") == "product":
            product_outputs = product_node_outputs(node)
            outputs[node["id"]] = product_outputs
            emit({"type": "log", "nodeId": node["id"], "text": f"Selected product: {product_outputs['product']} ({product_outputs['product_path']})\n"})
            run_nodes.append({"id": node["id"], "status": "success", "logs": "", "error": "", "outputs": product_outputs})
            emit({"type": "node_complete", "nodeId": node["id"], "status": "success", "outputs": product_outputs})
            continue
        if node.get("type") == "file":
            file_outputs = file_node_outputs(node)
            outputs[node["id"]] = file_outputs
            emit({"type": "log", "nodeId": node["id"], "text": f"Selected data file: {file_outputs['relativePath']}\n"})
            run_nodes.append({"id": node["id"], "status": "success", "logs": "", "error": "", "outputs": file_outputs})
            emit({"type": "node_complete", "nodeId": node["id"], "status": "success", "outputs": file_outputs})
            continue
        if node.get("type") == "breakpoint":
            cached_outputs = cached_breakpoint_outputs(node)
            if cached_outputs and mode != "before-break":
                outputs[node["id"]] = cached_outputs
                emit({"type": "log", "nodeId": node["id"], "text": "Using saved breakpoint output.\n"})
                run_nodes.append({"id": node["id"], "status": "success", "logs": "", "error": "", "outputs": cached_outputs})
                emit({"type": "node_complete", "nodeId": node["id"], "status": "success", "outputs": cached_outputs})
                continue
        if incoming_edges and supplied_inputs == 0:
            skipped = {"id": node["id"], "status": "skipped", "logs": "", "error": "Upstream output was not available.", "outputs": {}}
            run_nodes.append(skipped)
            emit({"type": "log", "nodeId": node["id"], "text": f"Skipping {node.get('name', 'Node')}: upstream output was not available.\n"})
            emit({"type": "node_complete", "nodeId": node["id"], "status": "skipped", "outputs": {}, "error": skipped["error"]})
            continue
        if node.get("type") != "input":
            emit({"type": "log", "nodeId": node["id"], "text": f"Resolved inputs: {json.dumps(inputs, default=str)}\n"})
        if node.get("type") == "breakpoint":
            breakpoint_outputs = breakpoint_outputs_from_inputs(inputs)
            node["breakpoint"] = {
                **node.get("breakpoint", {}),
                "cachedAt": int(time.time()),
                "cachedOutputs": breakpoint_outputs,
            }
            outputs[node["id"]] = breakpoint_outputs
            emit({"type": "log", "nodeId": node["id"], "text": "Saved breakpoint output. Workflow paused here; run again to continue from this saved point.\n"})
            run_nodes.append({"id": node["id"], "status": "success", "logs": "", "error": "", "outputs": breakpoint_outputs})
            emit({"type": "node_complete", "nodeId": node["id"], "status": "success", "outputs": breakpoint_outputs})
            break
        if node.get("type") == "condition":
            output_value = first_input_value(inputs)
            matched = condition_matches(output_value, node)
            branch = "if_yes" if matched else "if_no"
            condition_outputs = {"if_yes": None, "if_no": None, "result": output_value, "matched": matched}
            condition_outputs[branch] = output_value
            outputs[node["id"]] = condition_outputs
            emit({"type": "log", "nodeId": node["id"], "text": f"Condition routed to {branch}.\n"})
            run_nodes.append({"id": node["id"], "status": "success", "logs": "", "error": "", "outputs": condition_outputs})
            emit({"type": "node_complete", "nodeId": node["id"], "status": "success", "outputs": condition_outputs})
            continue
        if node.get("type") == "output":
            output_value = first_input_value(inputs)
            outputs[node["id"]] = {"input": output_value, "result": output_value}
            emit({"type": "log", "nodeId": node["id"], "text": f"Output display: {json.dumps(output_value, default=str)}\n"})
            run_nodes.append({"id": node["id"], "status": "success", "logs": "", "error": "", "outputs": outputs[node["id"]]})
            emit({"type": "node_complete", "nodeId": node["id"], "status": "success", "outputs": outputs[node["id"]]})
            continue
        if node.get("type") == "kill-claudia":
            output_value = first_input_value(inputs)
            kill_result = kill_claudia_llm()
            outputs[node["id"]] = {"input": output_value, "result": output_value, "kill": kill_result}
            message = "Claudia LLM killed; VRAM release requested.\n" if kill_result["killed"] else "Claudia LLM was not running.\n"
            emit({"type": "log", "nodeId": node["id"], "text": f"{message}{kill_result['command']}\n"})
            run_nodes.append({"id": node["id"], "status": "success", "logs": "", "error": "", "outputs": outputs[node["id"]]})
            emit({"type": "node_complete", "nodeId": node["id"], "status": "success", "outputs": outputs[node["id"]]})
            continue
        if node.get("type") == "agent":
            prompt_value = first_input_value(inputs)
            if prompt_value is None:
                raise ValueError("Agent needs an upstream prompt or input value.")
            agent = {**node.get("agent", {}), "systemPrompt": node.get("code") or node.get("agent", {}).get("systemPrompt") or DEFAULT_CLAUDIA_SYSTEM_PROMPT}
            if agent.get("builder") == "codex" or agent.get("builtByCodex") or agent.get("model") == "codex":
                response_text = run_codex_agent(node, str(prompt_value), emit)
                emit({"type": "log", "nodeId": node["id"], "text": "Codex agent completed.\n"})
            else:
                emit({"type": "log", "nodeId": node["id"], "text": "Claudia is generating a work order...\n"})
                response_text, endpoint = call_local_llm(agent, str(prompt_value))
                emit({"type": "log", "nodeId": node["id"], "text": f"Claudia endpoint: {endpoint}\n"})
            outputs[node["id"]] = {"response": response_text, "result": response_text}
            run_nodes.append({"id": node["id"], "status": "success", "logs": "", "error": "", "outputs": outputs[node["id"]]})
            emit({"type": "node_complete", "nodeId": node["id"], "status": "success", "outputs": outputs[node["id"]]})
            continue
        payload = json.dumps(inputs)
        runner = """import json, sys\nfrom pathlib import Path\ninputs=json.loads(sys.argv[1])\nscript_args=json.loads(sys.argv[2])\nsource_path=sys.argv[3]\nscript_path=sys.argv[4]\nscript_dir=str(Path(script_path).resolve().parent)\nif script_dir not in sys.path:\n    sys.path.insert(0, script_dir)\nsys.argv=[script_path, *script_args]\nnamespace={'inputs': inputs, 'result': None, '__name__': '__main__', '__file__': script_path}\nsource=Path(source_path).read_text(encoding='utf-8')\nexec(compile(source, script_path, 'exec'), namespace)\nprint('@@WORKFLOW_RESULT@@' + json.dumps(namespace.get('result'), default=str), flush=True)\n"""
        task = node.get("task")
        if task:
            script_path = project_path(task["scriptPath"])
            if not script_path.is_file():
                raise ValueError(f"Task script no longer exists: {script_path}")
            source_code = script_path.read_text(encoding="utf-8")
            working_directory = project_path(task["workingDirectory"])
            python_executable = str(project_path(task["pythonExecutable"])) if task.get("pythonExecutable") else sys.executable
            if not Path(python_executable).is_file():
                emit({"type": "log", "nodeId": node["id"], "text": f"{python_executable} not found; using {sys.executable}\n"})
                python_executable = sys.executable
            script_args = work_order_args_for_script(source_code, inputs, working_directory, node["id"]) or action_args_for_script(source_code, inputs)
            if script_args:
                emit({"type": "log", "nodeId": node["id"], "text": f"Action arguments: {' '.join(script_args)}\n"})
        else:
            source_code = node.get("code", "result = inputs")
            working_directory = PROJECT_ROOT
            python_executable = sys.executable
            script_path = working_directory / f"workflow-node-{node['id']}.py"
            script_args = []
        logs, returncode = run_interactive_python_node(
            [python_executable, "-u", "-c", runner, payload, json.dumps(script_args)],
            source_code,
            script_path,
            working_directory,
            node["id"],
            emit,
            timeout_seconds=DEFAULT_NODE_TIMEOUT_SECONDS,
        )
        if cancel_requested():
            raise WorkflowCancelled("Workflow run stopped.")
        result = {"id": node["id"], "status": "success", "logs": logs, "error": "", "outputs": {}}
        marker = "@@WORKFLOW_RESULT@@"
        for line in logs.splitlines():
            if line.startswith(marker):
                try:
                    value = json.loads(line[len(marker):])
                    if value is None and result["outputs"]:
                        continue
                    result["outputs"] = {**value, "result": value} if isinstance(value, dict) and "result" not in value else value if isinstance(value, dict) else {"result": value}
                except json.JSONDecodeError:
                    result["status"] = "error"
                    result["error"] = "Node result must be JSON serializable."
        if result["status"] == "success":
            if not result["outputs"] or result["outputs"].get("result") is None:
                visible_logs = workflow_visible_logs(logs)
                structured_outputs = structured_outputs_from_logs(logs)
                if not structured_outputs and visible_logs:
                    structured_outputs = {"terminal": visible_logs, "result": visible_logs}
                elif visible_logs:
                    structured_outputs = {**structured_outputs, "terminal": visible_logs}
                result["outputs"] = {**structured_outputs, **{key: value for key, value in result["outputs"].items() if value is not None}}
            artifact = artifact_from_logs(logs, working_directory)
            if artifact:
                output_key = "image" if artifact.get("type") == "image" else "video" if artifact.get("type") == "video" else "file"
                result["outputs"] = {**result["outputs"], "result": artifact, output_key: artifact}
        if returncode:
            result["status"] = "error"
            result["error"] = logs
        outputs[node["id"]] = result["outputs"]
        run_nodes.append(result)
        emit({"type": "node_complete", "nodeId": node["id"], "status": result["status"], "outputs": result["outputs"], "error": result["error"]})
        if result["status"] == "error":
            break
    return {"id": uuid.uuid4().hex, "startedAt": int(time.time()), "nodes": run_nodes}


def start_run(workflow: dict, mode: str = "whole") -> str:
    run_id = uuid.uuid4().hex
    record = {"events": queue.Queue(), "done": False, "cancel": threading.Event(), "terminalSessionId": None}
    RUNS[run_id] = record

    def emit(event: dict) -> None:
        if event.get("type") == "terminal_session" and event.get("sessionId"):
            record["terminalSessionId"] = event["sessionId"]
        record["events"].put(event)

    def worker() -> None:
        try:
            result = execute_workflow(workflow, emit, mode, cancel_requested=record["cancel"].is_set)
            if workflow.get("id"):
                save_workflow(workflow)
            emit({"type": "complete", "run": result})
        except WorkflowCancelled as error:
            emit({"type": "error", "text": str(error)})
        except Exception as error:
            emit({"type": "error", "text": str(error)})
        finally:
            record["done"] = True

    threading.Thread(target=worker, daemon=True).start()
    return run_id


def stop_run(run_id: str) -> dict:
    record = RUNS.get(run_id)
    if not record:
        raise FileNotFoundError(run_id)
    record["cancel"].set()
    session_id = record.get("terminalSessionId")
    terminal_stopped = False
    if session_id and session_id in TERMINAL_SESSIONS:
        try:
            stop_terminal_session(session_id)
            terminal_stopped = True
        except FileNotFoundError:
            terminal_stopped = False
    return {"stopped": True, "terminalStopped": terminal_stopped}


def clamp_recurring_seconds(value) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_RECURRING_SECONDS
    return max(MIN_RECURRING_SECONDS, min(MAX_RECURRING_SECONDS, seconds))


def normalize_time_of_day(value) -> str:
    if not isinstance(value, str):
        return "09:00"
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        return "09:00"
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def normalize_weekdays(value) -> list[int]:
    if not isinstance(value, list):
        return list(range(7))
    weekdays = []
    for item in value:
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6 and day not in weekdays:
            weekdays.append(day)
    return sorted(weekdays) or list(range(7))


def local_sunday_weekday(timestamp: int) -> int:
    return (time.localtime(timestamp).tm_wday + 1) % 7


def next_allowed_day_at_midnight(weekdays: list[int], after: int) -> int:
    current = time.localtime(after)
    midnight = int(time.mktime((
        current.tm_year,
        current.tm_mon,
        current.tm_mday,
        0,
        0,
        0,
        current.tm_wday,
        current.tm_yday,
        current.tm_isdst,
    )))
    for day_offset in range(8):
        candidate = midnight + day_offset * 86400
        if candidate > after and local_sunday_weekday(candidate) in weekdays:
            return candidate
    return after + DEFAULT_RECURRING_SECONDS


def next_daily_run_at(time_of_day: str, after: int | None = None, weekdays: list[int] | None = None) -> int:
    after = int(after or time.time())
    weekdays = normalize_weekdays(weekdays)
    hour_text, minute_text = normalize_time_of_day(time_of_day).split(":")
    current = time.localtime(after)
    for day_offset in range(8):
        base_day = int(time.mktime((
            current.tm_year,
            current.tm_mon,
            current.tm_mday,
            int(hour_text),
            int(minute_text),
            0,
            current.tm_wday,
            current.tm_yday,
            current.tm_isdst,
        ))) + day_offset * 86400
        if base_day > after and local_sunday_weekday(base_day) in weekdays:
            return int(base_day)
    return int(after + 86400)


def next_recurring_run_at(recurring: dict, after: int | None = None) -> int:
    after = int(after or time.time())
    weekdays = normalize_weekdays(recurring.get("weekdays"))
    if recurring.get("scheduleMode") == "dailyTime":
        return next_daily_run_at(recurring.get("timeOfDay", "09:00"), after, weekdays)
    candidate = after + clamp_recurring_seconds(recurring.get("frequencySeconds", DEFAULT_RECURRING_SECONDS))
    return candidate if local_sunday_weekday(candidate) in weekdays else next_allowed_day_at_midnight(weekdays, candidate)


def effective_next_recurring_run_at(recurring: dict, state: dict | None = None, now: int | None = None) -> int:
    now = int(now or time.time())
    state = state or {}
    candidate = recurring.get("nextRunAt") or state.get("nextRunAt")
    try:
        candidate = int(candidate)
    except (TypeError, ValueError):
        candidate = 0
    if recurring.get("scheduleMode") == "dailyTime" and candidate and candidate < now - DAILY_RECURRING_GRACE_SECONDS:
        return next_recurring_run_at(recurring, now)
    return candidate or next_recurring_run_at(recurring, now)


def service_event(workflow_id: str, text: str, level: str = "info", **extra) -> None:
    event = {"id": uuid.uuid4().hex, "at": int(time.time()), "workflowId": workflow_id, "text": text, "level": level, **extra}
    with WORKFLOW_SERVICE_LOCK:
        WORKFLOW_SERVICE_EVENTS.append(event)
        del WORKFLOW_SERVICE_EVENTS[:-200]


def service_event_from_run_event(workflow_id: str, event: dict) -> None:
    event_type = event.get("type")
    if event_type == "log" and event.get("text"):
        service_event(workflow_id, event["text"].strip())
    elif event_type == "node_start":
        service_event(workflow_id, f"Starting node: {event.get('name', 'Node')}", nodeId=event.get("nodeId"))
    elif event_type == "node_complete":
        status = event.get("status", "complete")
        error = event.get("error", "")
        text = f"Node {status}: {event.get('nodeId', 'unknown')}"
        if error:
            text = f"{text} - {error}"
        service_event(
            workflow_id,
            text,
            "error" if status == "error" else "info",
            nodeId=event.get("nodeId"),
            nodeStatus=status,
            outputs=event.get("outputs") if isinstance(event.get("outputs"), dict) else {},
        )
    elif event_type == "error" and event.get("text"):
        service_event(workflow_id, event["text"], "error")
    elif event_type == "terminal_session" and event.get("sessionId"):
        service_event(
            workflow_id,
            "Interactive terminal attached. Use the terminal input for prompts or sudo.",
            sessionId=event["sessionId"],
            nodeId=event.get("nodeId"),
        )


def sync_workflow_service_state(workflow: dict) -> None:
    workflow_id = workflow.get("id")
    if not workflow_id:
        return
    recurring = workflow.get("recurring") if isinstance(workflow.get("recurring"), dict) else {}
    with WORKFLOW_SERVICE_LOCK:
        state = WORKFLOW_SERVICES.get(workflow_id, {"workflowId": workflow_id})
        if recurring.get("enabled"):
            frequency = clamp_recurring_seconds(recurring.get("frequencySeconds", DEFAULT_RECURRING_SECONDS))
            schedule_mode = recurring.get("scheduleMode", "interval")
            time_of_day = normalize_time_of_day(recurring.get("timeOfDay"))
            weekdays = normalize_weekdays(recurring.get("weekdays"))
            next_run = effective_next_recurring_run_at(recurring, state)
            state.update({
                "workflowId": workflow_id,
                "status": state.get("status") if state.get("status") == "running" else "waiting",
                "startedAt": state.get("startedAt") or int(time.time()),
                "scheduleMode": schedule_mode,
                "frequencySeconds": frequency,
                "timeOfDay": time_of_day,
                "weekdays": weekdays,
                "displayMode": recurring.get("displayMode") if recurring.get("displayMode") in {"verbose", "silent"} else "verbose",
                "nextRunAt": int(next_run),
                "lastRunAt": recurring.get("lastRunAt") or state.get("lastRunAt"),
                "lastStatus": recurring.get("lastStatus") or state.get("lastStatus", ""),
                "lastError": recurring.get("lastError") or state.get("lastError", ""),
            })
            WORKFLOW_SERVICES[workflow_id] = state
        else:
            state.update({"workflowId": workflow_id, "status": "stopped", "nextRunAt": None})
            WORKFLOW_SERVICES[workflow_id] = state


def update_workflow_recurring(workflow_id: str, updates: dict) -> dict:
    workflow = read_workflow(workflow_id)
    recurring = workflow.get("recurring") if isinstance(workflow.get("recurring"), dict) else {}
    recurring.update(updates)
    workflow["recurring"] = recurring
    return save_workflow(workflow, recurring_authoritative=True)


def start_workflow_service(workflow_id: str, frequency_seconds=None) -> dict:
    workflow = read_workflow(workflow_id)
    recurring = workflow.get("recurring") if isinstance(workflow.get("recurring"), dict) else {}
    frequency = clamp_recurring_seconds(frequency_seconds or recurring.get("frequencySeconds", DEFAULT_RECURRING_SECONDS))
    schedule_mode = recurring.get("scheduleMode", "interval")
    time_of_day = normalize_time_of_day(recurring.get("timeOfDay"))
    weekdays = normalize_weekdays(recurring.get("weekdays"))
    workflow["recurring"] = {
        **recurring,
        "enabled": True,
        "scheduleMode": schedule_mode,
        "frequencySeconds": frequency,
        "timeOfDay": time_of_day,
        "weekdays": weekdays,
        "displayMode": recurring.get("displayMode") if recurring.get("displayMode") in {"verbose", "silent"} else "verbose",
        "nextRunAt": next_recurring_run_at({"scheduleMode": schedule_mode, "frequencySeconds": frequency, "timeOfDay": time_of_day, "weekdays": weekdays}),
    }
    save_workflow(workflow, recurring_authoritative=True)
    state = {
        "workflowId": workflow_id,
        "status": "waiting",
        "startedAt": int(time.time()),
        "scheduleMode": schedule_mode,
        "frequencySeconds": frequency,
        "timeOfDay": time_of_day,
        "weekdays": weekdays,
        "displayMode": workflow["recurring"]["displayMode"],
        "nextRunAt": workflow["recurring"]["nextRunAt"],
        "message": "Recurring workflow is enabled.",
    }
    WORKFLOW_SERVICES[workflow_id] = state
    service_event(workflow_id, f"Recurring workflow enabled every {frequency} seconds.")
    return state


def stop_workflow_service(workflow_id: str) -> dict:
    workflow = read_workflow(workflow_id)
    workflow["recurring"] = {
        **(workflow.get("recurring") if isinstance(workflow.get("recurring"), dict) else {}),
        "enabled": False,
    }
    save_workflow(workflow, recurring_authoritative=True)
    state = WORKFLOW_SERVICES.get(workflow_id, {"workflowId": workflow_id})
    state.update({
        "workflowId": workflow_id,
        "status": "stopped",
        "nextRunAt": None,
        "stoppedAt": int(time.time()),
        "message": "Recurring workflow is disabled.",
    })
    WORKFLOW_SERVICES[workflow_id] = state
    service_event(workflow_id, "Recurring workflow disabled.")
    return state


def run_recurring_workflow(workflow_id: str) -> None:
    status = "success"
    error_text = ""
    frequency = DEFAULT_RECURRING_SECONDS
    recurring = {}
    now = int(time.time())
    try:
        workflow = read_workflow(workflow_id)
        recurring = workflow.get("recurring") if isinstance(workflow.get("recurring"), dict) else {}
        if not recurring.get("enabled"):
            with WORKFLOW_SERVICE_LOCK:
                state = WORKFLOW_SERVICES.get(workflow_id, {"workflowId": workflow_id})
                state.update({"status": "stopped", "nextRunAt": None})
                WORKFLOW_SERVICES[workflow_id] = state
            return
        due_at = effective_next_recurring_run_at(recurring, WORKFLOW_SERVICES.get(workflow_id, {}), now)
        if due_at > now:
            with WORKFLOW_SERVICE_LOCK:
                state = WORKFLOW_SERVICES.get(workflow_id, {"workflowId": workflow_id})
                state.update({"status": "waiting", "nextRunAt": due_at})
                WORKFLOW_SERVICES[workflow_id] = state
            return
        with WORKFLOW_SERVICE_LOCK:
            state = WORKFLOW_SERVICES.get(workflow_id, {"workflowId": workflow_id})
            state["status"] = "running"
            state["runningAt"] = int(time.time())
            WORKFLOW_SERVICES[workflow_id] = state
        service_event(workflow_id, "Recurring run started.")
        frequency = clamp_recurring_seconds(recurring.get("frequencySeconds", DEFAULT_RECURRING_SECONDS))
        schedule_mode = recurring.get("scheduleMode", "interval")
        time_of_day = normalize_time_of_day(recurring.get("timeOfDay"))
        weekdays = normalize_weekdays(recurring.get("weekdays"))
        result = execute_workflow(workflow, lambda event: service_event_from_run_event(workflow_id, event))
        failed = next((node for node in result.get("nodes", []) if node.get("status") == "error"), None)
        if failed:
            status = "error"
            error_text = failed.get("error", "Workflow node failed.")
    except Exception as error:
        status = "error"
        error_text = str(error)
    now = int(time.time())
    try:
        workflow = read_workflow(workflow_id)
        recurring = workflow.get("recurring") if isinstance(workflow.get("recurring"), dict) else recurring
        frequency = clamp_recurring_seconds(recurring.get("frequencySeconds", frequency))
        schedule_mode = recurring.get("scheduleMode", "interval")
        time_of_day = normalize_time_of_day(recurring.get("timeOfDay"))
        weekdays = normalize_weekdays(recurring.get("weekdays"))
        if recurring.get("enabled"):
            recurring.update({
                "lastRunAt": now,
                "lastStatus": status,
                "lastError": error_text,
                "scheduleMode": schedule_mode,
                "frequencySeconds": frequency,
                "timeOfDay": time_of_day,
                "weekdays": weekdays,
                "displayMode": recurring.get("displayMode") if recurring.get("displayMode") in {"verbose", "silent"} else "verbose",
                "nextRunAt": next_recurring_run_at(recurring, now),
            })
            workflow["recurring"] = recurring
            save_workflow(workflow, recurring_authoritative=True)
    except Exception as error:
        status = "error"
        error_text = str(error)
        recurring = {"enabled": True, "nextRunAt": now + frequency}
    with WORKFLOW_SERVICE_LOCK:
        state = WORKFLOW_SERVICES.get(workflow_id, {"workflowId": workflow_id})
        state.update({
            "status": "waiting" if recurring.get("enabled") else "stopped",
            "lastRunAt": now,
            "lastStatus": status,
            "lastError": error_text,
            "nextRunAt": recurring.get("nextRunAt") if recurring.get("enabled") else None,
            "scheduleMode": recurring.get("scheduleMode", "interval"),
            "frequencySeconds": frequency,
            "timeOfDay": normalize_time_of_day(recurring.get("timeOfDay")),
            "weekdays": normalize_weekdays(recurring.get("weekdays")),
            "displayMode": recurring.get("displayMode") if recurring.get("displayMode") in {"verbose", "silent"} else "verbose",
        })
        WORKFLOW_SERVICES[workflow_id] = state
    service_event(workflow_id, "Recurring run finished." if status == "success" else f"Recurring run failed: {error_text}", "error" if status == "error" else "info")


def scheduler_loop() -> None:
    while True:
        now = int(time.time())
        seen = set()
        for path in iter_workflow_files():
            try:
                workflow = json.loads(path.read_text(encoding="utf-8"))
                workflow_id = workflow_id_from_file(path, workflow)
                if workflow_id in seen:
                    continue
                seen.add(workflow_id)
                workflow["id"] = workflow_id
                sync_workflow_service_state(workflow)
                recurring = workflow.get("recurring") if isinstance(workflow.get("recurring"), dict) else {}
                if not recurring.get("enabled"):
                    continue
                state = WORKFLOW_SERVICES.get(workflow_id, {})
                if state.get("status") == "running":
                    continue
                next_run_at = effective_next_recurring_run_at(recurring, state, now)
                state["nextRunAt"] = next_run_at
                WORKFLOW_SERVICES[workflow_id] = state
                if next_run_at <= now:
                    with WORKFLOW_SERVICE_LOCK:
                        state = WORKFLOW_SERVICES.get(workflow_id, {"workflowId": workflow_id})
                        if state.get("status") == "running":
                            continue
                        state.update({"workflowId": workflow_id, "status": "running", "runningAt": now})
                        WORKFLOW_SERVICES[workflow_id] = state
                    threading.Thread(target=run_recurring_workflow, args=(workflow_id,), daemon=True).start()
            except json.JSONDecodeError:
                continue
            except Exception as error:
                workflow_id = path.parent.name if path.name == WORKFLOW_FILE_NAME else path.stem
                try:
                    service_event(workflow_id, f"Recurring scheduler error: {error}", "error")
                    with WORKFLOW_SERVICE_LOCK:
                        state = WORKFLOW_SERVICES.get(workflow_id, {"workflowId": workflow_id})
                        state.update({"status": "waiting", "lastStatus": "error", "lastError": str(error), "nextRunAt": now + DEFAULT_RECURRING_SECONDS})
                        WORKFLOW_SERVICES[workflow_id] = state
                except Exception:
                    pass
        time.sleep(1)


def start_scheduler() -> None:
    global SCHEDULER_STARTED
    if SCHEDULER_STARTED:
        return
    SCHEDULER_STARTED = True
    threading.Thread(target=scheduler_loop, daemon=True).start()


def workflow_service_events() -> dict:
    with WORKFLOW_SERVICE_LOCK:
        return {"events": list(WORKFLOW_SERVICE_EVENTS)}


def project_slug_from_brief(brief: str, fallback: str = "script-project") -> str:
    stop_words = {
        "please", "can", "you", "could", "would", "i", "need", "make", "create", "build", "write",
        "generate", "a", "an", "the", "python", "script", "program", "app", "application", "project",
        "in", "with", "using", "for", "to", "me", "play",
    }
    raw_slug = re.sub(r"[^a-z0-9]+", "-", (brief or "").lower()).strip("-") or fallback
    tokens = [token for token in raw_slug.split("-") if token and token not in stop_words]
    return "-".join(tokens)[:52].strip("-") or slugify(brief, fallback)


def project_slug_from_title(title: str, fallback: str = "script-project") -> str:
    return project_slug_from_brief(title, fallback)


def parse_json_object(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def strip_code_fence(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:python|py)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def partial_json_string(arguments: str, field: str) -> str:
    marker = f'"{field}"'
    found = arguments.find(marker)
    if found < 0:
        return ""
    colon = arguments.find(":", found + len(marker))
    if colon < 0:
        return ""
    start = arguments.find('"', colon + 1)
    if start < 0:
        return ""
    value = []
    escaped = False
    for char in arguments[start + 1:]:
        if escaped:
            value.append("\\" + char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            break
        else:
            value.append(char)
    try:
        return json.loads(f'"{"".join(value)}"')
    except json.JSONDecodeError:
        return "".join(value).replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def unique_project_dir(base_slug: str) -> Path:
    candidate = GENERATED_PROJECTS_DIR / base_slug
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = GENERATED_PROJECTS_DIR / f"{base_slug}-{index}"
        if not candidate.exists():
            return candidate
    raise ValueError("Could not allocate a generated project folder.")


def terminal_emit(record: dict, event: dict) -> None:
    record["events"].put(event)


def start_terminal_session(kind: str, command: list[str], cwd: Path, initial_input: str = "") -> str:
    cwd = cwd.resolve()
    allowed_roots = [PROJECT_ROOT.resolve(), DEFAULT_PROJECTS_DIR.resolve()]
    if not any(cwd == root or root in cwd.parents for root in allowed_roots):
        raise ValueError("Terminal session folder must stay inside the local projects folder.")
    session_id = uuid.uuid4().hex
    record = {"events": queue.Queue(), "done": False, "kind": kind, "cwd": str(cwd)}
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        text=True,
        close_fds=True,
    )
    os.close(slave_fd)
    record["process"] = process
    record["master_fd"] = master_fd
    TERMINAL_SESSIONS[session_id] = record
    terminal_emit(record, {"type": "start", "sessionId": session_id, "kind": kind, "cwd": str(cwd), "command": " ".join(command)})

    def reader() -> None:
        try:
            while process.poll() is None:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                terminal_emit(record, {"type": "output", "text": chunk.decode("utf-8", errors="replace")})
            while True:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                terminal_emit(record, {"type": "output", "text": chunk.decode("utf-8", errors="replace")})
            code = process.wait()
            terminal_emit(record, {"type": "exit", "code": code})
        except Exception as error:
            terminal_emit(record, {"type": "error", "text": str(error)})
        finally:
            record["done"] = True
            try:
                os.close(master_fd)
            except OSError:
                pass

    threading.Thread(target=reader, daemon=True).start()
    if initial_input:
        send_terminal_input(session_id, initial_input)
    return session_id


def send_terminal_input(session_id: str, text: str) -> dict:
    record = TERMINAL_SESSIONS.get(session_id)
    if not record:
        raise FileNotFoundError(session_id)
    process: subprocess.Popen = record["process"]
    if process.poll() is not None:
        raise ValueError("Terminal session is not accepting input.")
    os.write(record["master_fd"], text.encode("utf-8"))
    return {"sent": True}


def stop_terminal_session(session_id: str) -> dict:
    record = TERMINAL_SESSIONS.get(session_id)
    if not record:
        raise FileNotFoundError(session_id)
    process: subprocess.Popen = record["process"]
    if process.poll() is None:
        process.terminate()
    record["done"] = True
    terminal_emit(record, {"type": "exit", "code": process.poll()})
    return {"stopped": True}


def codex_command() -> list[str]:
    fallback = Path.home() / ".local" / "bin" / "codex"
    path = shutil.which("codex") or str(fallback)
    if not Path(path).exists() and shutil.which("codex") is None:
        raise FileNotFoundError(f"Codex CLI was not found on PATH or at {fallback}.")
    # User services do not inherit the interactive shell's PATH. The npm
    # launcher may be present even when its /usr/bin/env node cannot resolve.
    with Path(path).open("rb") as handle:
        shebang = handle.readline(256).strip()
    if shebang == b"#!/usr/bin/env node":
        node = shutil.which("node")
        if not node:
            candidate = Path(path).parent / "node"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                node = str(candidate)
        if not node:
            raise FileNotFoundError(
                "Codex requires Node.js, but node was not found on PATH or beside the Codex launcher."
            )
        return [node, path]
    return [path]


def run_codex_agent(node: dict, user_prompt: str, emit) -> str:
    agent = node.get("agent", {}) if isinstance(node.get("agent"), dict) else {}
    system_prompt = node.get("code") or agent.get("systemPrompt") or "You are a Codex-backed Automated Workflow agent. Return only the downstream value."
    prompt = (
        "You are running as a Codex-backed agent inside Automated Workflow.\n"
        "Follow this agent system prompt exactly:\n\n"
        f"{system_prompt}\n\n"
        "Upstream workflow input:\n"
        f"{user_prompt}\n\n"
        "Return only the downstream value. Do not include markdown fences unless the agent system prompt explicitly requires them.\n"
    )
    emit({"type": "log", "nodeId": node["id"], "text": "Codex agent is generating a response...\n"})
    process = subprocess.run(
        [*codex_command(), "exec", "--color", "never", "--skip-git-repo-check", "--sandbox", "workspace-write", prompt],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=300,
    )
    combined = (process.stdout or "").strip()
    if process.stderr:
        emit({"type": "log", "nodeId": node["id"], "text": process.stderr})
    if process.returncode:
        raise RuntimeError(combined or process.stderr.strip() or "Codex agent failed.")
    return combined


def safe_project_file(project_dir: Path, relative_path: str) -> Path:
    relative_path = (relative_path or "").strip().lstrip("/")
    if not relative_path or relative_path.endswith("/"):
        raise ValueError("Generated file path is invalid.")
    path = (project_dir / relative_path).resolve()
    if not (path == project_dir.resolve() or project_dir.resolve() in path.parents):
        raise ValueError("Generated file path escapes the project folder.")
    return path


def create_generated_project(brief: str, prefix: str, title: str = "") -> Path:
    brief = (brief or "").strip()
    if not brief:
        raise ValueError("Tell the agent what script to create.")
    project_dir = unique_project_dir(project_slug_from_title(title or brief, f"{prefix}-script"))
    project_dir.mkdir(parents=True)
    subprocess.run(["python3", "-m", "venv", "venv"], cwd=str(project_dir), check=True, timeout=120)
    (project_dir / "REQUEST.md").write_text(f"# Script Request\n\n{brief}\n", encoding="utf-8")
    return project_dir


def create_codex_script_project(brief: str) -> dict:
    brief = (brief or "").strip()
    if not brief:
        raise ValueError("Tell Codex what script to create.")
    project_dir = create_generated_project(brief, "codex")
    initial_prompt = (
        "You are Codex running inside an Automated Workflow generated project folder.\n"
        "Create the requested script project in this directory. Ask concise questions if required.\n"
        "Use the local venv at ./venv for Python dependencies and document any commands needed to run it.\n"
        "When code is ready, keep the main runnable script obvious, preferably main.py.\n\n"
        f"User request: {brief}\n"
    )
    session_id = start_terminal_session(
        "codex-script",
        [*codex_command(), "exec", "--color", "never", "--skip-git-repo-check", "--sandbox", "workspace-write", initial_prompt],
        project_dir,
    )
    llm_activity_emit("start", run_id=session_id, title=f"Codex project: {project_dir.name}", text="Codex script creation session started.", metadata={"activity": "script-create-codex", "projectDir": str(project_dir)})
    llm_activity_emit("prompt", run_id=session_id, title="Codex prompt", text=initial_prompt, metadata={"projectDir": str(project_dir)})
    return {
        "sessionId": session_id,
        "projectName": project_dir.name,
        "projectDir": str(project_dir),
        "scriptPath": str(project_dir / "main.py"),
        "pythonExecutable": str(project_dir / "venv" / "bin" / "python"),
    }


def create_codex_agent_project(brief: str) -> dict:
    brief = (brief or "").strip()
    if not brief:
        raise ValueError("Tell Codex what agent to create.")
    project_dir = unique_project_dir(project_slug_from_title(brief, "codex-agent"))
    project_dir.mkdir(parents=True)
    (project_dir / "REQUEST.md").write_text(f"# Agent Request\n\n{brief}\n", encoding="utf-8")
    agent_path = project_dir / "agent.json"
    initial_prompt = (
        "You are Codex running inside an Automated Workflow generated agent folder.\n"
        "Create a workflow specialist agent definition from the user request.\n"
        "Write exactly one JSON file named agent.json in this directory with these keys:\n"
        "- name: short UI-friendly agent name.\n"
        "- systemPrompt: directly usable system prompt for the specialist agent.\n\n"
        "The systemPrompt must preserve the requested purpose, define the agent role, responsibilities, input expectations, output expectations, quality bar, and constraints.\n"
        "Keep it conservative: do not invent subjects, settings, facts, or requirements beyond the user's request. Include an instruction to return only the downstream value.\n"
        "Do not create a Python project. Do not wrap the JSON in markdown.\n\n"
        f"User request: {brief}\n"
    )
    session_id = start_terminal_session(
        "codex-agent",
        [*codex_command(), "exec", "--color", "never", "--skip-git-repo-check", "--sandbox", "workspace-write", initial_prompt],
        project_dir,
    )
    llm_activity_emit("start", run_id=session_id, title=f"Codex agent: {project_dir.name}", text="Codex agent build session started.", metadata={"activity": "agent-build-codex", "projectDir": str(project_dir)})
    llm_activity_emit("prompt", run_id=session_id, title="Codex agent prompt", text=initial_prompt, metadata={"projectDir": str(project_dir)})
    return {"sessionId": session_id, "projectName": project_dir.name, "projectDir": str(project_dir), "agentPath": str(agent_path)}


def workflow_context_for_codex(workflow: dict) -> str:
    if not isinstance(workflow, dict):
        workflow = {}
    nodes = workflow.get("nodes") if isinstance(workflow.get("nodes"), list) else []
    edges = workflow.get("edges") if isinstance(workflow.get("edges"), list) else []
    node_lines = []
    for node in nodes[:60]:
        if not isinstance(node, dict):
            continue
        task = node.get("task") if isinstance(node.get("task"), dict) else {}
        label = f"- {node.get('name') or node.get('id')}: type={node.get('type') or 'code'}"
        if task.get("scriptPath"):
            label += f", script={task.get('scriptPath')}"
        if node.get("outputs"):
            label += f", outputs={node.get('outputs')}"
        if node.get("inputs"):
            label += f", inputs={node.get('inputs')}"
        node_lines.append(label)
    edge_lines = []
    for edge in edges[:80]:
        if not isinstance(edge, dict):
            continue
        edge_lines.append(
            f"- {edge.get('source')}:{edge.get('sourceHandle', 'result')} -> "
            f"{edge.get('target')}:{edge.get('targetHandle', 'input')}"
        )
    compact = {
        "id": workflow.get("id"),
        "name": workflow.get("name"),
        "nodeCount": len(nodes),
        "edgeCount": len(edges),
        "nodes": node_lines,
        "edges": edge_lines,
    }
    return json.dumps(compact, indent=2)


def create_codex_workflow_terminal(workflow: dict | None = None) -> dict:
    context = workflow_context_for_codex(workflow or {})
    prompt = (
        "You are Codex working inside the Automated-Workflow dashboard project.\n"
        f"Repository root: {PROJECT_ROOT}\n\n"
        "The user opened you from the dashboard to modify the workflow system and related scripts without leaving the app.\n"
        "Help design and implement workflow changes directly in this repo when asked.\n"
        "Be careful with existing dirty files: inspect before editing and do not revert unrelated user work.\n"
        "Prefer the project's existing patterns. Validate with focused checks like node --check web/app.js and python py_compile when relevant.\n"
        "User preference to preserve for workflow generation: include output display boxes between task nodes so script outputs are easy to visualize.\n\n"
        "Current dashboard workflow context:\n"
        f"{context}\n\n"
        "Wait for the user's next instruction in this terminal."
    )
    session_id = start_terminal_session(
        "codex-workflow",
        [*codex_command(), "--sandbox", "workspace-write", "--ask-for-approval", "on-request", prompt],
        PROJECT_ROOT,
    )
    llm_activity_emit("start", run_id=session_id, title="Codex workflow terminal", text="Interactive Codex workflow terminal started.", metadata={"activity": "workflow-codex-terminal", "projectDir": str(PROJECT_ROOT)})
    llm_activity_emit("prompt", run_id=session_id, title="Codex workflow context", text=prompt, metadata={"projectDir": str(PROJECT_ROOT)})
    return {
        "sessionId": session_id,
        "projectDir": str(PROJECT_ROOT),
        "workflowName": (workflow or {}).get("name") if isinstance(workflow, dict) else "",
    }


def codex_agent_details(project_dir_text: str) -> dict:
    project_dir = project_path(project_dir_text).resolve()
    generated_root = GENERATED_PROJECTS_DIR.resolve()
    if generated_root not in project_dir.parents and project_dir != generated_root:
        raise ValueError("Agent project must be inside generated-projects.")
    agent_path = project_dir / "agent.json"
    if not agent_path.is_file():
        raise FileNotFoundError(f"{agent_path} was not created.")
    data = json.loads(agent_path.read_text(encoding="utf-8"))
    name = str(data.get("name") or "Codex Agent").strip()[:80] or "Codex Agent"
    system_prompt = str(data.get("systemPrompt") or "").strip()
    if not system_prompt:
        raise ValueError("agent.json is missing systemPrompt.")
    return {"name": name, "systemPrompt": system_prompt, "agentPath": str(agent_path)}


def edit_task_script_with_local_llm(script_path_text: str, instructions: str) -> dict:
    instructions = (instructions or "").strip()
    if not instructions:
        raise ValueError("Describe what should change in the script.")
    details = project_details(script_path_text)
    source_code = details["code"]
    prompt = (
        f"Script path: {details['scriptPath']}\n\n"
        f"Requested change:\n{instructions}\n\n"
        "Current Python file:\n"
        "```python\n"
        f"{source_code}\n"
        "```\n"
    )
    response_text, endpoint = call_local_llm(
        {"systemPrompt": SCRIPT_EDITOR_SYSTEM_PROMPT, "temperature": 0.15, "nPredict": 12000},
        prompt,
        activity_title=f"Edit script: {Path(details['scriptPath']).name}",
        activity_kind="script-edit-local",
    )
    try:
        parsed = parse_json_object(response_text)
        updated_code = strip_code_fence(parsed.get("code", ""))
        notes = str(parsed.get("notes") or "").strip()
    except json.JSONDecodeError:
        updated_code = strip_code_fence(response_text)
        notes = ""
    if not updated_code or "def " not in updated_code and "import " not in updated_code and "from " not in updated_code:
        raise ValueError("The local LLM did not return a complete Python file.")
    script_path = Path(details["scriptPath"])
    script_path.write_text(updated_code.rstrip() + "\n", encoding="utf-8")
    updated_details = project_details(str(script_path))
    return {"taskDetails": updated_details, "endpoint": endpoint, "notes": notes}


def edit_task_script_with_codex(script_path_text: str, instructions: str) -> dict:
    instructions = (instructions or "").strip()
    if not instructions:
        raise ValueError("Describe what Codex should change in the script.")
    details = project_details(script_path_text)
    script_path = Path(details["scriptPath"])
    working_directory = Path(details["workingDirectory"])
    prompt = (
        "You are Codex editing an existing Automated Workflow Python task script.\n"
        f"Edit this exact file: {script_path}\n"
        "Keep the script runnable from its current working directory and preserve existing behavior unless requested.\n"
        "After editing, run a lightweight syntax check on the file if possible.\n\n"
        f"Requested change:\n{instructions}\n"
    )
    session_id = start_terminal_session(
        "codex-edit",
        [*codex_command(), "exec", "--color", "never", "--skip-git-repo-check", "--sandbox", "workspace-write", prompt],
        working_directory,
    )
    llm_activity_emit("start", run_id=session_id, title=f"Codex edit: {script_path.name}", text="Codex edit session started.", metadata={"activity": "script-edit-codex", "scriptPath": str(script_path), "workingDirectory": str(working_directory)})
    llm_activity_emit("prompt", run_id=session_id, title="Codex prompt", text=prompt, metadata={"scriptPath": str(script_path)})
    return {
        "sessionId": session_id,
        "scriptPath": str(script_path),
        "workingDirectory": str(working_directory),
    }


def generated_project_task_details(project_dir_text: str) -> dict:
    project_dir = Path(project_dir_text or "").expanduser().resolve()
    generated_root = GENERATED_PROJECTS_DIR.resolve()
    if not (project_dir == generated_root or generated_root in project_dir.parents):
        raise ValueError("Generated project folder is outside the managed projects directory.")
    script_path = project_dir / "main.py"
    if not script_path.is_file():
        python_files = sorted(project_dir.glob("*.py"))
        if not python_files:
            raise FileNotFoundError("No Python entry point was found in the generated project.")
        script_path = python_files[0]
    details = project_details(str(script_path))
    details["displayName"] = project_dir.name
    return {
        "projectName": project_dir.name,
        "projectDir": str(project_dir),
        "taskDetails": details,
    }


def create_claudia_script_project(brief: str) -> dict:
    brief = (brief or "").strip()
    if not brief:
        raise ValueError("Tell Claudia what script to create.")
    response_text, endpoint = call_local_llm(
        {"systemPrompt": SCRIPT_BUILDER_SYSTEM_PROMPT, "temperature": 0.2, "nPredict": 4096},
        brief,
        activity_title="Create script with local LLM",
        activity_kind="script-create-local",
    )
    parsed = parse_json_object(response_text)
    project_name = str(parsed.get("name") or brief).strip()
    project_dir = create_generated_project(brief, "claudia", project_name)
    files = parsed.get("files") or []
    if not isinstance(files, list) or not files:
        raise ValueError("Claudia did not return any files to create.")
    for item in files:
        if not isinstance(item, dict):
            continue
        target = safe_project_file(project_dir, str(item.get("path") or ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(item.get("content") or ""), encoding="utf-8")
    requirements = [str(item).strip() for item in (parsed.get("requirements") or []) if str(item).strip()]
    install_output = ""
    if requirements:
        requirements_path = project_dir / "requirements.txt"
        requirements_path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        install = subprocess.run(
            [str(project_dir / "venv" / "bin" / "python"), "-m", "pip", "install", "-r", str(requirements_path)],
            cwd=str(project_dir),
            text=True,
            capture_output=True,
            timeout=240,
        )
        install_output = (install.stdout + install.stderr).strip()
        if install.returncode:
            raise RuntimeError(install_output or "Dependency installation failed.")
    notes = str(parsed.get("notes") or "").strip()
    run_command = str(parsed.get("runCommand") or "./venv/bin/python main.py").strip()
    (project_dir / "README.md").write_text(
        f"# {parsed.get('name') or project_dir.name}\n\n## Run\n\n`{run_command}`\n\n## Notes\n\n{notes or 'No notes.'}\n",
        encoding="utf-8",
    )
    script_path = project_dir / "main.py"
    if not script_path.is_file():
        python_files = sorted(project_dir.glob("*.py"))
        if not python_files:
            raise ValueError("Claudia did not create a Python entry point.")
        script_path = python_files[0]
    details = project_details(str(script_path))
    details["displayName"] = project_name[:80] or script_path.name
    return {
        "projectName": project_dir.name,
        "projectDir": str(project_dir),
        "endpoint": endpoint,
        "notes": notes,
        "runCommand": run_command,
        "installOutput": install_output,
        "taskDetails": details,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path.startswith("/api/terminal/") and path.endswith("/events"):
            session_id = path.split("/")[-2]
            record = TERMINAL_SESSIONS.get(session_id)
            if not record:
                return self.send_json({"error": "Terminal session not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            while not (record["done"] and record["events"].empty()):
                try:
                    event = record["events"].get(timeout=0.5)
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            return
        if path.startswith("/api/runs/") and path.endswith("/events"):
            run_id = path.split("/")[-2]
            record = RUNS.get(run_id)
            if not record:
                return self.send_json({"error": "Run not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            while not (record["done"] and record["events"].empty()):
                try:
                    event = record["events"].get(timeout=0.5)
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            return
        if path == "/api/artifacts":
            artifact_values = parse_qs(parsed_url.query).get("path", [])
            if not artifact_values:
                return self.send_json({"error": "Artifact path is required"}, 400)
            artifact_path = Path(unquote(artifact_values[0])).expanduser().resolve()
            allowed_roots = [PROJECT_ROOT.resolve(), DEFAULT_PROJECTS_DIR.resolve(), Path("/tmp").resolve()]
            if not any(artifact_path == root or root in artifact_path.parents for root in allowed_roots):
                return self.send_json({"error": "Artifact path is outside the allowed project folders"}, 403)
            if not artifact_path.is_file():
                return self.send_json({"error": "Artifact not found"}, 404)
            content = artifact_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(artifact_path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if path == "/api/workflows":
            return self.send_json(list_workflows())
        if path == "/api/products":
            return self.send_json(list_products())
        if path == "/api/data-files":
            params = parse_qs(parsed_url.query)
            workflow_id = params.get("workflowId", [""])[0] or None
            return self.send_json(list_data_files(workflow_id))
        if path == "/api/llm-activity/events":
            params = parse_qs(parsed_url.query)
            after = int(params.get("after", ["0"])[0] or 0)
            wait = params.get("wait", ["1"])[0] != "0"
            return self.send_json(llm_activity_events(after, wait))
        if path == "/api/workflow-service/events":
            return self.send_json(workflow_service_events())
        if path.startswith("/api/workflows/"):
            try:
                return self.send_json(read_workflow(path.rsplit("/", 1)[-1]))
            except (FileNotFoundError, ValueError):
                return self.send_json({"error": "Workflow not found"}, 404)
        requested = "index.html" if path == "/" else path.lstrip("/")
        file_path = (STATIC_DIR / requested).resolve()
        if STATIC_DIR not in file_path.parents and file_path != STATIC_DIR:
            return self.send_error(HTTPStatus.NOT_FOUND)
        if not file_path.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        content = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.body_json()
            if path == "/api/tasks/pick":
                return self.send_json(choose_task_script())
            if path == "/api/tasks/image-generation":
                return self.send_json(image_generation_task())
            if path == "/api/tasks/details":
                return self.send_json(project_details(payload.get("scriptPath", "")))
            if path == "/api/tasks/edit-local-llm":
                return self.send_json(edit_task_script_with_local_llm(payload.get("scriptPath", ""), payload.get("instructions", "")))
            if path == "/api/tasks/edit-codex":
                return self.send_json(edit_task_script_with_codex(payload.get("scriptPath", ""), payload.get("instructions", "")), 201)
            if path == "/api/agents/build-prompt":
                return self.send_json(build_agent_prompt(payload.get("brief", "")))
            if path == "/api/claudia/create-script":
                return self.send_json(create_claudia_script_project(payload.get("brief", "")), 201)
            if path == "/api/codex/create-script":
                return self.send_json(create_codex_script_project(payload.get("brief", "")), 201)
            if path == "/api/codex/workflow-terminal":
                return self.send_json(create_codex_workflow_terminal(payload.get("workflow")), 201)
            if path == "/api/codex/build-agent":
                return self.send_json(create_codex_agent_project(payload.get("brief", "")), 201)
            if path == "/api/codex/agent-details":
                return self.send_json(codex_agent_details(payload.get("projectDir", "")))
            if path == "/api/generated-projects/task-details":
                return self.send_json(generated_project_task_details(payload.get("projectDir", "")))
            if path.startswith("/api/terminal/") and path.endswith("/input"):
                return self.send_json(send_terminal_input(path.split("/")[-2], payload.get("text", "")))
            if path.startswith("/api/terminal/") and path.endswith("/stop"):
                return self.send_json(stop_terminal_session(path.split("/")[-2]))
            if path.startswith("/api/runs/") and path.endswith("/stop"):
                return self.send_json(stop_run(path.split("/")[-2]))
            if path == "/api/llm/start":
                return self.send_json(control_llm("start", payload.get("sudoPassword")))
            if path == "/api/llm/stop":
                return self.send_json(control_llm("stop", payload.get("sudoPassword")))
            if path == "/api/products/upload":
                return self.send_json(upload_product_file(payload), 201)
            if path == "/api/data-files/upload":
                return self.send_json(upload_data_file(payload), 201)
            if path == "/api/workflows":
                return self.send_json(save_workflow(payload), 201)
            if path == "/api/workflows/active-order":
                return self.send_json(update_active_workflow_order(payload.get("workflowIds", [])))
            if path.startswith("/api/workflows/") and path.endswith("/service/start"):
                return self.send_json(start_workflow_service(path.split("/")[-3], payload.get("frequencySeconds")))
            if path.startswith("/api/workflows/") and path.endswith("/service/stop"):
                return self.send_json(stop_workflow_service(path.split("/")[-3]))
            if path.startswith("/api/workflows/") and path.endswith("/recurring"):
                workflow_id = path.split("/")[-2]
                updates = {}
                if "enabled" in payload:
                    updates["enabled"] = bool(payload.get("enabled"))
                if payload.get("scheduleMode") in {"interval", "dailyTime"}:
                    updates["scheduleMode"] = payload.get("scheduleMode")
                if "frequencySeconds" in payload:
                    updates["frequencySeconds"] = clamp_recurring_seconds(payload.get("frequencySeconds"))
                if "timeOfDay" in payload:
                    updates["timeOfDay"] = normalize_time_of_day(payload.get("timeOfDay"))
                if "weekdays" in payload:
                    updates["weekdays"] = normalize_weekdays(payload.get("weekdays"))
                if payload.get("displayMode") in {"verbose", "silent"}:
                    updates["displayMode"] = payload.get("displayMode")
                if updates.get("enabled"):
                    updates.setdefault("frequencySeconds", DEFAULT_RECURRING_SECONDS)
                    updates.setdefault("scheduleMode", "interval")
                    updates.setdefault("timeOfDay", "09:00")
                    updates.setdefault("weekdays", list(range(7)))
                    updates["nextRunAt"] = next_recurring_run_at(updates)
                if updates.get("enabled") is False:
                    updates["nextRunAt"] = None
                return self.send_json(update_workflow_recurring(workflow_id, updates))
            if path.startswith("/api/workflows/") and path.endswith("/run"):
                workflow = read_workflow(path.split("/")[-2])
                mode = payload.get("mode", "whole")
                if mode not in {"whole", "before-break", "after-break"}:
                    raise ValueError("Unknown workflow run mode.")
                return self.send_json({"runId": start_run(workflow, mode)})
            if path.startswith("/api/workflows/"):
                payload["id"] = path.rsplit("/", 1)[-1]
                return self.send_json(save_workflow(payload))
        except subprocess.TimeoutExpired:
            limit = DEFAULT_NODE_TIMEOUT_SECONDS or "configured"
            return self.send_json({"error": f"A node exceeded the {limit} second timeout."}, 408)
        except (ValueError, json.JSONDecodeError) as error:
            return self.send_json({"error": str(error)}, 400)
        except FileNotFoundError:
            return self.send_json({"error": "Workflow not found"}, 404)
        except Exception as error:
            return self.send_json({"error": str(error)}, 500)
        return self.send_json({"error": "Not found"}, 404)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/workflows/"):
                delete_workflow(path.rsplit("/", 1)[-1])
                return self.send_json({"deleted": True})
        except ValueError as error:
            return self.send_json({"error": str(error)}, 400)
        except FileNotFoundError:
            return self.send_json({"error": "Workflow not found"}, 404)
        except Exception as error:
            return self.send_json({"error": str(error)}, 500)
        return self.send_json({"error": "Not found"}, 404)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch Automated Workflow")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    load_env_file()
    ensure_storage()
    start_scheduler()
    server = ReusableThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Automated Workflow is running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
