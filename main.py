from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import signal
import sys
import threading
import tempfile
import time
import uuid
import base64
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "sui.db"
GENERATED_DIR = DATA_DIR / "generated"
FRONTEND_DIST = ROOT / "frontend" / "dist"
SCHEMA_VERSION = 1
SUPPORTED_STEP_TYPES = {
    "navigate_to_website",
    "navigate",
    "click",
    "fill",
    "delay",
    "keypress",
    "assign",
    "scrape",
    "pattern_matcher",
    "smartie",
    "message_box",
    "check_element_exists",
    "check_text_exists",
    "if",
    "for",
    "use_js",
    "writer",
    "call_recipe",
    "invoke_recipe",
}
WRITER_COLUMN_TYPES = {"TEXT", "INTEGER", "REAL", "BLOB", "BOOLEAN", "DATE", "DATETIME"}
INTERNAL_SQLITE_TABLES = {"projects", "recipes", "recipe_versions", "jobs", "job_logs"}
KEYPRESS_KEYS = {
    "Enter", "Escape", "Tab", "Backspace", "Delete", "Space",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End",
    "PageUp", "PageDown", "Insert", "Control", "Shift", "Alt", "Meta",
    *(f"F{index}" for index in range(1, 13)),
}


def load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or name in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[name] = value


load_env_file()
STEP_RUNS: dict[str, dict[str, Any]] = {}
STEP_RUNS_LOCK = threading.Lock()
STEP_RUNS_CHANGED = threading.Condition(STEP_RUNS_LOCK)
STEP_SESSION_START_LOCK = threading.Lock()
JOB_RUNS: dict[str, dict[str, Any]] = {}
JOB_RUNS_LOCK = threading.Lock()
JOB_RUNS_CHANGED = threading.Condition(JOB_RUNS_LOCK)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    with db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recipes (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                current_version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, name)
            );
            CREATE TABLE IF NOT EXISTS recipe_versions (
                recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                definition_json TEXT NOT NULL,
                generated_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(recipe_id, version)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                recipe_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT NOT NULL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        value = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object")
    return value


def validate_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("Navigate to website requires a URL beginning with http:// or https://")
    return url


def validate_selector(value: Any) -> str:
    selector = str(value or "").strip()
    if not selector:
        raise ValueError("Click requires a selector. Pick an element in the live browser first")
    if selector.lower().startswith("javascript:"):
        raise ValueError("JavaScript selectors are not supported")
    return selector


def validate_delay(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Delay requires a number of seconds") from exc
    if seconds < 0.1 or seconds > 3600:
        raise ValueError("Delay must be between 0.1 and 3600 seconds")
    return round(seconds, 3)


def validate_wait_timeout(value: Any) -> float:
    try:
        timeout = float(value if value is not None else 15)
    except (TypeError, ValueError) as exc:
        raise ValueError("Post-action wait timeout must be a number of seconds") from exc
    if timeout < 0.1 or timeout > 120:
        raise ValueError("Post-action wait timeout must be between 0.1 and 120 seconds")
    return round(timeout, 3)


def normalize_post_action_wait(settings: dict[str, Any]) -> dict[str, Any]:
    expected_url = str(settings.get("expected_url") or "").strip()
    destination_selector = str(settings.get("destination_selector") or "").strip()
    if destination_selector:
        destination_selector = validate_selector(destination_selector)
    return {
        "expected_url": expected_url,
        "destination_selector": destination_selector,
        "wait_timeout_seconds": validate_wait_timeout(settings.get("wait_timeout_seconds", 15)),
    }


def validate_until_load_completes(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def validate_step_disabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def validate_keypress(value: Any) -> str:
    key = str(value or "Enter").strip()
    if key == "Esc":
        key = "Escape"
    if key not in KEYPRESS_KEYS and len(key) != 1:
        raise ValueError("Unsupported keypress key")
    return key


def validate_navigation_action(value: Any) -> str:
    action = str(value or "back").strip().lower()
    if action not in {"back", "forward", "refresh"}:
        raise ValueError("Navigate action must be Back, Forward, or Refresh")
    return action


def validate_recipe_type(value: Any) -> str:
    recipe_type = str(value or "standard").strip().lower()
    if recipe_type not in {"standard", "orchest"}:
        raise ValueError("Recipe type must be standard or orchest")
    return recipe_type


def validate_condition_operator(value: Any) -> str:
    operator = str(value or "=").strip()
    if operator not in {"=", "<>", "<", "<=", ">", ">=", "in"}:
        raise ValueError("Unsupported condition operator")
    return operator


def validate_variable_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
        raise ValueError("Variable names must start with a letter or underscore and contain only letters, numbers, and underscores")
    return name


def validate_table_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or "\x00" in name or len(name) > 255:
        raise ValueError("Writer requires a non-empty table name")
    return name


def validate_writer_column_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or "\x00" in name or len(name) > 255:
        raise ValueError("Writer column names must be non-empty")
    return name


def validate_writer_column_type(value: Any) -> str:
    column_type = str(value or "TEXT").strip().upper()
    if column_type not in WRITER_COLUMN_TYPES:
        raise ValueError(f"Unsupported Writer column type: {column_type}")
    return column_type


def normalize_existing_sqlite_type(value: Any) -> str:
    raw_type = str(value or "TEXT").strip().upper()
    if raw_type in WRITER_COLUMN_TYPES:
        return raw_type
    if "INT" in raw_type:
        return "INTEGER"
    if any(token in raw_type for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in raw_type or not raw_type:
        return "BLOB" if "BLOB" in raw_type else "TEXT"
    if any(token in raw_type for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "REAL"


def normalize_writer_columns(settings: dict[str, Any]) -> list[dict[str, Any]]:
    raw_columns = settings.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raw_columns = [{"name": "value", "type": "TEXT", "value": settings.get("value", "")}]
    columns: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw_column in raw_columns:
        if not isinstance(raw_column, dict):
            raise ValueError("Writer columns must be objects")
        name = validate_writer_column_name(raw_column.get("name"))
        name_key = name.casefold()
        if name_key in names:
            raise ValueError(f"Writer column {name} is duplicated")
        names.add(name_key)
        columns.append({
            "name": name,
            "type": validate_writer_column_type(raw_column.get("type", "TEXT")),
            "value": str(raw_column.get("value") or ""),
            "write": bool(raw_column.get("write", True)),
        })
    return columns


def normalize_writer_duplicate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(settings.get("semantic_duplicate_removal", False))
    raw_threshold = settings.get("duplicate_threshold", 0.88)
    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError("Writer duplicate threshold must be a number") from exc
    if not 0.5 <= threshold <= 1.0:
        raise ValueError("Writer duplicate threshold must be between 0.5 and 1")
    return {
        "semantic_duplicate_removal": enabled,
        "duplicate_threshold": threshold,
    }


SMARTIE_OUTPUT_TYPES = {"text", "number", "json"}
SMARTIE_FIELD_TYPES = {"string", "number", "boolean", "json"}
SMARTIE_MODELS = {"gemini-2.5-flash"}


def normalize_smartie_schema(settings: dict[str, Any]) -> dict[str, Any]:
    model = str(settings.get("model") or "gemini-2.5-flash").strip()
    # Gemini 2.0 Flash has been retired by the API. Migrate saved recipes
    # created before the model was retired when they are normalized again.
    if model == "gemini-2.0-flash":
        model = "gemini-2.5-flash"
    if model not in SMARTIE_MODELS:
        raise ValueError("Unsupported Smartie Gemini model")
    output_type = str(settings.get("output_type") or "text").strip().lower()
    if output_type not in SMARTIE_OUTPUT_TYPES:
        raise ValueError("Smartie output must be text, number, or json")
    output_variable = validate_variable_name(settings.get("output_variable"))
    system_prompt = str(settings.get("system_prompt") or "")
    user_prompt = str(settings.get("user_prompt") or "")
    if not user_prompt.strip():
        raise ValueError("Smartie requires a user prompt")
    raw_fields = settings.get("schema") or []
    if output_type != "json":
        raw_fields = []
    if not isinstance(raw_fields, list):
        raise ValueError("Smartie JSON schema must be an array")
    fields: list[dict[str, Any]] = []
    field_names: set[str] = set()
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            raise ValueError("Smartie JSON schema fields must be objects")
        name = str(raw_field.get("name") or "").strip()
        if not name or len(name) > 100:
            raise ValueError("Smartie JSON schema field names must be non-empty")
        name_key = name.casefold()
        if name_key in field_names:
            raise ValueError(f"Smartie JSON schema field {name} is duplicated")
        field_names.add(name_key)
        field_type = str(raw_field.get("type") or "string").strip().lower()
        if field_type not in SMARTIE_FIELD_TYPES:
            raise ValueError("Smartie JSON schema field types must be string, number, boolean, or json")
        fields.append({"name": name, "type": field_type, "required": bool(raw_field.get("required", True))})
    if output_type == "json" and not fields:
        raise ValueError("Smartie JSON output requires at least one schema field")
    return {
        "provider": "google_gemini",
        "model": model,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "output_type": output_type,
        "output_variable": output_variable,
        "schema": fields,
    }


def quote_sqlite_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def writer_tables() -> list[str]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    return [str(row["name"]) for row in rows if str(row["name"]) not in INTERNAL_SQLITE_TABLES]


def viewer_tables() -> list[str]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    return [str(row["name"]) for row in rows if str(row["name"]) not in INTERNAL_SQLITE_TABLES]


def viewer_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"encoding": "base64", "value": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def viewer_table_data(table_name: str, limit: Any = 200) -> dict[str, Any]:
    name = validate_table_name(table_name)
    try:
        row_limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError) as exc:
        raise ValueError("Viewer row limit must be an integer") from exc
    connection = db_connection()
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        if not exists:
            raise ValueError("Viewer table not found")
        schema_rows = connection.execute(f"PRAGMA table_info({quote_sqlite_identifier(name)})").fetchall()
        rows = connection.execute(
            f"SELECT rowid AS __sui_rowid, * FROM {quote_sqlite_identifier(name)} LIMIT ?", (row_limit,)
        ).fetchall()
    finally:
        connection.close()
    columns = [
        {"name": str(row["name"]), "type": normalize_existing_sqlite_type(row["type"])}
        for row in schema_rows
    ]
    return {
        "table": name,
        "columns": columns,
        "rows": [{str(key): viewer_value(row[key]) for key in row.keys()} for row in rows],
        "limit": row_limit,
    }


def data_table_row(table_name: str, operation: str, raw_values: Any = None, row_id: Any = None, column_name: Any = None, value: Any = None) -> dict[str, Any]:
    name = validate_table_name(table_name)
    if name in INTERNAL_SQLITE_TABLES or name.startswith("sqlite_"):
        raise ValueError("That table is reserved by Sui")
    connection = db_connection()
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        if not exists:
            raise ValueError("Data table not found")
        schema_rows = connection.execute(f"PRAGMA table_info({quote_sqlite_identifier(name)})").fetchall()
        schema_names = {str(row["name"]): str(row["name"]) for row in schema_rows}
        quoted_table = quote_sqlite_identifier(name)
        if operation == "insert":
            if not isinstance(raw_values, dict):
                raise ValueError("A row must be an object")
            values = [(schema_names[key], raw_values[key]) for key in raw_values if key in schema_names]
            if not values:
                raise ValueError("Add a value for at least one column")
            quoted_columns = ", ".join(quote_sqlite_identifier(key) for key, _ in values)
            placeholders = ", ".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES ({placeholders})",
                tuple(item for _, item in values),
            )
        elif operation == "update":
            try:
                numeric_row_id = int(row_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("A valid row is required") from exc
            column = str(column_name or "")
            if column not in schema_names:
                raise ValueError("Column not found")
            result = connection.execute(
                f"UPDATE {quoted_table} SET {quote_sqlite_identifier(column)} = ? WHERE rowid = ?",
                (value, numeric_row_id),
            )
            if result.rowcount != 1:
                raise ValueError("Row not found")
        elif operation == "delete":
            try:
                numeric_row_id = int(row_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("A valid row is required") from exc
            result = connection.execute(
                f"DELETE FROM {quoted_table} WHERE rowid = ?",
                (numeric_row_id,),
            )
            if result.rowcount != 1:
                raise ValueError("Row not found")
        else:
            raise ValueError("Unsupported data operation")
        connection.commit()
    finally:
        connection.close()
    return viewer_table_data(name, 500)


def craft_table(table_name: str, raw_columns: Any) -> dict[str, Any]:
    name = validate_table_name(table_name)
    if name in INTERNAL_SQLITE_TABLES or name.startswith("sqlite_"):
        raise ValueError("That table is reserved by Sui")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ValueError("Craft requires at least one column")
    columns: list[tuple[str, str, str | None]] = []
    names: set[str] = set()
    for raw_column in raw_columns:
        if not isinstance(raw_column, dict):
            raise ValueError("Craft columns must be objects")
        column_name = validate_writer_column_name(raw_column.get("name"))
        if column_name.casefold() in names:
            raise ValueError(f"Column {column_name} is duplicated")
        names.add(column_name.casefold())
        original_name = str(raw_column.get("original_name") or "").strip() or None
        if original_name:
            original_name = validate_writer_column_name(original_name)
        columns.append((column_name, validate_writer_column_type(raw_column.get("type", "TEXT")), original_name))
    quoted_table = quote_sqlite_identifier(name)
    connection = db_connection()
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        created = not bool(exists)
        if created:
            definitions = ", ".join(f"{quote_sqlite_identifier(column_name)} {column_type}" for column_name, column_type, _ in columns)
            connection.execute(f"CREATE TABLE {quoted_table} ({definitions})")
        else:
            schema_rows = connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
            existing = {str(row["name"]).casefold(): row for row in schema_rows}
            for column_name, _, original_name in columns:
                if original_name and original_name.casefold() in existing and column_name.casefold() not in existing and original_name.casefold() != column_name.casefold():
                    connection.execute(f"ALTER TABLE {quoted_table} RENAME COLUMN {quote_sqlite_identifier(original_name)} TO {quote_sqlite_identifier(column_name)}")
                    existing[column_name.casefold()] = existing.pop(original_name.casefold())
            desired_names = {column_name.casefold() for column_name, _, _ in columns}
            current_names = set(existing)
            type_changes = any(
                column_name.casefold() in existing
                and normalize_existing_sqlite_type(existing[column_name.casefold()]["type"]) != column_type
                for column_name, column_type, _ in columns
            )
            schema_changed = desired_names != current_names or type_changes
            if schema_changed and not any(bool(row["pk"]) for row in existing.values()):
                temporary_name = f"__sui_craft_{uuid.uuid4().hex}"
                temporary_table = quote_sqlite_identifier(temporary_name)
                definitions = ", ".join(f"{quote_sqlite_identifier(column_name)} {column_type}" for column_name, column_type, _ in columns)
                connection.execute(f"CREATE TABLE {temporary_table} ({definitions})")
                common = [(column_name, column_type) for column_name, column_type, _ in columns if column_name.casefold() in existing]
                if common:
                    names = ", ".join(quote_sqlite_identifier(column_name) for column_name, _ in common)
                    casts = ", ".join(f"CAST({quote_sqlite_identifier(column_name)} AS {column_type})" for column_name, column_type in common)
                    connection.execute(f"INSERT INTO {temporary_table} ({names}) SELECT {casts} FROM {quoted_table}")
                connection.execute(f"DROP TABLE {quoted_table}")
                connection.execute(f"ALTER TABLE {temporary_table} RENAME TO {quote_sqlite_identifier(name)}")
                existing = {column_name.casefold(): {"name": column_name, "type": column_type, "pk": 0} for column_name, column_type, _ in columns}
            elif schema_changed and any(bool(row["pk"]) for row in existing.values()):
                raise ValueError("Craft cannot remove or change columns on a table with a primary key")
            for column_name, column_type, _ in columns:
                if column_name.casefold() not in existing:
                    connection.execute(f"ALTER TABLE {quoted_table} ADD COLUMN {quote_sqlite_identifier(column_name)} {column_type}")
        connection.commit()
    finally:
        connection.close()
    return {"table": name, "columns": writer_table_schema(name), "created": created}


def delete_data_table(table_name: str) -> str:
    name = validate_table_name(table_name)
    if name in INTERNAL_SQLITE_TABLES or name.startswith("sqlite_"):
        raise ValueError("That table is reserved by Sui")
    connection = db_connection()
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        if not exists:
            raise ValueError("Data table not found")
        connection.execute(f"DROP TABLE {quote_sqlite_identifier(name)}")
        connection.commit()
    finally:
        connection.close()
    return name


def writer_table_schema(table_name: str) -> list[dict[str, Any]]:
    name = validate_table_name(table_name)
    connection = db_connection()
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        if not exists:
            raise ValueError("Writer table not found")
        rows = connection.execute(f"PRAGMA table_info({quote_sqlite_identifier(name)})").fetchall()
    finally:
        connection.close()
    return [
        {
            "name": str(row["name"]),
            "type": normalize_existing_sqlite_type(row["type"]),
            "write": not bool(row["pk"]),
            "primary_key": bool(row["pk"]),
            "not_null": bool(row["notnull"]),
            "default": row["dflt_value"],
        }
        for row in rows
    ]


def normalize_variable_reference(value: Any) -> str:
    reference = str(value or "").strip()
    if reference.startswith("{{") and reference.endswith("}}"):
        reference = reference[2:-2].strip()
    elif reference.startswith("{") and reference.endswith("}"):
        reference = reference[1:-1].strip()
    return validate_variable_name(reference)


def normalize_loop_iterator(value: Any) -> tuple[str, int | None]:
    raw_iterator = str(value or "i").strip()
    if "=" not in raw_iterator:
        return normalize_variable_reference(raw_iterator), None
    iterator_reference, start_text = raw_iterator.split("=", 1)
    try:
        start_value = int(start_text.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("For loop iterator start must be an integer, such as i=1") from exc
    if start_value < -100000 or start_value > 100000:
        raise ValueError("For loop iterator start must be between -100000 and 100000")
    return normalize_variable_reference(iterator_reference), start_value


def validate_loop_range(value: Any, variable_names: set[str]) -> str:
    range_value = str(value or "").strip()
    if not range_value:
        raise ValueError("For loop requires a hardcoded integer or a recipe variable such as {count}")
    expression = re.fullmatch(r"(.+?)(?:\s*([+-])\s*(\d+))?", range_value)
    if not expression:
        raise ValueError("For loop range must be a number, variable, or variable plus/minus an integer")
    base = expression.group(1).strip()
    offset = int(expression.group(3) or 0) * (-1 if expression.group(2) == "-" else 1)
    if base.isdigit():
        count = int(base) + offset
    else:
        variable_name = normalize_variable_reference(base) if base.startswith("{") else validate_variable_name(base)
        if variable_name not in variable_names:
            raise ValueError(f"For loop range refers to unknown variable {variable_name}")
        return range_value
    if count < 0 or count > 100000:
        raise ValueError("For loop range must be between 0 and 100000")
    return range_value


def normalize_definition(
    definition: dict[str, Any],
    project_id: str,
    recipe_id: str,
    recipe_name: str,
    version: int,
    require_first_navigation: bool = True,
) -> dict[str, Any]:
    raw_steps = definition.get("steps", [])
    if not isinstance(raw_steps, list):
        raise ValueError("Recipe steps must be an array")
    recipe_type = validate_recipe_type(definition.get("recipe_type", "standard"))
    if require_first_navigation and recipe_type != "orchest" and raw_steps:
        first_step = raw_steps[0]
        if not isinstance(first_step, dict) or str(first_step.get("type") or "") != "navigate_to_website":
            raise ValueError("The first recipe step must be Navigate to website")
    raw_variables = definition.get("variables", [])
    if not isinstance(raw_variables, list):
        raise ValueError("Recipe variables must be an array")
    variables: list[dict[str, str]] = []
    variable_names: set[str] = set()
    for raw_variable in raw_variables:
        if not isinstance(raw_variable, dict):
            raise ValueError("Every recipe variable must be an object")
        variable_name = validate_variable_name(raw_variable.get("name"))
        if variable_name in variable_names:
            raise ValueError(f"Variable {variable_name} is already defined")
        variable_names.add(variable_name)
        variables.append({
            "id": str(raw_variable.get("id") or f"variable_{uuid.uuid4().hex[:12]}"),
            "name": variable_name,
            "value": str(raw_variable.get("value") or ""),
        })
    def normalize_steps(
        raw_items: Any,
        branch_label: str = "steps",
        scoped_variable_names: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_items, list):
            raise ValueError(f"Recipe {branch_label} must be an array")
        available_variable_names = set(variable_names if scoped_variable_names is None else scoped_variable_names)
        normalized_steps: list[dict[str, Any]] = []
        for index, raw_step in enumerate(raw_items):
            if not isinstance(raw_step, dict):
                raise ValueError("Every recipe step must be an object")
            step_type = str(raw_step.get("type") or "")
            if step_type not in SUPPORTED_STEP_TYPES:
                raise ValueError("Unsupported recipe activity")
            settings = raw_step.get("settings") or {}
            position = raw_step.get("position") or {}
            if step_type == "navigate_to_website":
                normalized_settings = {"url": validate_url(settings.get("url"))}
            elif step_type == "navigate":
                normalized_settings = {"action": validate_navigation_action(settings.get("action", "back"))}
            elif step_type == "click":
                normalized_settings = {
                    "selector": validate_selector(settings.get("selector")),
                    **normalize_post_action_wait(settings),
                }
            elif step_type == "fill":
                normalized_settings = {
                    "selector": validate_selector(settings.get("selector")),
                    "value": str(settings.get("value") or ""),
                }
            elif step_type == "delay":
                normalized_settings = {
                    "seconds": validate_delay(settings.get("seconds", 1)),
                    "until_load_completes": validate_until_load_completes(settings.get("until_load_completes", False)),
                }
            elif step_type == "keypress":
                normalized_settings = {
                    "key": validate_keypress(settings.get("key", "Enter")),
                    **normalize_post_action_wait(settings),
                }
            elif step_type == "assign":
                variable_name = validate_variable_name(settings.get("variable"))
                if variable_name not in variable_names:
                    raise ValueError(f"Assign refers to unknown variable {variable_name}")
                normalized_settings = {
                    "variable": variable_name,
                    "value": str(settings.get("value") or ""),
                }
            elif step_type == "scrape":
                variable_name = validate_variable_name(settings.get("variable"))
                if variable_name not in variable_names:
                    raise ValueError(f"Scrape refers to unknown variable {variable_name}")
                normalized_settings = {
                    "selector": validate_selector(settings.get("selector")),
                    "variable": variable_name,
                }
            elif step_type == "pattern_matcher":
                variable_name = validate_variable_name(settings.get("variable"))
                if variable_name not in variable_names:
                    raise ValueError(f"Pattern Matcher refers to unknown variable {variable_name}")
                raw_patterns = settings.get("patterns") or []
                if not isinstance(raw_patterns, list):
                    raise ValueError("Pattern Matcher patterns must be an array")
                patterns: list[str] = []
                for raw_pattern in raw_patterns:
                    pattern = str(raw_pattern or "").strip()
                    if not pattern:
                        continue
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        raise ValueError(f"Invalid Pattern Matcher regex: {exc}") from exc
                    patterns.append(pattern)
                if not patterns:
                    raise ValueError("Pattern Matcher requires at least one regex pattern")
                normalized_settings = {
                    "patterns": patterns,
                    "variable": variable_name,
                }
            elif step_type == "smartie":
                normalized_settings = normalize_smartie_schema(settings)
                if normalized_settings["output_variable"] not in available_variable_names:
                    raise ValueError(f"Smartie output refers to unknown variable {normalized_settings['output_variable']}")
            elif step_type == "message_box":
                normalized_settings = {"message": str(settings.get("message") or "")}
            elif step_type == "check_element_exists":
                normalized_settings = {
                    "selector": validate_selector(settings.get("selector")),
                    "timeout_seconds": validate_delay(settings.get("timeout_seconds", 5)),
                }
            elif step_type == "check_text_exists":
                text = str(settings.get("text") or "").strip()
                if not text:
                    raise ValueError("Check text exists requires text or {{variable}}")
                normalized_settings = {
                    "text": text,
                    "case_insensitive": bool(settings.get("case_insensitive", True)),
                    "timeout_seconds": validate_delay(settings.get("timeout_seconds", 5)),
                }
            elif step_type == "if":
                normalized_settings = {
                    "left": str(settings.get("left") or ""),
                    "operator": validate_condition_operator(settings.get("operator", "=")),
                    "right": str(settings.get("right") or ""),
                }
            elif step_type == "for":
                source = str(settings.get("source") or "range").strip().lower()
                if source == "table":
                    table_mode = str(settings.get("table_mode") or "row").strip().lower()
                    if table_mode not in {"row", "column"}:
                        raise ValueError("For loop table mode must be row or column")
                    default_iterator = "row" if table_mode == "row" else str(settings.get("column_name") or "value")
                    iterator = validate_variable_name(settings.get("iterator") or default_iterator)
                    table_name = validate_table_name(settings.get("table_name"))
                    normalized_settings = {
                        "source": "table",
                        "table_mode": table_mode,
                        "table_name": table_name,
                        "iterator": iterator,
                        "column_name": validate_writer_column_name(settings.get("column_name") or iterator) if table_mode == "column" else "",
                        "current_row": "currentRow",
                    }
                else:
                    iterator, inline_start = normalize_loop_iterator(settings.get("iterator", "i"))
                    start_value = inline_start if inline_start is not None else int(settings.get("start_value", 0) or 0)
                    if start_value < -100000 or start_value > 100000:
                        raise ValueError("For loop iterator start must be between -100000 and 100000")
                    normalized_settings = {
                        "iterator": iterator,
                        "start_value": start_value,
                        "range_value": validate_loop_range(settings.get("range_value", ""), available_variable_names),
                    }
            elif step_type == "use_js":
                code = str(settings.get("code") or "").strip()
                if not code:
                    raise ValueError("Use JS requires a function body")
                raw_inputs = settings.get("inputs") or []
                if not isinstance(raw_inputs, list):
                    raise ValueError("Use JS inputs must be an array of variables")
                inputs = [normalize_variable_reference(item) for item in raw_inputs if str(item or "").strip()]
                if any(item not in available_variable_names for item in inputs):
                    raise ValueError("Use JS inputs must reference existing variables")
                output_reference = str(settings.get("output") or "").strip()
                output = normalize_variable_reference(output_reference) if output_reference else ""
                if output and output not in variable_names:
                    raise ValueError(f"Use JS output refers to unknown variable {output}")
                normalized_settings = {
                    "code": code,
                    "inputs": inputs,
                    "output": output,
                    **normalize_post_action_wait(settings),
                }
            elif step_type == "writer":
                columns = normalize_writer_columns(settings)
                current_row_reference = str(settings.get("current_row") or "").strip()
                if current_row_reference:
                    current_row_reference = normalize_variable_reference(current_row_reference)
                    if current_row_reference != "currentRow":
                        raise ValueError("Writer current row must use currentRow")
                normalized_settings = {
                    "value": str(settings.get("value") or ""),
                    "table_name": validate_table_name(settings.get("table_name")),
                    "columns": columns,
                    "current_row": current_row_reference,
                    **normalize_writer_duplicate_settings(settings),
                }
            elif step_type in {"call_recipe", "invoke_recipe"}:
                if recipe_type != "orchest":
                    raise ValueError("Run recipe and Invoke are available only for orchest recipes")
                target_recipe_id = str(settings.get("recipe_id") or "").strip()
                target_recipe_name = str(settings.get("recipe_name") or "").strip()
                if not target_recipe_id and not target_recipe_name:
                    raise ValueError("Invoke requires a target recipe")
                if target_recipe_id == recipe_id:
                    raise ValueError("A recipe cannot run itself")
                normalized_settings = {
                    "recipe_id": target_recipe_id,
                    "recipe_name": target_recipe_name,
                }
            else:
                normalized_settings = {"selector": validate_selector(settings.get("selector"))}

            branches: dict[str, list[dict[str, Any]]] = {}
            raw_branches = raw_step.get("branches") or {}
            if step_type in {"check_element_exists", "check_text_exists"}:
                branches = {
                    "exists": normalize_steps(raw_branches.get("exists", []), "exists branch", available_variable_names),
                    "not_exists": normalize_steps(raw_branches.get("not_exists", []), "not exists branch", available_variable_names),
                }
            elif step_type == "if":
                branches = {
                    "then": normalize_steps(raw_branches.get("then", []), "then branch", available_variable_names),
                    "else": normalize_steps(raw_branches.get("else", []), "else branch", available_variable_names),
                }
            elif step_type == "for":
                loop_variable_names = set(available_variable_names)
                loop_variable_names.add(normalized_settings["iterator"])
                if normalized_settings.get("source") == "table":
                    loop_variable_names.add("currentRow")
                branches = {
                    "body": normalize_steps(raw_branches.get("body", []), "for loop body", loop_variable_names),
                }
            normalized_steps.append(
                {
                    "id": str(raw_step.get("id") or f"step_{uuid.uuid4().hex[:12]}"),
                    "type": step_type,
                    "disabled": validate_step_disabled(raw_step.get("disabled", False)),
                    "settings": normalized_settings,
                    "connections": raw_step.get("connections") or {"next": []},
                    "position": {
                        "x": int(position.get("x", 48 + index * 220)),
                        "y": int(position.get("y", 48)),
                    },
                    "branches": branches,
                }
            )
        return normalized_steps

    steps = normalize_steps(raw_steps, "steps", variable_names)
    """
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise ValueError("Every recipe step must be an object")
        step_type = str(raw_step.get("type") or "")
        if step_type not in SUPPORTED_STEP_TYPES:
            raise ValueError("Only Navigate to website, Click, Fill, Delay, Assign, Scrape, and Message box are supported")
        settings = raw_step.get("settings") or {}
        position = raw_step.get("position") or {}
        if step_type == "navigate_to_website":
            normalized_settings = {"url": validate_url(settings.get("url"))}
        elif step_type == "fill":
            normalized_settings = {
                "selector": validate_selector(settings.get("selector")),
                "value": str(settings.get("value") or ""),
            }
        elif step_type == "delay":
            normalized_settings = {"seconds": validate_delay(settings.get("seconds", 1))}
        elif step_type == "assign":
            variable_name = validate_variable_name(settings.get("variable"))
            if variable_name not in variable_names:
                raise ValueError(f"Assign refers to unknown variable {variable_name}")
            normalized_settings = {
                "variable": variable_name,
                "value": str(settings.get("value") or ""),
            }
        elif step_type == "scrape":
            variable_name = validate_variable_name(settings.get("variable"))
            if variable_name not in variable_names:
                raise ValueError(f"Scrape refers to unknown variable {variable_name}")
            normalized_settings = {
                "selector": validate_selector(settings.get("selector")),
                "variable": variable_name,
            }
        elif step_type == "message_box":
            normalized_settings = {"message": str(settings.get("message") or "")}
        else:
            normalized_settings = {"selector": validate_selector(settings.get("selector"))}
        steps.append(
            {
                "id": str(raw_step.get("id") or f"step_{uuid.uuid4().hex[:12]}"),
                "type": step_type,
                "settings": normalized_settings,
                "connections": raw_step.get("connections") or {"next": []},
                "position": {
                    "x": int(position.get("x", 48 + index * 220)),
                    "y": int(position.get("y", 48)),
                },
            }
        )
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "recipe_id": recipe_id,
        "recipe_name": recipe_name,
        "recipe_type": recipe_type,
        "version": version,
        "steps": steps,
        "connections": definition.get("connections") or [],
        "variables": variables,
        "mode": "headless" if definition.get("mode") == "headless" else "headful",
    }


def _generated_window_support(lines: list[str]) -> None:
    lines.extend(
        [
            "def preserve_foreground_window():",
            "    if os.name != \"nt\": return None",
            "    return ctypes.windll.user32.GetForegroundWindow()",
            "",
            "",
            "def restore_foreground_window(hwnd):",
            "    if hwnd and os.name == \"nt\": ctypes.windll.user32.SetForegroundWindow(hwnd)",
            "",
            "",
            "def visible_window_handles():",
            "    if os.name != \"nt\": return set()",
            "    user32 = ctypes.windll.user32",
            "    handles = set()",
            "    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)",
            "    def visit(hwnd, _):",
            "        if user32.IsWindowVisible(hwnd): handles.add(int(hwnd))",
            "        return True",
            "    callback_ref = callback_type(visit)",
            "    user32.EnumWindows(callback_ref, 0)",
            "    return handles",
            "",
            "",
            "def is_chrome_window(hwnd):",
            "    user32 = ctypes.windll.user32",
            "    title = ctypes.create_unicode_buffer(512)",
            "    class_name = ctypes.create_unicode_buffer(256)",
            "    user32.GetWindowTextW(hwnd, title, 512)",
            "    user32.GetClassNameW(hwnd, class_name, 256)",
            "    return \"Chrome_WidgetWin\" in class_name.value or \"Google Chrome\" in title.value",
            "",
            "",
            "def maximize_browser_window(before_handles):",
            "    if os.name != \"nt\": return None",
            "    user32 = ctypes.windll.user32",
            "    candidates = []",
            "    for _ in range(20):",
            "        candidates = [hwnd for hwnd in visible_window_handles() - before_handles if is_chrome_window(hwnd)]",
            "        if candidates: break",
            "        time.sleep(0.05)",
            "    foreground = user32.GetForegroundWindow()",
            "    if not candidates and foreground and is_chrome_window(foreground): candidates = [foreground]",
            "    if not candidates: return None",
            "    hwnd = candidates[-1]",
            "    user32.ShowWindow(hwnd, 3)",
            "    return hwnd",
        ]
    )


def _generated_smartie_support(lines: list[str]) -> None:
    lines.extend(
        [
            "def smartie_env_value(name):",
            "    value = os.environ.get(name, '').strip()",
            "    if value: return value",
            "    env_path = os.path.join(os.path.dirname(os.path.abspath(WRITER_DB_PATH)), '..', '.env')",
            "    try:",
            "        with open(env_path, 'r', encoding='utf-8') as handle:",
            "            for raw_line in handle:",
            "                line = raw_line.strip()",
            "                if not line or line.startswith('#') or '=' not in line: continue",
            "                key, candidate = line.split('=', 1)",
            "                if key.strip() == name:",
            "                    candidate = candidate.strip()",
            "                    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in chr(39) + chr(34): candidate = candidate[1:-1]",
            "                    return candidate",
            "    except OSError: pass",
            "    return ''",
            "",
            "",
            "def smartie_response_text(payload):",
            "    candidates = payload.get('candidates') or []",
            "    if not candidates: raise RuntimeError('Gemini returned no candidates')",
            "    content = candidates[0].get('content') or {}",
            "    parts = content.get('parts') or []",
            "    text = ''.join(str(part.get('text') or '') for part in parts).strip()",
            "    if not text: raise RuntimeError('Gemini returned an empty response')",
            "    return text",
            "",
            "",
            "def smartie_json_schema(fields):",
            "    type_map = {'string': 'STRING', 'number': 'NUMBER', 'boolean': 'BOOLEAN', 'json': 'OBJECT'}",
            "    properties = {}",
            "    required = []",
            "    for field in fields or []:",
            "        field_name = str(field.get('name') or '')",
            "        properties[field_name] = {'type': type_map.get(str(field.get('type') or 'string'), 'STRING')}",
            "        if field.get('required', True): required.append(field_name)",
            "    return {'type': 'OBJECT', 'properties': properties, 'required': required}",
            "",
            "",
            "def run_smartie(model, system_prompt, user_prompt, output_type, output_schema):",
            "    api_key = smartie_env_value('GOOGLE_GEMINI_KEY')",
            "    if not api_key: raise RuntimeError('Smartie requires GOOGLE_GEMINI_KEY in .env')",
            "    model = str(model or 'gemini-2.5-flash')",
            "    if model == 'gemini-2.0-flash': model = 'gemini-2.5-flash'",
            "    endpoint = 'https://generativelanguage.googleapis.com/v1beta/models/' + quote(model, safe='') + ':generateContent'",
            "    request_body = {'contents': [{'role': 'user', 'parts': [{'text': str(user_prompt)}]}]}",
            "    if str(system_prompt or '').strip(): request_body['systemInstruction'] = {'parts': [{'text': str(system_prompt)}]}",
            "    if output_type == 'json': request_body['generationConfig'] = {'responseMimeType': 'application/json', 'responseSchema': smartie_json_schema(output_schema)}",
            "    request = urllib.request.Request(endpoint, data=json.dumps(request_body, ensure_ascii=False).encode('utf-8'), headers={'Content-Type': 'application/json', 'x-goog-api-key': api_key}, method='POST')",
            "    try:",
            "        with urllib.request.urlopen(request, timeout=120) as response: payload = json.loads(response.read().decode('utf-8'))",
            "    except urllib.error.HTTPError as exc:",
            "        detail = exc.read().decode('utf-8', errors='replace')",
            "        try:",
            "            error_payload = json.loads(detail).get('error') or {}",
            "            error_message = str(error_payload.get('message') or detail).replace(chr(10), ' ').replace(chr(13), ' ').strip()",
            "            error_reason = str(error_payload.get('status') or error_payload.get('details', [{}])[0].get('reason') or '').strip()",
            "            if error_reason: error_message += ' [' + error_reason + ']'",
            "        except (TypeError, ValueError, json.JSONDecodeError): error_message = detail.replace(chr(10), ' ').replace(chr(13), ' ').strip()",
            "        raise RuntimeError('Gemini request failed (' + str(exc.code) + '): ' + error_message[:1000]) from exc",
            "    text = smartie_response_text(payload)",
            "    if output_type == 'number':",
            "        candidate = text.strip().replace(',', '')",
            "        try: return int(candidate) if re.fullmatch(r'[-+]?\\d+', candidate) else float(candidate)",
            "        except ValueError:",
            "            match = re.search(r'[-+]?(?:\\d+\\.\\d+|\\d+)', candidate)",
            "            if not match: raise RuntimeError('Smartie expected a number but Gemini returned: ' + text[:300])",
            "            return float(match.group(0))",
            "    if output_type == 'json':",
            "        cleaned = text.strip()",
            "        if cleaned.startswith('```'): cleaned = re.sub(r'^```(?:json)?\\s*|\\s*```$', '', cleaned, flags=re.IGNORECASE | re.DOTALL).strip()",
            "        try: parsed = json.loads(cleaned)",
            "        except json.JSONDecodeError as exc: raise RuntimeError('Smartie returned invalid JSON: ' + str(exc)) from exc",
            "        if not isinstance(parsed, dict): raise RuntimeError('Smartie JSON output must be an object')",
            "        missing = [str(field.get('name')) for field in output_schema if field.get('required', True) and str(field.get('name')) not in parsed]",
            "        if missing: raise RuntimeError('Smartie JSON output is missing fields: ' + ', '.join(missing))",
            "        return json.dumps(parsed, ensure_ascii=False, separators=(',', ':'))",
            "    return text",
            "",
            "",
        ]
    )


def _generate_recursive_sync_python(definition: dict[str, Any]) -> str:
    mode = definition.get("mode") or "headful"
    project_id_literal = json.dumps(str(definition.get("project_id") or ""))
    recipe_id_literal = json.dumps(str(definition.get("recipe_id") or ""))
    steps_literal = json.dumps(json.dumps(definition.get("steps", [])))
    variables_literal = json.dumps(json.dumps({item["name"]: item.get("value", "") for item in definition.get("variables", [])}))
    writer_db_literal = json.dumps(str(DB_PATH))
    lines = [
        "import base64",
        "import ast",
        "import ctypes",
        "import difflib",
        "import json",
        "import os",
        "import re",
        "import sqlite3",
        "import time",
        "import urllib.error",
        "import urllib.request",
        "from urllib.parse import quote",
        "from rustwright.sync_api import sync_playwright",
        "",
        "",
        "def human_pause(seconds):",
        "    time.sleep(seconds)",
        "",
        "",
        "def wait_for_load_completion(page, timeout_ms=30000):",
        "    try:",
        "        page.wait_for_load_state('load', timeout=timeout_ms)",
        "    except Exception as exc:",
        "        message = str(exc).lower()",
        "        if 'timeout' not in message and 'timed out' not in message:",
        "            raise",
        "        print('SUI_LOG:Page load did not complete before the 30 second limit; continuing', flush=True)",
        "",
        "",
        "def evaluate_value_expression(expression, variables):",
        "    tree = ast.parse(str(expression), mode='eval')",
        "    def visit(node):",
        "        if isinstance(node, ast.Expression): return visit(node.body)",
        "        if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, (str, int, float, bool))): return node.value",
        "        if isinstance(node, ast.Name):",
        "            if node.id not in variables: raise ValueError('Unknown variable: ' + node.id)",
        "            return variables[node.id]",
        "        if isinstance(node, ast.Attribute):",
        "            owner = visit(node.value)",
        "            if isinstance(owner, dict) and node.attr in owner: return owner[node.attr]",
        "            raise ValueError('Unknown row column: ' + node.attr)",
        "        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):",
        "            left, right = visit(node.left), visit(node.right)",
        "            return str(left) + str(right) if isinstance(left, str) or isinstance(right, str) else left + right",
        "        raise ValueError('Only variables, quoted strings, and + are supported in value expressions')",
        "    return visit(tree)",
        "",
        "",
        "def resolve(value, variables):",
        "    text = str(value or \"\")",
        "    pattern = re.compile(r\"\\{\\{?\\s*([^{}]+?)\\s*\\}\\}?\")",
        "    def replace(match):",
        "        try: return str(evaluate_value_expression(match.group(1).strip(), variables))",
        "        except (SyntaxError, ValueError, TypeError, KeyError): return match.group(0)",
        "    return pattern.sub(replace, text)",
        "",
        "",
        f"WRITER_DB_PATH = {writer_db_literal}",
        "",
    ]
    _generated_smartie_support(lines)
    lines.extend([
        "",
        "def semantic_row_text(row):",
        "    return ' '.join(str(row.get(key) or '').casefold() for key in sorted(row))",
        "",
        "",
        "def semantic_row_similarity(left, right):",
        "    left_text, right_text = semantic_row_text(left), semantic_row_text(right)",
        "    if not left_text or not right_text: return 0.0",
        "    sequence_score = difflib.SequenceMatcher(None, left_text, right_text).ratio()",
        "    left_tokens = set(re.findall(r'\\w+', left_text, re.UNICODE))",
        "    right_tokens = set(re.findall(r'\\w+', right_text, re.UNICODE))",
        "    token_score = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)",
        "    return max(sequence_score, token_score)",
        "",
        "",
        "def record_semantic_duplicate(connection, source_table, incoming_row, existing_row, similarity):",
        "    connection.execute('CREATE TABLE IF NOT EXISTS \\\"duplicates\\\" (id INTEGER PRIMARY KEY AUTOINCREMENT, source_table TEXT NOT NULL, incoming_row TEXT NOT NULL, existing_row TEXT NOT NULL, similarity REAL NOT NULL, created_at TEXT NOT NULL)')",
        "    connection.execute('INSERT INTO \\\"duplicates\\\" (source_table, incoming_row, existing_row, similarity, created_at) VALUES (?, ?, ?, ?, ?)', (str(source_table), json.dumps(incoming_row, ensure_ascii=True, default=str), json.dumps(existing_row, ensure_ascii=True, default=str), float(similarity), time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))",
        "",
        "",
        "def write_values_to_sqlite(table_name, columns, semantic_duplicate_removal=False, duplicate_threshold=0.88, current_row=None):",
        "    if not str(table_name or '').strip() or chr(0) in str(table_name): raise ValueError('Writer requires a valid table name')",
        "    normalized = []",
        "    for column in columns or []:",
        "        name = str(column.get('name') or '').strip(); column_type = str(column.get('type') or 'TEXT').upper()",
        "        if not name or chr(0) in name: raise ValueError('Writer column names must be non-empty')",
        "        if column_type not in {'TEXT', 'INTEGER', 'REAL', 'BLOB', 'BOOLEAN', 'DATE', 'DATETIME'}: raise ValueError('Unsupported Writer column type: ' + column_type)",
        "        normalized.append((name, column_type, column))",
        "    if not normalized: raise ValueError('Writer requires at least one column')",
        "    quoted_table = chr(34) + str(table_name).replace(chr(34), chr(34) * 2) + chr(34)",
        "    os.makedirs(os.path.dirname(WRITER_DB_PATH), exist_ok=True)",
        "    connection = sqlite3.connect(WRITER_DB_PATH)",
        "    try:",
        "        existing = {str(row[1]) for row in connection.execute(f'PRAGMA table_info({quoted_table})').fetchall()}",
        "        if not existing:",
        "            definitions = ', '.join(chr(34) + name.replace(chr(34), chr(34) * 2) + chr(34) + ' ' + column_type for name, column_type, _ in normalized)",
        "            connection.execute(f'CREATE TABLE IF NOT EXISTS {quoted_table} ({definitions})')",
        "        else:",
        "            for name, column_type, _ in normalized:",
        "                if name not in existing: connection.execute(f'ALTER TABLE {quoted_table} ADD COLUMN {chr(34) + name.replace(chr(34), chr(34) * 2) + chr(34)} {column_type}')",
        "        writable = [(name, column) for name, _, column in normalized if column.get('write', True)]",
        "        row_updates = [(name, column) for name, column in writable if str(column.get('value') or '') != '']",
        "        has_current_row = isinstance(current_row, dict) and current_row.get('__sui_table') == str(table_name) and current_row.get('__sui_rowid') not in (None, '')",
        "        if has_current_row and row_updates:",
        "            assignments = ', '.join(chr(34) + name.replace(chr(34), chr(34) * 2) + chr(34) + ' = ?' for name, _ in row_updates)",
        "            values = tuple(str(column.get('value') or '') for _, column in row_updates)",
        "            connection.execute(f'UPDATE {quoted_table} SET {assignments} WHERE rowid = ?', values + (int(current_row.get('__sui_rowid')),))",
        "            connection.commit()",
        "            return {'updated': True}",
        "        if has_current_row:",
        "            connection.commit()",
        "            return {'updated': False, 'skipped': True}",
        "        if semantic_duplicate_removal and writable:",
        "            incoming_row = {name: column.get('value') for name, column in writable}",
        "            connection.row_factory = sqlite3.Row",
        "            candidates = connection.execute(f'SELECT * FROM {quoted_table}').fetchall()",
        "            best_match, best_score = None, 0.0",
        "            for candidate in candidates:",
        "                candidate_row = {name: candidate[name] for name, _ in writable}",
        "                score = semantic_row_similarity(incoming_row, candidate_row)",
        "                if score > best_score: best_match, best_score = dict(candidate), score",
        "            if best_match is not None and best_score >= float(duplicate_threshold):",
        "                record_semantic_duplicate(connection, table_name, incoming_row, best_match, best_score)",
        "                connection.commit()",
        "                return {'duplicate': True, 'similarity': best_score}",
        "        if writable:",
        "            names = ', '.join(chr(34) + name.replace(chr(34), chr(34) * 2) + chr(34) for name, _ in writable)",
        "            placeholders = ', '.join('?' for _ in writable)",
        "            connection.execute(f'INSERT INTO {quoted_table} ({names}) VALUES ({placeholders})', tuple(str(column.get('value') or '') for _, column in writable))",
        "        else: connection.execute(f'INSERT INTO {quoted_table} DEFAULT VALUES')",
        "        connection.commit()",
        "    finally:",
        "        connection.close()",
        "",
        "",
        "def read_sqlite_table(table_name, column_name=None):",
        "    table = str(table_name or '').strip(); column = str(column_name or '').strip() if column_name else ''",
        "    if not table or chr(0) in table or chr(0) in column: raise ValueError('For loop requires a valid table and column')",
        "    quote = lambda value: chr(34) + str(value).replace(chr(34), chr(34) * 2) + chr(34)",
        "    connection = sqlite3.connect(WRITER_DB_PATH)",
        "    connection.row_factory = sqlite3.Row",
        "    try:",
        "        return [dict(row) for row in connection.execute(f'SELECT rowid AS __sui_rowid, * FROM {quote(table)}').fetchall()]",
        "    finally:",
        "        connection.close()",
        "",
        "",
        "def resolve_loop_range(value, variables):",
        "    text = str(value or '').strip()",
        "    match = re.fullmatch(r'(.*?)(?:\\s*([+-])\\s*(\\d+))?', text)",
        "    if not match: raise ValueError('Invalid for loop range')",
        "    base = match.group(1).strip(); offset = int(match.group(3) or 0) * (-1 if match.group(2) == '-' else 1)",
        "    if base.isdigit(): return int(base) + offset",
        "    variable_name = base.strip('{}').strip()",
        "    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', variable_name): raise ValueError('Invalid for loop range variable')",
        "    return int(variables.get(variable_name, 0)) + offset",
        "",
        "",
        "def locator_for(page, selector):",
        "    value = str(selector or '').strip()",
        "    if value.startswith('role='):",
        "        role_query = value[5:].strip(); name = None",
        "        if '[name=' in role_query:",
        "            role_query, name_part = role_query.split('[name=', 1); name = name_part.rstrip(']').strip(\"'\\\"\")",
        "        get_by_role = getattr(page, 'get_by_role', None)",
        "        if get_by_role:",
        "            return get_by_role(role_query.strip(), name=name) if name else get_by_role(role_query.strip())",
        "        return page.locator('[role=\\\"' + role_query.strip() + '\\\"]')",
        "    return page.locator(value)",
        "",
        "",
        "def is_frame_transition_error(exc):",
        "    message = str(exc).lower()",
        "    return any(marker in message for marker in (",
        "        'frame ownership changed', 'execution context was destroyed',",
        "        'cannot find context with specified id', 'context was destroyed',",
        "    ))",
        "",
        "",
        "def wait_for_locator(page, selector, action, timeout_ms=30000):",
        "    value = str(selector or '').strip()",
        "    if not value: raise ValueError(action + ' requires a selector')",
        "    deadline = time.monotonic() + max(float(timeout_ms), 1) / 1000.0",
        "    while True:",
        "        try:",
        "            locator = locator_for(page, value).first",
        "            remaining_ms = max(100, int((deadline - time.monotonic()) * 1000))",
        "            locator.wait_for(state='attached' if str(action).lower() == 'scrape' else 'visible', timeout=remaining_ms)",
        "            return locator",
        "        except Exception as exc:",
        "            if not is_frame_transition_error(exc) or time.monotonic() >= deadline:",
        "                raise RuntimeError(action + ' target not found for selector ' + repr(value)) from exc",
        "            time.sleep(0.10)",
        "",
        "",
        "def page_body_text(page, timeout_seconds=30):",
        "    deadline = time.monotonic() + max(float(timeout_seconds), 0.1)",
        "    while True:",
        "        try: return page.locator('body').inner_text()",
        "        except Exception as exc:",
        "            if not is_frame_transition_error(exc) or time.monotonic() >= deadline: raise",
        "            time.sleep(0.10)",
        "",
        "",
        "def page_url(page):",
        "    try: return str(page.url)",
        "    except Exception: return '<unavailable>'",
        "",
        "",
        "def page_description(page):",
        "    try:",
        "        pages = list(page.context.pages)",
        "        page_index = next((index for index, item in enumerate(pages) if item is page), '?')",
        "    except Exception:",
        "        page_index = '?'",
        "    return f'active_page={page_index} url={page_url(page)}'",
        "",
        "",
        "def context_pages(page):",
        "    try: return list(page.context.pages)",
        "    except Exception: return [page]",
        "",
        "",
        "def new_pages(page, before_pages):",
        "    candidates = [candidate for candidate in context_pages(page) if not any(candidate is old for old in before_pages)]",
        "    for candidate in candidates: PAGE_OPENERS[id(candidate)] = page",
        "    return candidates",
        "",
        "",
        "def destination_matches(page, expected_url, destination_selector):",
        "    if expected_url:",
        "        current_url = page_url(page)",
        "        if current_url != expected_url and expected_url not in current_url:",
        "            return False",
        "    if destination_selector:",
        "        if locator_for(page, destination_selector).first.count() < 1:",
        "            return False",
        "    return True",
        "",
        "",
        "def wait_for_action_destination(page, before_pages, settings, action):",
        "    expected_url = resolve(settings.get('expected_url'), variables).strip()",
        "    destination_selector = resolve(settings.get('destination_selector'), variables).strip()",
        "    timeout_seconds = min(max(float(settings.get('wait_timeout_seconds', 15)), 0.1), 120.0)",
        "    if not expected_url and not destination_selector:",
        "        candidates = new_pages(page, before_pages)",
        "        return candidates[-1] if candidates else page",
        "    deadline = time.monotonic() + timeout_seconds",
        "    while True:",
        "        candidates = new_pages(page, before_pages)",
        "        for candidate in [*candidates, page]:",
        "            if destination_matches(candidate, expected_url, destination_selector):",
        "                return candidate",
        "        if time.monotonic() >= deadline:",
        "            condition = f'expected_url={expected_url!r} destination_selector={destination_selector!r}'",
        "            raise TimeoutError(f'{action} destination wait timed out ({condition}); {page_description(page)}')",
        "        time.sleep(0.05)",
        "",
        "",
        "def emit_frame(page):",
        "    try:",
        "        frame = page.screenshot(type=\"jpeg\", quality=70)",
        "        print(\"SUI_FRAME:\" + base64.b64encode(frame).decode(\"ascii\"), flush=True)",
        "    except Exception as exc:",
        "        print(\"SUI_FRAME_ERROR:\" + str(exc), flush=True)",
        "",
        "",
        "USE_JS_RUNNER = \"\"\"async ({body, args}) => { const source = 'return (async function suiJSRunner(args) {' + String(body || '') + '\\\\n})(args);'; const runner = new Function('args', source); return await runner(args); }\"\"\"",
        "",
        "",
        "def evaluate_condition(left, operator, right, variables):",
        "    left_text = resolve(left, variables).strip()",
        "    right_text = resolve(right, variables).strip()",
        "    try:",
        "        left_value = float(left_text); right_value = float(right_text)",
        "    except (TypeError, ValueError):",
        "        left_value = left_text; right_value = right_text",
        "    if operator == '=': return left_value == right_value",
        "    if operator == '<>': return left_value != right_value",
        "    if operator == '<': return left_value < right_value",
        "    if operator == '<=': return left_value <= right_value",
        "    if operator == '>': return left_value > right_value",
        "    if operator == '>=': return left_value >= right_value",
        "    if operator == 'in': return left_text.lower() in {item.strip().lower() for item in right_text.split(',')}",
        "    raise ValueError('Unsupported condition operator: ' + str(operator))",
        "",
        "",
        "def show_message_box(page, message):",
        "    page.evaluate(\"\"\"(text) => { const old = document.getElementById('sui-message-box'); if (old) old.remove(); const overlay = document.createElement('div'); overlay.id = 'sui-message-box'; Object.assign(overlay.style, { position: 'fixed', inset: '0', zIndex: '2147483647', display: 'grid', placeItems: 'center', background: 'rgba(20, 25, 22, .35)' }); const box = document.createElement('div'); Object.assign(box.style, { padding: '24px', borderRadius: '12px', background: '#fff', color: '#17201d' }); const copy = document.createElement('div'); copy.textContent = text; copy.style.marginBottom = '20px'; const ok = document.createElement('button'); ok.id = 'sui-message-box-ok'; ok.textContent = 'OK'; ok.onclick = () => overlay.remove(); box.append(copy, ok); overlay.append(box); document.body.append(overlay); }\"\"\", message)",
        "",
        "",
        f"recipe_steps = json.loads({steps_literal})",
        f"variables = json.loads({variables_literal})",
        f"ORCHESTRATOR_PROJECT_ID = {project_id_literal}",
        f"CALL_STACK = [{recipe_id_literal}]",
        "PAGE_OPENERS = {}",
        "PAGE_SOURCES = {}",
        "",
        "",
        "def load_called_recipe(settings):",
        "    target_id = str(settings.get('recipe_id') or '').strip()",
        "    target_name = str(settings.get('recipe_name') or '').strip()",
        "    connection = sqlite3.connect(WRITER_DB_PATH)",
        "    try:",
        "        if target_id:",
        "            row = connection.execute(\"SELECT rv.definition_json FROM recipe_versions rv JOIN recipes r ON r.id = rv.recipe_id WHERE r.project_id = ? AND r.id = ? ORDER BY rv.version DESC LIMIT 1\", (ORCHESTRATOR_PROJECT_ID, target_id)).fetchone()",
        "        else:",
        "            row = connection.execute(\"SELECT rv.definition_json FROM recipe_versions rv JOIN recipes r ON r.id = rv.recipe_id WHERE r.project_id = ? AND r.name = ? ORDER BY rv.version DESC LIMIT 1\", (ORCHESTRATOR_PROJECT_ID, target_name)).fetchone()",
        "    finally:",
        "        connection.close()",
        "    if not row: raise ValueError('Target recipe was not found in this project')",
        "    return json.loads(row[0])",
        "",
        "",
        "def run_called_recipe(page, settings):",
        "    target = load_called_recipe(settings)",
        "    target_id = str(target.get('recipe_id') or '')",
        "    if target_id in CALL_STACK: raise ValueError('Recursive recipe calls are not allowed')",
        "    previous_variables = dict(variables)",
        "    variables.clear()",
        "    variables.update({item.get('name'): item.get('value', '') for item in target.get('variables', []) if item.get('name')})",
        "    CALL_STACK.append(target_id)",
        "    try:",
        "        return execute_steps(page, target.get('steps') or [])",
        "    finally:",
        "        CALL_STACK.pop()",
        "        variables.clear()",
        "        variables.update(previous_variables)",
        "",
        "",
        "def execute_step(page, step):",
        "    if step.get('disabled'): return page",
        "    settings = step.get('settings') or {}",
        "    step_type = step.get('type')",
        "    if step_type == 'navigate_to_website':",
        "        human_pause(0.35); page.goto(resolve(settings.get('url'), variables), wait_until='domcontentloaded'); human_pause(0.65)",
        "    elif step_type == 'navigate':",
        "        action = settings.get('action', 'back'); human_pause(0.30)",
        "        if action == 'back':",
        "            opener = PAGE_OPENERS.get(id(page))",
        "            source_url = PAGE_SOURCES.get(id(page))",
        "            previous_url = page_url(page)",
        "            try:",
        "                page.go_back(wait_until='commit')",
        "            except Exception:",
        "                if not opener: raise",
        "                try: page.close()",
        "                except Exception: pass",
        "                page = opener",
        "            else:",
        "                if opener and (page_url(page) == previous_url or page_url(page).startswith('about:')):",
        "                    try: page.close()",
        "                    except Exception: pass",
        "                    page = opener",
        "                elif source_url and page_url(page) == previous_url and source_url != previous_url:",
        "                    page.goto(source_url, wait_until='commit')",
        "        elif action == 'forward': page.go_forward(wait_until='commit')",
        "        elif action == 'refresh': page.reload(wait_until='commit')",
        "        else: raise ValueError('Unsupported Navigate action: ' + str(action))",
        "        human_pause(0.45)",
        "    elif step_type == 'click':",
        "        source_url = page_url(page); before_pages = context_pages(page); human_pause(0.30)",
        "        target = wait_for_locator(page, resolve(settings.get('selector'), variables), 'Click')",
        "        try:",
        "            target.click()",
        "        except Exception as exc:",
        "            navigated = page_url(page) != source_url or bool(new_pages(page, before_pages))",
        "            if not navigated: raise",
        "            print(f\"SUI_LOG:Click raised after navigation; continuing; command_id={step.get('command_id', '')}; error={exc}\", flush=True)",
        "        page = wait_for_action_destination(page, before_pages, settings, 'Click'); PAGE_SOURCES[id(page)] = source_url; human_pause(0.35)",
        "    elif step_type == 'fill':",
        "        human_pause(0.30); wait_for_locator(page, resolve(settings.get('selector'), variables), 'Fill').fill(resolve(settings.get('value'), variables)); human_pause(0.35)",
        "    elif step_type == 'delay':",
        "        if settings.get('until_load_completes'): wait_for_load_completion(page)",
        "        else: time.sleep(float(settings.get('seconds', 1)))",
        "    elif step_type == 'keypress':",
        "        source_url = page_url(page); before_pages = context_pages(page); human_pause(0.25); page.keyboard.press(str(settings.get('key') or 'Enter')); page = wait_for_action_destination(page, before_pages, settings, 'Keypress'); PAGE_SOURCES[id(page)] = source_url; human_pause(0.25)",
        "    elif step_type == 'assign':",
        "        variables[settings.get('variable')] = resolve(settings.get('value'), variables); human_pause(0.25)",
        "    elif step_type == 'scrape':",
        "        variables[settings.get('variable')] = wait_for_locator(page, resolve(settings.get('selector'), variables), 'Scrape').inner_text(); human_pause(0.30)",
        "    elif step_type == 'pattern_matcher':",
        "        source = page_body_text(page); combined_pattern = '|'.join('(?:' + str(pattern) + ')' for pattern in (settings.get('patterns') or []))",
        "        matches = [candidate.group(0) for candidate in re.finditer(combined_pattern, source, re.IGNORECASE)] if combined_pattern else []",
        "        variables[settings.get('variable')] = ' | '.join(matches) if matches else 'NO CONTACTS FOUND'; human_pause(0.30)",
        "    elif step_type == 'smartie':",
        "        smartie_system = resolve(settings.get('system_prompt'), variables); smartie_user = resolve(settings.get('user_prompt'), variables); variables[settings.get('output_variable')] = run_smartie(settings.get('model', 'gemini-2.5-flash'), smartie_system, smartie_user, settings.get('output_type', 'text'), settings.get('schema') or []); human_pause(0.30)",
        "    elif step_type == 'message_box':",
        f"        show_message_box(page, resolve(settings.get('message'), variables)); page.locator('#sui-message-box-ok').click() if {json.dumps(mode)} == 'headless' else page.wait_for_function(\"() => !document.getElementById('sui-message-box')\")",
        "    elif step_type == 'writer':",
        "        writer_columns = [dict(column, value=resolve(column.get('value'), variables)) for column in (settings.get('columns') or [{'name': 'value', 'type': 'TEXT', 'value': settings.get('value', ''), 'write': True}])]; current_row = variables.get(settings.get('current_row')) if settings.get('current_row') else None; write_values_to_sqlite(settings.get('table_name'), writer_columns, settings.get('semantic_duplicate_removal', False), settings.get('duplicate_threshold', 0.88), current_row); human_pause(0.25)",
        "    elif step_type in {'call_recipe', 'invoke_recipe'}:",
        "        page = run_called_recipe(page, settings); human_pause(0.25)",
        "    elif step_type == 'check_element_exists':",
        "        selector = resolve(settings.get('selector'), variables); locator = locator_for(page, selector)",
        "        try:",
        "            locator.wait_for(state='attached', timeout=int(float(settings.get('timeout_seconds', 5)) * 1000)); exists = True",
        "        except Exception:",
        "            exists = False",
        "        page = execute_steps(page, (step.get('branches') or {}).get('exists' if exists else 'not_exists', []))",
        "    elif step_type == 'check_text_exists':",
        "        expected = resolve(settings.get('text'), variables); timeout = float(settings.get('timeout_seconds', 5)); deadline = time.monotonic() + timeout; exists = False",
        "        while time.monotonic() < deadline:",
        "            body = page_body_text(page, max(0.1, deadline - time.monotonic())); exists = expected.lower() in body.lower() if settings.get('case_insensitive', True) else expected in body",
        "            if exists: break",
        "            time.sleep(0.2)",
        "        page = execute_steps(page, (step.get('branches') or {}).get('exists' if exists else 'not_exists', []))",
        "    elif step_type == 'if':",
        "        matched = evaluate_condition(settings.get('left'), settings.get('operator', '='), settings.get('right'), variables); page = execute_steps(page, (step.get('branches') or {}).get('then' if matched else 'else', []))",
        "    elif step_type == 'for':",
        "        if settings.get('source') == 'table':",
        "            table_name = resolve(settings.get('table_name'), variables); table_mode = settings.get('table_mode') or 'row'; iterator = settings.get('iterator') or ('row' if table_mode == 'row' else 'value'); column_name = resolve(settings.get('column_name'), variables) if table_mode == 'column' else None; loop_iterable = enumerate(read_sqlite_table(table_name, column_name))",
        "        else:",
        "            loop_count = resolve_loop_range(settings.get('range_value'), variables); loop_start = int(settings.get('start_value', 0)); iterator = settings.get('iterator') or 'i'",
        "            if loop_count < 0: raise ValueError('For loop range must not be negative')",
        "            loop_iterable = ((loop_index, loop_index) for loop_index in range(loop_start, loop_start + loop_count))",
        "        for loop_index, loop_value in loop_iterable:",
        "            variables[iterator] = loop_value.get(column_name) if settings.get('source') == 'table' and table_mode == 'column' else loop_value",
        "            variables['currentRow'] = dict(loop_value, __sui_table=table_name) if settings.get('source') == 'table' and isinstance(loop_value, dict) else variables.get('currentRow')",
        "            print(f\"SUI_LOG:For loop iteration {loop_index} started\", flush=True)",
        "            try:",
        "                page = execute_steps(page, (step.get('branches') or {}).get('body', []))",
        "            except Exception as exc:",
        "                print(f\"SUI_LOG:For loop iteration {loop_index} failed; {page_description(page)}; error={type(exc).__name__}: {exc}\", flush=True)",
        "                raise",
        "            print(f\"SUI_LOG:For loop iteration {loop_index} completed\", flush=True)",
        "    elif step_type == 'use_js':",
        "        source_url = page_url(page); before_pages = context_pages(page); args = [variables.get(name, '') for name in (settings.get('inputs') or [])]; result = page.evaluate(USE_JS_RUNNER, {'body': settings.get('code', ''), 'args': args}); page = wait_for_action_destination(page, before_pages, settings, 'Use JS'); PAGE_SOURCES[id(page)] = source_url; output_name = settings.get('output')",
        "        if output_name: variables[output_name] = result",
        "    else:",
        "        raise ValueError('Unsupported activity: ' + str(step_type))",
        "    return page",
        "",
        "",
        "def execute_steps(page, steps):",
        "    for step in steps:",
        "        if step.get('disabled'):",
        "            print(f\"SUI_LOG:Step skipped: {step.get('id', 'step')} ({step.get('type')})\", flush=True)",
        "            continue",
        "        step_id = step.get('id', 'step')",
        "        print(f\"SUI_LOG:Step started: {step_id} ({step.get('type')})\", flush=True)",
        "        try:",
        "            page = execute_step(page, step)",
        "        except Exception as exc:",
        "            print(f\"SUI_LOG:Step failed: {step_id} ({step.get('type')}); {page_description(page)}; error={type(exc).__name__}: {exc}\", flush=True)",
        "            raise",
        "        emit_frame(page)",
        "        print(f\"SUI_LOG:Step completed: {step_id} ({step.get('type')})\", flush=True)",
        "    return page",
        "",
        "",
    ])
    _generated_window_support(lines)
    lines.extend(
        [
            "",
            "",
            "def run():",
            "    browser = None",
            "    with sync_playwright() as playwright:",
            f"        foreground_window = preserve_foreground_window() if {json.dumps(mode)} == 'headful' else None",
            "        before_handles = visible_window_handles()",
            "        browser = playwright.chromium.launch(headless=" + json.dumps(mode) + " == 'headless')",
            "        page = browser.new_page(no_viewport=True)",
            f"        maximize_browser_window(before_handles) if {json.dumps(mode)} == 'headful' else None",
            "        restore_foreground_window(foreground_window)",
            "        try:",
            "            emit_frame(page)",
            "            page = execute_steps(page, recipe_steps)",
            "        finally:",
            "            try:",
            "                if browser: browser.close()",
            "            except Exception: pass",
            "",
            "",
            "if __name__ == '__main__':",
            "    run()",
            "",
        ]
    )
    return "\n".join(lines)


def generate_python(definition: dict[str, Any]) -> str:
    return _generate_recursive_sync_python(definition)
    variable_values = {variable["name"]: variable.get("value", "") for variable in definition.get("variables", [])}
    chrome_mode = definition.get("mode") or "headful"
    lines = [
        "import base64",
        "import ctypes",
        "import os",
        "import re",
        "import time",
        "from rustwright.sync_api import sync_playwright",
        "",
        "",
        "def human_pause(seconds):",
        "    time.sleep(seconds)",
        "",
        "",
        "def resolve(value, variables):",
        "    text = str(value or \"\")",
        "    return re.sub(r\"\\{\\{\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*\\}\\}\", lambda match: str(variables.get(match.group(1), match.group(0))), text)",
        "",
        "",
        "def emit_frame(page):",
        "    frame = page.screenshot(type=\"jpeg\", quality=70)",
        "    print(\"SUI_FRAME:\" + base64.b64encode(frame).decode(\"ascii\"), flush=True)",
        "",
        "",
        "def show_message_box(page, message):",
        "    page.evaluate(\"\"\"(text) => {",
        "        const old = document.getElementById('sui-message-box');",
        "        if (old) old.remove();",
        "        const overlay = document.createElement('div');",
        "        overlay.id = 'sui-message-box';",
        "        Object.assign(overlay.style, { position: 'fixed', inset: '0', zIndex: '2147483647', display: 'grid', placeItems: 'center', background: 'rgba(20, 25, 22, .35)', fontFamily: 'system-ui, sans-serif' });",
        "        const box = document.createElement('div');",
        "        Object.assign(box.style, { width: 'min(420px, calc(100vw - 40px))', padding: '24px', borderRadius: '12px', background: '#fff', color: '#17201d', boxShadow: '0 18px 50px rgba(0,0,0,.28)' });",
        "        const copy = document.createElement('div');",
        "        copy.textContent = text;",
        "        copy.style.cssText = 'white-space: pre-wrap; line-height: 1.5; margin-bottom: 20px;';",
        "        const ok = document.createElement('button');",
        "        ok.id = 'sui-message-box-ok';",
        "        ok.textContent = 'OK';",
        "        ok.style.cssText = 'border: 0; border-radius: 7px; padding: 9px 18px; background: #FF5B35; color: #fff; font-weight: 700; cursor: pointer;';",
        "        ok.addEventListener('click', () => overlay.remove());",
        "        box.append(copy, ok);",
        "        overlay.append(box);",
        "        document.body.append(overlay);",
        "    }\"\"\", message)",
        "",
        "",
        "def preserve_foreground_window():",
        "    if os.name != \"nt\": return None",
        "    return ctypes.windll.user32.GetForegroundWindow()",
        "",
        "",
        "def restore_foreground_window(hwnd):",
        "    if hwnd and os.name == \"nt\": ctypes.windll.user32.SetForegroundWindow(hwnd)",
        "",
        "",
        "def visible_window_handles():",
        "    if os.name != \"nt\": return set()",
        "    user32 = ctypes.windll.user32",
        "    handles = set()",
        "    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)",
        "    def visit(hwnd, _):",
        "        if user32.IsWindowVisible(hwnd): handles.add(int(hwnd))",
        "        return True",
        "    callback_ref = callback_type(visit)",
        "    user32.EnumWindows(callback_ref, 0)",
        "    return handles",
        "",
        "",
        "def is_chrome_window(hwnd):",
        "    user32 = ctypes.windll.user32",
        "    title = ctypes.create_unicode_buffer(512)",
        "    class_name = ctypes.create_unicode_buffer(256)",
        "    user32.GetWindowTextW(hwnd, title, 512)",
        "    user32.GetClassNameW(hwnd, class_name, 256)",
        "    return \"Chrome_WidgetWin\" in class_name.value or \"Google Chrome\" in title.value",
        "",
        "",
        "def maximize_browser_window(before_handles):",
        "    if os.name != \"nt\": return None",
        "    user32 = ctypes.windll.user32",
        "    candidates = []",
        "    for _ in range(20):",
        "        candidates = [hwnd for hwnd in visible_window_handles() - before_handles if is_chrome_window(hwnd)]",
        "        if candidates: break",
        "        time.sleep(0.05)",
        "    foreground = user32.GetForegroundWindow()",
        "    if not candidates and foreground and is_chrome_window(foreground): candidates = [foreground]",
        "    if not candidates: return None",
        "    hwnd = candidates[-1]",
        "    user32.ShowWindow(hwnd, 3)",
        "    return hwnd",
        "",
        "",
        "def launch_browser(playwright, mode):",
        "    browser = playwright.chromium.launch(headless=mode == \"headless\")",
        "    page = browser.new_page(no_viewport=True)",
        "    return browser, page",
        "",
        "",
        "def run():",
        "    browser = None",
        "    with sync_playwright() as playwright:",
        f"        foreground_window = preserve_foreground_window() if {json.dumps(chrome_mode)} == \"headful\" else None",
        "        before_handles = visible_window_handles()",
        f"        browser, page = launch_browser(playwright, {json.dumps(chrome_mode)})",
        f"        maximize_browser_window(before_handles) if {json.dumps(chrome_mode)} == \"headful\" else None",
        "        restore_foreground_window(foreground_window)",
        f"        variables = {json.dumps(variable_values)}",
        "        emit_frame(page)",
        "        try:",
    ]
    for step in definition["steps"]:
        # json.dumps creates a safe Python string literal; it is never executed as code.
        start_log = json.dumps(f"SUI_LOG:Step started: {step['id']} ({step['type']})")
        complete_log = json.dumps(f"SUI_LOG:Step completed: {step['id']} ({step['type']})")
        if step["type"] == "navigate_to_website":
            url_literal = json.dumps(step["settings"]["url"])
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    "            human_pause(0.35)",
                    f"            page.goto(resolve({url_literal}, variables), wait_until=\"domcontentloaded\")",
                    "            human_pause(0.65)",
                    f"            print(\"SUI_LOG:Navigated: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        elif step["type"] == "fill":
            selector_literal = json.dumps(step["settings"]["selector"])
            value_literal = json.dumps(step["settings"].get("value", ""))
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    "            human_pause(0.30)",
                    f"            page.locator({selector_literal}).first.fill(resolve({value_literal}, variables))",
                    "            human_pause(0.35)",
                    f"            print(\"SUI_LOG:Filled: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        elif step["type"] == "delay":
            seconds_literal = json.dumps(step["settings"].get("seconds", 1))
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    f"            time.sleep({seconds_literal})",
                    f"            print(\"SUI_LOG:Delayed: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        elif step["type"] == "assign":
            variable_literal = json.dumps(step["settings"]["variable"])
            value_literal = json.dumps(step["settings"].get("value", ""))
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    f"            variables[{variable_literal}] = resolve({value_literal}, variables)",
                    "            human_pause(0.25)",
                    f"            print(\"SUI_LOG:Assigned: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        elif step["type"] == "scrape":
            selector_literal = json.dumps(step["settings"]["selector"])
            variable_literal = json.dumps(step["settings"]["variable"])
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    f"            variables[{variable_literal}] = page.locator({selector_literal}).first.inner_text()",
                    "            human_pause(0.30)",
                    f"            print(\"SUI_LOG:Scraped: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        elif step["type"] == "message_box":
            message_literal = json.dumps(step["settings"].get("message", ""))
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    f"            show_message_box(page, resolve({message_literal}, variables))",
                    *( ["            page.locator(\"#sui-message-box-ok\").click()"] if chrome_mode == "headless" else ["            page.wait_for_function(\"() => !document.getElementById('sui-message-box')\")"] ),
                    f"            print(\"SUI_LOG:Message box closed: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        else:
            selector_literal = json.dumps(step["settings"]["selector"])
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    "            human_pause(0.30)",
                    f"            page.locator({selector_literal}).first.click()",
                    "            human_pause(0.35)",
                    f"            print(\"SUI_LOG:Clicked: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        lines.append("            emit_frame(page)")
    lines.extend(
        [
            "        finally:",
            "            try:",
            "                if browser: browser.close()",
            "            except Exception: pass",
            "",
            "",
            "if __name__ == \"__main__\":",
            "    run()",
            "",
        ]
    )
    return "\n".join(lines)


def _generate_recursive_step_python(definition: dict[str, Any]) -> str:
    mode = definition.get("mode") or "headful"
    project_id_literal = json.dumps(str(definition.get("project_id") or ""))
    recipe_id_literal = json.dumps(str(definition.get("recipe_id") or ""))
    step_literal = json.dumps(json.dumps((definition.get("steps") or [{}])[0]))
    variables_literal = json.dumps(json.dumps({item["name"]: item.get("value", "") for item in definition.get("variables", [])}))
    writer_db_literal = json.dumps(str(DB_PATH))
    lines = [
        "import asyncio",
        "import ast",
        "import base64",
        "import ctypes",
        "import difflib",
        "import json",
        "import os",
        "import re",
        "import sqlite3",
        "import sys",
        "import time",
        "import urllib.error",
        "import urllib.request",
        "from urllib.parse import quote",
        "from rustwright.async_api import async_playwright",
        "",
        "",
        "async def human_pause(seconds):",
        "    await asyncio.sleep(seconds)",
        "",
        "",
        "async def wait_for_load_completion(page, timeout_ms=30000):",
        "    try:",
        "        await page.wait_for_load_state('load', timeout=timeout_ms)",
        "    except Exception as exc:",
        "        message = str(exc).lower()",
        "        if 'timeout' not in message and 'timed out' not in message:",
        "            raise",
        "        print('SUI_LOG:Page load did not complete before the 30 second limit; continuing', flush=True)",
        "",
        "",
        "def evaluate_value_expression(expression, variables):",
        "    tree = ast.parse(str(expression), mode='eval')",
        "    def visit(node):",
        "        if isinstance(node, ast.Expression): return visit(node.body)",
        "        if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, (str, int, float, bool))): return node.value",
        "        if isinstance(node, ast.Name):",
        "            if node.id not in variables: raise ValueError('Unknown variable: ' + node.id)",
        "            return variables[node.id]",
        "        if isinstance(node, ast.Attribute):",
        "            owner = visit(node.value)",
        "            if isinstance(owner, dict) and node.attr in owner: return owner[node.attr]",
        "            raise ValueError('Unknown row column: ' + node.attr)",
        "        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):",
        "            left, right = visit(node.left), visit(node.right)",
        "            return str(left) + str(right) if isinstance(left, str) or isinstance(right, str) else left + right",
        "        raise ValueError('Only variables, quoted strings, and + are supported in value expressions')",
        "    return visit(tree)",
        "",
        "",
        "def resolve(value, variables):",
        "    text = str(value or \"\")",
        "    pattern = re.compile(r\"\\{\\{?\\s*([^{}]+?)\\s*\\}\\}?\")",
        "    def replace(match):",
        "        try: return str(evaluate_value_expression(match.group(1).strip(), variables))",
        "        except (SyntaxError, ValueError, TypeError, KeyError): return match.group(0)",
        "    return pattern.sub(replace, text)",
        "",
        "",
        f"WRITER_DB_PATH = {writer_db_literal}",
        "",
    ]
    _generated_smartie_support(lines)
    lines.extend([
        "",
        "def semantic_row_text(row):",
        "    return ' '.join(str(row.get(key) or '').casefold() for key in sorted(row))",
        "",
        "",
        "def semantic_row_similarity(left, right):",
        "    left_text, right_text = semantic_row_text(left), semantic_row_text(right)",
        "    if not left_text or not right_text: return 0.0",
        "    sequence_score = difflib.SequenceMatcher(None, left_text, right_text).ratio()",
        "    left_tokens = set(re.findall(r'\\w+', left_text, re.UNICODE))",
        "    right_tokens = set(re.findall(r'\\w+', right_text, re.UNICODE))",
        "    token_score = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)",
        "    return max(sequence_score, token_score)",
        "",
        "",
        "def record_semantic_duplicate(connection, source_table, incoming_row, existing_row, similarity):",
        "    connection.execute('CREATE TABLE IF NOT EXISTS \\\"duplicates\\\" (id INTEGER PRIMARY KEY AUTOINCREMENT, source_table TEXT NOT NULL, incoming_row TEXT NOT NULL, existing_row TEXT NOT NULL, similarity REAL NOT NULL, created_at TEXT NOT NULL)')",
        "    connection.execute('INSERT INTO \\\"duplicates\\\" (source_table, incoming_row, existing_row, similarity, created_at) VALUES (?, ?, ?, ?, ?)', (str(source_table), json.dumps(incoming_row, ensure_ascii=True, default=str), json.dumps(existing_row, ensure_ascii=True, default=str), float(similarity), time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))",
        "",
        "",
        "def write_values_to_sqlite(table_name, columns, semantic_duplicate_removal=False, duplicate_threshold=0.88, current_row=None):",
        "    if not str(table_name or '').strip() or chr(0) in str(table_name): raise ValueError('Writer requires a valid table name')",
        "    normalized = []",
        "    for column in columns or []:",
        "        name = str(column.get('name') or '').strip(); column_type = str(column.get('type') or 'TEXT').upper()",
        "        if not name or chr(0) in name: raise ValueError('Writer column names must be non-empty')",
        "        if column_type not in {'TEXT', 'INTEGER', 'REAL', 'BLOB', 'BOOLEAN', 'DATE', 'DATETIME'}: raise ValueError('Unsupported Writer column type: ' + column_type)",
        "        normalized.append((name, column_type, column))",
        "    if not normalized: raise ValueError('Writer requires at least one column')",
        "    quoted_table = chr(34) + str(table_name).replace(chr(34), chr(34) * 2) + chr(34)",
        "    os.makedirs(os.path.dirname(WRITER_DB_PATH), exist_ok=True)",
        "    connection = sqlite3.connect(WRITER_DB_PATH)",
        "    try:",
        "        existing = {str(row[1]) for row in connection.execute(f'PRAGMA table_info({quoted_table})').fetchall()}",
        "        if not existing:",
        "            definitions = ', '.join(chr(34) + name.replace(chr(34), chr(34) * 2) + chr(34) + ' ' + column_type for name, column_type, _ in normalized)",
        "            connection.execute(f'CREATE TABLE IF NOT EXISTS {quoted_table} ({definitions})')",
        "        else:",
        "            for name, column_type, _ in normalized:",
        "                if name not in existing: connection.execute(f'ALTER TABLE {quoted_table} ADD COLUMN {chr(34) + name.replace(chr(34), chr(34) * 2) + chr(34)} {column_type}')",
        "        writable = [(name, column) for name, _, column in normalized if column.get('write', True)]",
        "        row_updates = [(name, column) for name, column in writable if str(column.get('value') or '') != '']",
        "        has_current_row = isinstance(current_row, dict) and current_row.get('__sui_table') == str(table_name) and current_row.get('__sui_rowid') not in (None, '')",
        "        if has_current_row and row_updates:",
        "            assignments = ', '.join(chr(34) + name.replace(chr(34), chr(34) * 2) + chr(34) + ' = ?' for name, _ in row_updates)",
        "            values = tuple(str(column.get('value') or '') for _, column in row_updates)",
        "            connection.execute(f'UPDATE {quoted_table} SET {assignments} WHERE rowid = ?', values + (int(current_row.get('__sui_rowid')),))",
        "            connection.commit()",
        "            return {'updated': True}",
        "        if has_current_row:",
        "            connection.commit()",
        "            return {'updated': False, 'skipped': True}",
        "        if semantic_duplicate_removal and writable:",
        "            incoming_row = {name: column.get('value') for name, column in writable}",
        "            connection.row_factory = sqlite3.Row",
        "            candidates = connection.execute(f'SELECT * FROM {quoted_table}').fetchall()",
        "            best_match, best_score = None, 0.0",
        "            for candidate in candidates:",
        "                candidate_row = {name: candidate[name] for name, _ in writable}",
        "                score = semantic_row_similarity(incoming_row, candidate_row)",
        "                if score > best_score: best_match, best_score = dict(candidate), score",
        "            if best_match is not None and best_score >= float(duplicate_threshold):",
        "                record_semantic_duplicate(connection, table_name, incoming_row, best_match, best_score)",
        "                connection.commit()",
        "                return {'duplicate': True, 'similarity': best_score}",
        "        if writable:",
        "            names = ', '.join(chr(34) + name.replace(chr(34), chr(34) * 2) + chr(34) for name, _ in writable)",
        "            placeholders = ', '.join('?' for _ in writable)",
        "            connection.execute(f'INSERT INTO {quoted_table} ({names}) VALUES ({placeholders})', tuple(str(column.get('value') or '') for _, column in writable))",
        "        else: connection.execute(f'INSERT INTO {quoted_table} DEFAULT VALUES')",
        "        connection.commit()",
        "    finally:",
        "        connection.close()",
        "",
        "",
        "def resolve_loop_range(value, variables):",
        "    text = str(value or '').strip()",
        "    match = re.fullmatch(r'(.*?)(?:\\s*([+-])\\s*(\\d+))?', text)",
        "    if not match: raise ValueError('Invalid for loop range')",
        "    base = match.group(1).strip(); offset = int(match.group(3) or 0) * (-1 if match.group(2) == '-' else 1)",
        "    if base.isdigit(): return int(base) + offset",
        "    variable_name = base.strip('{}').strip()",
        "    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', variable_name): raise ValueError('Invalid for loop range variable')",
        "    return int(variables.get(variable_name, 0)) + offset",
        "",
        "",
        "USE_JS_RUNNER = \"\"\"async ({body, args}) => { const source = 'return (async function suiJSRunner(args) {' + String(body || '') + '\\\\n})(args);'; const runner = new Function('args', source); return await runner(args); }\"\"\"",
        "",
        "",
        "def read_sqlite_table(table_name, column_name=None):",
        "    table = str(table_name or '').strip(); column = str(column_name or '').strip() if column_name else ''",
        "    if not table or chr(0) in table or chr(0) in column: raise ValueError('For loop requires a valid table and column')",
        "    quote = lambda value: chr(34) + str(value).replace(chr(34), chr(34) * 2) + chr(34)",
        "    connection = sqlite3.connect(WRITER_DB_PATH)",
        "    connection.row_factory = sqlite3.Row",
        "    try:",
        "        return [dict(row) for row in connection.execute(f'SELECT rowid AS __sui_rowid, * FROM {quote(table)}').fetchall()]",
        "    finally:",
        "        connection.close()",
        "",
        "",
        "async def emit_frame(page, capture_lock):",
        "    async with capture_lock:",
        "        try:",
        "            frame = await page.screenshot(type=\"jpeg\", quality=70)",
        "            print(\"SUI_FRAME:\" + base64.b64encode(frame).decode(\"ascii\"), flush=True)",
        "        except Exception as exc:",
        "            print(\"SUI_FRAME_ERROR:\" + str(exc), flush=True)",
        "",
        "",
        "def async_locator_for(page, selector):",
        "    value = str(selector or '').strip()",
        "    if value.startswith('role='):",
        "        role_query = value[5:].strip(); name = None",
        "        if '[name=' in role_query:",
        "            role_query, name_part = role_query.split('[name=', 1); name = name_part.rstrip(']').strip(\"'\\\"\")",
        "        get_by_role = getattr(page, 'get_by_role', None)",
        "        if get_by_role:",
        "            return get_by_role(role_query.strip(), name=name) if name else get_by_role(role_query.strip())",
        "        return page.locator('[role=\\\"' + role_query.strip() + '\\\"]')",
        "    return page.locator(value)",
        "",
        "",
        "def is_frame_transition_error(exc):",
        "    message = str(exc).lower()",
        "    return any(marker in message for marker in (",
        "        'frame ownership changed', 'execution context was destroyed',",
        "        'cannot find context with specified id', 'context was destroyed',",
        "    ))",
        "",
        "",
        "async def wait_for_locator(page, selector, action, timeout_ms=30000):",
        "    value = str(selector or '').strip()",
        "    if not value: raise ValueError(action + ' requires a selector')",
        "    deadline = asyncio.get_running_loop().time() + max(float(timeout_ms), 1) / 1000.0",
        "    while True:",
        "        try:",
        "            locator = async_locator_for(page, value).first",
        "            remaining_ms = max(100, int((deadline - asyncio.get_running_loop().time()) * 1000))",
        "            await locator.wait_for(state='attached' if str(action).lower() == 'scrape' else 'visible', timeout=remaining_ms)",
        "            return locator",
        "        except Exception as exc:",
        "            if not is_frame_transition_error(exc) or asyncio.get_running_loop().time() >= deadline:",
        "                raise RuntimeError(action + ' target not found for selector ' + repr(value)) from exc",
        "            await asyncio.sleep(0.10)",
        "",
        "",
        "async def page_body_text(page, timeout_seconds=30):",
        "    deadline = asyncio.get_running_loop().time() + max(float(timeout_seconds), 0.1)",
        "    while True:",
        "        try: return await page.locator('body').inner_text()",
        "        except Exception as exc:",
        "            if not is_frame_transition_error(exc) or asyncio.get_running_loop().time() >= deadline: raise",
        "            await asyncio.sleep(0.10)",
        "",
        "",
        "def page_url(page):",
        "    try: return str(page.url)",
        "    except Exception: return '<unavailable>'",
        "",
        "",
        "def page_description(page):",
        "    try:",
        "        pages = list(page.context.pages)",
        "        page_index = next((index for index, item in enumerate(pages) if item is page), '?')",
        "    except Exception:",
        "        page_index = '?'",
        "    return f'active_page={page_index} url={page_url(page)}'",
        "",
        "",
        "def context_pages(page):",
        "    try: return list(page.context.pages)",
        "    except Exception: return [page]",
        "",
        "",
        "def new_pages(page, before_pages):",
        "    candidates = [candidate for candidate in context_pages(page) if not any(candidate is old for old in before_pages)]",
        "    for candidate in candidates: PAGE_OPENERS[id(candidate)] = page",
        "    return candidates",
        "",
        "",
        "async def destination_matches(page, expected_url, destination_selector):",
        "    if expected_url:",
        "        current_url = page_url(page)",
        "        if current_url != expected_url and expected_url not in current_url:",
        "            return False",
        "    if destination_selector:",
        "        if await async_locator_for(page, destination_selector).first.count() < 1:",
        "            return False",
        "    return True",
        "",
        "",
        "async def wait_for_action_destination(page, before_pages, settings, action):",
        "    expected_url = resolve(settings.get('expected_url'), variables).strip()",
        "    destination_selector = resolve(settings.get('destination_selector'), variables).strip()",
        "    timeout_seconds = min(max(float(settings.get('wait_timeout_seconds', 15)), 0.1), 120.0)",
        "    if not expected_url and not destination_selector:",
        "        candidates = new_pages(page, before_pages)",
        "        return candidates[-1] if candidates else page",
        "    deadline = asyncio.get_running_loop().time() + timeout_seconds",
        "    while True:",
        "        candidates = new_pages(page, before_pages)",
        "        for candidate in [*candidates, page]:",
        "            if await destination_matches(candidate, expected_url, destination_selector):",
        "                return candidate",
        "        if asyncio.get_running_loop().time() >= deadline:",
        "            condition = f'expected_url={expected_url!r} destination_selector={destination_selector!r}'",
        "            raise TimeoutError(f'{action} destination wait timed out ({condition}); {page_description(page)}')",
        "        await asyncio.sleep(0.05)",
        "",
        "",
        "async def show_message_box(page, message):",
        "    await page.evaluate(\"\"\"(text) => { const old = document.getElementById('sui-message-box'); if (old) old.remove(); const overlay = document.createElement('div'); overlay.id = 'sui-message-box'; Object.assign(overlay.style, { position: 'fixed', inset: '0', zIndex: '2147483647', display: 'grid', placeItems: 'center', background: 'rgba(20, 25, 22, .35)' }); const box = document.createElement('div'); Object.assign(box.style, { padding: '24px', borderRadius: '12px', background: '#fff', color: '#17201d' }); const copy = document.createElement('div'); copy.textContent = text; copy.style.marginBottom = '20px'; const ok = document.createElement('button'); ok.id = 'sui-message-box-ok'; ok.textContent = 'OK'; ok.onclick = () => overlay.remove(); box.append(copy, ok); overlay.append(box); document.body.append(overlay); }\"\"\", message)",
        "",
        "",
        "async def evaluate_condition(left, operator, right, variables):",
        "    left_text = resolve(left, variables).strip(); right_text = resolve(right, variables).strip()",
        "    try: left_value = float(left_text); right_value = float(right_text)",
        "    except (TypeError, ValueError): left_value = left_text; right_value = right_text",
        "    if operator == '=': return left_value == right_value",
        "    if operator == '<>': return left_value != right_value",
        "    if operator == '<': return left_value < right_value",
        "    if operator == '<=': return left_value <= right_value",
        "    if operator == '>': return left_value > right_value",
        "    if operator == '>=': return left_value >= right_value",
        "    if operator == 'in': return left_text.lower() in {item.strip().lower() for item in right_text.split(',')}",
        "    raise ValueError('Unsupported condition operator: ' + str(operator))",
        "",
        "",
        f"selected_step = json.loads({step_literal})",
        f"variables = json.loads({variables_literal})",
        f"ORCHESTRATOR_PROJECT_ID = {project_id_literal}",
        f"CALL_STACK = [{recipe_id_literal}]",
        "PAGE_OPENERS = {}",
        "PAGE_SOURCES = {}",
        "",
        "",
        "def load_called_recipe(settings):",
        "    target_id = str(settings.get('recipe_id') or '').strip()",
        "    target_name = str(settings.get('recipe_name') or '').strip()",
        "    connection = sqlite3.connect(WRITER_DB_PATH)",
        "    try:",
        "        if target_id:",
        "            row = connection.execute(\"SELECT rv.definition_json FROM recipe_versions rv JOIN recipes r ON r.id = rv.recipe_id WHERE r.project_id = ? AND r.id = ? ORDER BY rv.version DESC LIMIT 1\", (ORCHESTRATOR_PROJECT_ID, target_id)).fetchone()",
        "        else:",
        "            row = connection.execute(\"SELECT rv.definition_json FROM recipe_versions rv JOIN recipes r ON r.id = rv.recipe_id WHERE r.project_id = ? AND r.name = ? ORDER BY rv.version DESC LIMIT 1\", (ORCHESTRATOR_PROJECT_ID, target_name)).fetchone()",
        "    finally:",
        "        connection.close()",
        "    if not row: raise ValueError('Target recipe was not found in this project')",
        "    return json.loads(row[0])",
        "",
        "",
        "async def run_called_recipe(page, settings, capture_lock):",
        "    target = load_called_recipe(settings)",
        "    target_id = str(target.get('recipe_id') or '')",
        "    if target_id in CALL_STACK: raise ValueError('Recursive recipe calls are not allowed')",
        "    previous_variables = dict(variables)",
        "    variables.clear()",
        "    variables.update({item.get('name'): item.get('value', '') for item in target.get('variables', []) if item.get('name')})",
        "    CALL_STACK.append(target_id)",
        "    try:",
        "        return await execute_steps(page, target.get('steps') or [], capture_lock)",
        "    finally:",
        "        CALL_STACK.pop()",
        "        variables.clear()",
        "        variables.update(previous_variables)",
        "",
        "",
        "async def execute_step(page, step, capture_lock):",
        "    if step.get('disabled'): return page",
        "    settings = step.get('settings') or {}; step_type = step.get('type')",
        "    if step_type == 'navigate_to_website':",
        "        await human_pause(0.35); await page.goto(resolve(settings.get('url'), variables), wait_until='domcontentloaded'); await human_pause(0.65)",
        "    elif step_type == 'navigate':",
        "        action = settings.get('action', 'back'); await human_pause(0.30)",
        "        if action == 'back':",
        "            opener = PAGE_OPENERS.get(id(page))",
        "            source_url = PAGE_SOURCES.get(id(page))",
        "            previous_url = page_url(page)",
        "            try:",
        "                await page.go_back(wait_until='commit')",
        "            except Exception:",
        "                if not opener: raise",
        "                try: await page.close()",
        "                except Exception: pass",
        "                page = opener",
        "            else:",
        "                if opener and (page_url(page) == previous_url or page_url(page).startswith('about:')):",
        "                    try: await page.close()",
        "                    except Exception: pass",
        "                    page = opener",
        "                elif source_url and page_url(page) == previous_url and source_url != previous_url:",
        "                    await page.goto(source_url, wait_until='commit')",
        "        elif action == 'forward': await page.go_forward(wait_until='commit')",
        "        elif action == 'refresh': await page.reload(wait_until='commit')",
        "        else: raise ValueError('Unsupported Navigate action: ' + str(action))",
        "        await human_pause(0.45)",
        "    elif step_type == 'click':",
        "        source_url = page_url(page); before_pages = context_pages(page); await human_pause(0.30)",
        "        target = await wait_for_locator(page, resolve(settings.get('selector'), variables), 'Click')",
        "        try:",
        "            await target.click()",
        "        except Exception as exc:",
        "            navigated = page_url(page) != source_url or bool(new_pages(page, before_pages))",
        "            if not navigated: raise",
        "            print(f\"SUI_LOG:Click raised after navigation; continuing; command_id={step.get('command_id', '')}; error={exc}\", flush=True)",
        "        page = await wait_for_action_destination(page, before_pages, settings, 'Click'); PAGE_SOURCES[id(page)] = source_url; await human_pause(0.35)",
        "    elif step_type == 'fill':",
        "        await human_pause(0.30); await (await wait_for_locator(page, resolve(settings.get('selector'), variables), 'Fill')).fill(resolve(settings.get('value'), variables)); await human_pause(0.35)",
        "    elif step_type == 'delay':",
        "        if settings.get('until_load_completes'): await wait_for_load_completion(page)",
        "        else: await asyncio.sleep(float(settings.get('seconds', 1)))",
        "    elif step_type == 'keypress':",
        "        source_url = page_url(page); before_pages = context_pages(page); await human_pause(0.25); await page.keyboard.press(str(settings.get('key') or 'Enter')); page = await wait_for_action_destination(page, before_pages, settings, 'Keypress'); PAGE_SOURCES[id(page)] = source_url; await human_pause(0.25)",
        "    elif step_type == 'assign':",
        "        variables[settings.get('variable')] = resolve(settings.get('value'), variables); await human_pause(0.25)",
        "    elif step_type == 'scrape':",
        "        variables[settings.get('variable')] = await (await wait_for_locator(page, resolve(settings.get('selector'), variables), 'Scrape')).inner_text(); await human_pause(0.30)",
        "    elif step_type == 'pattern_matcher':",
        "        source = await page_body_text(page); combined_pattern = '|'.join('(?:' + str(pattern) + ')' for pattern in (settings.get('patterns') or []))",
        "        matches = [candidate.group(0) for candidate in re.finditer(combined_pattern, source, re.IGNORECASE)] if combined_pattern else []",
        "        variables[settings.get('variable')] = ' | '.join(matches) if matches else 'NO CONTACTS FOUND'; await human_pause(0.30)",
        "    elif step_type == 'smartie':",
        "        smartie_system = resolve(settings.get('system_prompt'), variables); smartie_user = resolve(settings.get('user_prompt'), variables); variables[settings.get('output_variable')] = await asyncio.to_thread(run_smartie, settings.get('model', 'gemini-2.5-flash'), smartie_system, smartie_user, settings.get('output_type', 'text'), settings.get('schema') or []); await human_pause(0.30)",
        "    elif step_type == 'message_box':",
        "        await show_message_box(page, resolve(settings.get('message'), variables))",
        f"        if {json.dumps(mode)} == 'headless': await page.locator('#sui-message-box-ok').click()",
        "        else: await page.wait_for_function(\"() => !document.getElementById('sui-message-box')\")",
        "    elif step_type == 'writer':",
        "        writer_columns = [dict(column, value=resolve(column.get('value'), variables)) for column in (settings.get('columns') or [{'name': 'value', 'type': 'TEXT', 'value': settings.get('value', ''), 'write': True}])]; current_row = variables.get(settings.get('current_row')) if settings.get('current_row') else None; await asyncio.to_thread(write_values_to_sqlite, settings.get('table_name'), writer_columns, settings.get('semantic_duplicate_removal', False), settings.get('duplicate_threshold', 0.88), current_row); await human_pause(0.25)",
        "    elif step_type in {'call_recipe', 'invoke_recipe'}:",
        "        page = await run_called_recipe(page, settings, capture_lock); await human_pause(0.25)",
        "    elif step_type == 'check_element_exists':",
        "        selector = resolve(settings.get('selector'), variables); locator = async_locator_for(page, selector)",
        "        try:",
        "            await locator.wait_for(state='attached', timeout=int(float(settings.get('timeout_seconds', 5)) * 1000)); exists = True",
        "        except Exception:",
        "            exists = False",
        "        page = await execute_steps(page, (step.get('branches') or {}).get('exists' if exists else 'not_exists', []), capture_lock)",
        "    elif step_type == 'check_text_exists':",
        "        expected = resolve(settings.get('text'), variables); timeout = float(settings.get('timeout_seconds', 5)); deadline = asyncio.get_running_loop().time() + timeout; exists = False",
        "        while asyncio.get_running_loop().time() < deadline:",
        "            body = await page_body_text(page, max(0.1, deadline - asyncio.get_running_loop().time())); exists = expected.lower() in body.lower() if settings.get('case_insensitive', True) else expected in body",
        "            if exists: break",
        "            await asyncio.sleep(0.2)",
        "        page = await execute_steps(page, (step.get('branches') or {}).get('exists' if exists else 'not_exists', []), capture_lock)",
        "    elif step_type == 'if':",
        "        matched = await evaluate_condition(settings.get('left'), settings.get('operator', '='), settings.get('right'), variables); page = await execute_steps(page, (step.get('branches') or {}).get('then' if matched else 'else', []), capture_lock)",
        "    elif step_type == 'for':",
        "        if settings.get('source') == 'table':",
        "            table_name = resolve(settings.get('table_name'), variables); table_mode = settings.get('table_mode') or 'row'; iterator = settings.get('iterator') or ('row' if table_mode == 'row' else 'value'); column_name = resolve(settings.get('column_name'), variables) if table_mode == 'column' else None; loop_values = await asyncio.to_thread(read_sqlite_table, table_name, column_name); loop_iterable = enumerate(loop_values)",
        "        else:",
        "            loop_count = resolve_loop_range(settings.get('range_value'), variables); loop_start = int(settings.get('start_value', 0)); iterator = settings.get('iterator') or 'i'",
        "            if loop_count < 0: raise ValueError('For loop range must not be negative')",
        "            loop_iterable = ((loop_index, loop_index) for loop_index in range(loop_start, loop_start + loop_count))",
        "        for loop_index, loop_value in loop_iterable:",
        "            variables[iterator] = loop_value.get(column_name) if settings.get('source') == 'table' and table_mode == 'column' else loop_value",
        "            variables['currentRow'] = dict(loop_value, __sui_table=table_name) if settings.get('source') == 'table' and isinstance(loop_value, dict) else variables.get('currentRow')",
        "            print(f\"SUI_LOG:For loop iteration {loop_index} started\", flush=True)",
        "            try:",
        "                page = await execute_steps(page, (step.get('branches') or {}).get('body', []), capture_lock)",
        "            except Exception as exc:",
        "                print(f\"SUI_LOG:For loop iteration {loop_index} failed; {page_description(page)}; error={type(exc).__name__}: {exc}\", flush=True)",
        "                raise",
        "            print(f\"SUI_LOG:For loop iteration {loop_index} completed\", flush=True)",
        "    elif step_type == 'use_js':",
        "        source_url = page_url(page); before_pages = context_pages(page); args = [variables.get(name, '') for name in (settings.get('inputs') or [])]; result = await page.evaluate(USE_JS_RUNNER, {'body': settings.get('code', ''), 'args': args}); page = await wait_for_action_destination(page, before_pages, settings, 'Use JS'); PAGE_SOURCES[id(page)] = source_url; output_name = settings.get('output')",
        "        if output_name: variables[output_name] = result",
        "    else:",
        "        raise ValueError('Unsupported activity: ' + str(step_type))",
        "    return page",
        "",
        "",
        "async def execute_steps(page, steps, capture_lock):",
        "    for step in steps:",
        "        if step.get('disabled'):",
        "            print(f\"SUI_LOG:Step skipped: {step.get('id', 'step')} ({step.get('type')})\", flush=True)",
        "            continue",
        "        step_id = step.get('id', 'step')",
        "        print(f\"SUI_LOG:Step started: {step_id} ({step.get('type')})\", flush=True)",
        "        try:",
        "            page = await execute_step(page, step, capture_lock)",
        "        except Exception as exc:",
        "            print(f\"SUI_LOG:Step failed: {step_id} ({step.get('type')}); {page_description(page)}; error={type(exc).__name__}: {exc}\", flush=True)",
        "            raise",
        "        await emit_frame(page, capture_lock)",
        "        print(f\"SUI_LOG:Step completed: {step_id} ({step.get('type')})\", flush=True)",
        "    return page",
        "",
        "",
        "async def inspect_at(page, x, y):",
        "    return await page.evaluate(\"\"\"({x, y}) => { const target = document.elementFromPoint(x, y); if (!target) return null; const node = target.closest('button,a,input,textarea,select,[role=button],[role=link]') || target; const esc = (value) => CSS.escape(value); const selectors = []; const add = (kind, selector) => { if (selector && !selectors.some((item) => item.selector === selector)) selectors.push({ kind, selector, unique: document.querySelectorAll(selector).length === 1, matches: document.querySelectorAll(selector).length }); }; if (node.id) add('ID', '#' + esc(node.id)); if (node.getAttribute('data-testid')) add('Test ID', '[data-testid=\"' + esc(node.getAttribute('data-testid')) + '\"]'); if (node.getAttribute('name')) add('Name', '[name=\"' + esc(node.getAttribute('name')) + '\"]'); add('CSS', node.tagName.toLowerCase()); return { selector: selectors[0]?.selector || node.tagName.toLowerCase(), selectorCandidates: selectors, metadata: { tag: node.tagName.toLowerCase(), text: String(node.innerText || '').trim().slice(0, 120) } }; }\"\"\", {x, y})",
        "",
        "async def inspect_at_safe(page, x, y):",
        "    return await page.evaluate(\"\"\"({x, y}) => { const target = document.elementFromPoint(x, y) || document.body; const node = target.closest('button,a,input,textarea,select,[role=button],[role=link]') || target; const esc = (value) => { try { return CSS.escape(String(value)); } catch { return String(value).replace(/[^A-Za-z0-9_-]/g, '\\\\$&'); } }; const selectors = []; const add = (kind, selector) => { if (!selector || selectors.some((item) => item.selector === selector)) return; try { const matches = document.querySelectorAll(selector).length; selectors.push({ kind, selector, unique: matches === 1, matches }); } catch {} }; if (node.id) add('ID', '#' + esc(node.id)); if (node.getAttribute('data-testid')) add('Test ID', '[data-testid=\"' + esc(node.getAttribute('data-testid')) + '\"]'); if (node.getAttribute('name')) add('Name', '[name=\"' + esc(node.getAttribute('name')) + '\"]'); add('CSS', node.tagName.toLowerCase()); return { selector: selectors[0]?.selector || node.tagName.toLowerCase(), selectorCandidates: selectors, metadata: { tag: node.tagName.toLowerCase(), text: String(node.innerText || '').trim().slice(0, 120) } }; }\"\"\", {x, y})",
        "",
        "def json_safe(value):",
        "    if isinstance(value, dict): return {str(key): json_safe(item) for key, item in value.items()}",
        "    if isinstance(value, (list, tuple, set)): return [json_safe(item) for item in value]",
        "    return value",
        "",
        "def inspect_wire_text(value):",
        "    if isinstance(value, str):",
        "        value = json.loads(value)",
        "    return json.dumps(",
        "        json_safe(value),",
        "        ensure_ascii=True,",
        "        separators=(\",\", \":\"),",
        "    )",
        "",
        "async def inspect_at_wire(page, x, y):",
        "    return await page.evaluate(\"\"\"({x, y}) => { const hovered = document.elementFromPoint(x, y); if (!hovered) return JSON.stringify({ error: 'No element at the selected point', selectorCandidates: [] }); const target = hovered.closest('button,a,input,textarea,select,[role=button],[role=link]') || hovered; const escape = (value) => (window.CSS && CSS.escape ? CSS.escape(String(value)) : String(value).replace(/[^a-zA-Z0-9_-]/g, '\\\\$&')); const parts = []; const targetClasses = Array.from(target.classList || []).filter(Boolean).slice(0, 2); let node = target; while (node && node.nodeType === 1 && node !== document.body) { let part = node.tagName.toLowerCase(); if (node.id) { parts.unshift('#' + escape(node.id)); break; } const classes = Array.from(node.classList || []).filter(Boolean).slice(0, 2); if (classes.length) part += classes.map((name) => '.' + escape(name)).join(''); const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((item) => item.tagName === node.tagName) : []; if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')'; parts.unshift(part); node = node.parentElement; } const pathSelector = parts.join(' > '); const text = String(target.innerText || target.getAttribute('aria-label') || target.value || '').replace(/\\s+/g, ' ').trim().slice(0, 120); const candidates = []; const addCandidate = (kind, value) => { if (!value || candidates.some((candidate) => candidate.selector === value)) return; let matches = null; try { matches = document.querySelectorAll(value).length; } catch (_) {} candidates.push({ selector: value, kind, unique: matches === 1, matches }); }; if (target.id) addCandidate('ID', '#' + escape(target.id)); const testId = target.getAttribute('data-testid'); if (testId) addCandidate('Test ID', '[data-testid=\"' + escape(testId) + '\"]'); const name = target.getAttribute('name'); if (name) addCandidate('Name', '[name=\"' + escape(name) + '\"]'); const ariaLabel = target.getAttribute('aria-label'); if (ariaLabel) addCandidate('ARIA label', '[aria-label=\"' + escape(ariaLabel) + '\"]'); const role = target.getAttribute('role'); if (role) addCandidate('Role', '[role=\"' + escape(role) + '\"]'); if (targetClasses.length) addCandidate('CSS', target.tagName.toLowerCase() + targetClasses.map((name) => '.' + escape(name)).join('')); addCandidate('Path', pathSelector); const rect = target.getBoundingClientRect(); return JSON.stringify({ selector: candidates[0]?.selector || pathSelector, selectorCandidates: candidates, metadata: { tag: target.tagName.toLowerCase(), text, role: target.getAttribute('role') || '', ariaLabel: target.getAttribute('aria-label') || '' }, boundingRect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } }); }\"\"\", {x, y})",
        "",
        "async def inspect_picker_at(page, x, y):",
        r'''    return await page.evaluate("""({x, y}) => { const hovered = document.elementFromPoint(x, y); if (!hovered) return JSON.stringify({ error: "No element at the selected point", selectorCandidates: [] }); const target = hovered.closest("button,a,input,textarea,select,label,[role=button],[role=link],[role=checkbox],[role=tab],[contenteditable=\\"true\\"]") || hovered; const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim(); const escapeCss = (value) => (window.CSS && CSS.escape ? CSS.escape(String(value)) : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\\\$&")); const quoteAttr = (value) => String(value || "").replace(/\\\\/g, "\\\\\\\\").replace(/\\\"/g, "\\\\\\\""); const candidates = []; const add = (kind, selector, matchSelector = selector) => { if (!selector || candidates.some((candidate) => candidate.selector === selector)) return; let matches = null; try { matches = document.querySelectorAll(matchSelector).length; } catch (_) {} candidates.push({ kind, selector, unique: matches === 1, matches }); }; const tag = target.tagName.toLowerCase(); const attr = (name, value, prefix = tag) => prefix + "[" + name + "=\\\"" + quoteAttr(value) + "\\\"]"; const text = normalize(target.innerText || target.textContent || target.value || target.getAttribute("aria-label") || "").slice(0, 120); if (target.id) add("ID", "#" + escapeCss(target.id)); for (const dataName of ["data-testid", "data-test", "data-qa"]) { const dataValue = target.getAttribute(dataName); if (dataValue) add("Test attribute", attr(dataName, dataValue)); } const name = target.getAttribute("name"); if (name) add("Name", attr("name", name)); const ariaLabel = target.getAttribute("aria-label"); if (ariaLabel) add("ARIA label", attr("aria-label", ariaLabel)); const placeholder = target.getAttribute("placeholder"); if (placeholder) add("Placeholder", attr("placeholder", placeholder)); const title = target.getAttribute("title"); if (title) add("Title", attr("title", title)); const type = target.getAttribute("type"); if (type) add("Type", attr("type", type)); const role = target.getAttribute("role"); if (role) { const accessibleName = normalize(ariaLabel || name || target.getAttribute("placeholder") || text); const roleSelector = accessibleName ? "role=" + role + "[name='" + accessibleName.replace(/'/g, "\\\\'") + "']" : "role=" + role; const roleMatch = "[role=\\\"" + quoteAttr(role) + "\\\"]"; add("Role", roleSelector, roleMatch); add("Role attribute", roleMatch); } const classes = Array.from(target.classList || []).filter(Boolean).slice(0, 3); if (classes.length) add("CSS", tag + classes.map((className) => "." + escapeCss(className)).join("")); if (target.matches("[contenteditable=\\\"true\\\"]")) add("Contenteditable", "[contenteditable=\\\"true\\\"]"); if (text && ["button", "a", "label"].includes(tag) && text.length <= 80 && text.split(/\\s+/).filter(Boolean).length <= 12) add("Text", tag + ":has-text('" + text.replace(/'/g, "\\\\'") + "')"); const parts = []; let node = target; while (node && node.nodeType === 1 && node !== document.body) { let part = node.tagName.toLowerCase(); if (node.id) { parts.unshift("#" + escapeCss(node.id)); break; } const nodeClasses = Array.from(node.classList || []).filter(Boolean).slice(0, 2); if (nodeClasses.length) part += nodeClasses.map((className) => "." + escapeCss(className)).join(""); const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((item) => item.tagName === node.tagName) : []; if (siblings.length > 1) part += ":nth-of-type(" + (siblings.indexOf(node) + 1) + ")"; parts.unshift(part); node = node.parentElement; } add("Path", parts.join(" > ")); const rect = target.getBoundingClientRect(); return JSON.stringify({ selector: candidates[0]?.selector || tag, selectorCandidates: candidates, metadata: { tag, text, role: role || "", ariaLabel: ariaLabel || "", name: name || "", placeholder: target.getAttribute("placeholder") || "", type: target.getAttribute("type") || "" }, boundingRect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } }); }""", {x, y})''',
        "",
        "",
        "def locator_for(page, selector):",
        "    value = str(selector or '').strip()",
        "    if value.startswith('role='):",
        "        role_query = value[5:].strip(); name = None",
        "        if '[name=' in role_query:",
        "            role_query, name_part = role_query.split('[name=', 1); name = name_part.rstrip(']').strip(\"'\\\"\")",
        "        get_by_role = getattr(page, 'get_by_role', None)",
        "        if get_by_role:",
        "            return get_by_role(role_query.strip(), name=name) if name else get_by_role(role_query.strip())",
        "        role_value = role_query.strip()",
        "        if name: return page.locator('[role=\\\"' + role_value + '\\\"]')",
        "        return page.locator('[role=\\\"' + role_value + '\\\"]')",
        "    return page.locator(value)",
        "",
        "async def picker_point(page, x, y, source_width, source_height):",
        "    if not source_width or not source_height: return x, y",
        "    viewport = await page.evaluate(\"() => ({ width: window.innerWidth, height: window.innerHeight })\")",
        "    return x * float(viewport.get('width') or source_width) / float(source_width), y * float(viewport.get('height') or source_height) / float(source_height)",
        "",
        "async def highlight_picker_target(page, x, y, source_width, source_height):",
        "    await page.evaluate(\"\"\"({x, y, sourceWidth, sourceHeight}) => { const id = '__sui_picker_highlight__'; const current = document.getElementById(id); const point = sourceWidth && sourceHeight ? { x: x * window.innerWidth / sourceWidth, y: y * window.innerHeight / sourceHeight } : { x, y }; const hovered = document.elementFromPoint(point.x, point.y); const target = hovered && !hovered.closest('#' + id) ? (hovered.closest('button,a,input,textarea,select,[role=button],[role=link]') || hovered) : null; if (!target) { if (current) current.style.display = 'none'; return; } let overlay = current; if (!overlay) { overlay = document.createElement('div'); overlay.id = id; overlay.setAttribute('aria-hidden', 'true'); Object.assign(overlay.style, { position: 'fixed', pointerEvents: 'none', zIndex: '2147483647', border: '3px solid #FF5B35', background: 'rgba(255, 91, 53, 0.14)', boxShadow: '0 0 0 1px rgba(255,255,255,0.95), 0 0 18px rgba(255,91,53,0.85)', borderRadius: '3px', transition: 'none' }); document.documentElement.appendChild(overlay); } const rect = target.getBoundingClientRect(); Object.assign(overlay.style, { display: 'block', left: rect.left + 'px', top: rect.top + 'px', width: rect.width + 'px', height: rect.height + 'px' }); }\"\"\", {x, y, sourceWidth, sourceHeight})",
        "",
        "async def clear_picker_target(page):",
        "    await page.evaluate(\"\"\"() => { const overlay = document.getElementById('__sui_picker_highlight__'); if (overlay) overlay.remove(); }\"\"\")",
        "",
        "async def execute_command(session, command, capture_lock):",
        "    command_type = command.get('type')",
        "    page = session['page']",
        "    try:",
        "        action_result = None",
        "        if command_type == 'run_step':",
        "            step = command.get('step') or {}; page = await execute_step(page, step, capture_lock); action_result = {'status': 'completed', 'action': step.get('type'), 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'delay':",
        "            if command.get('until_load_completes'): await wait_for_load_completion(page)",
        "            else: await asyncio.sleep(float(command.get('seconds') or 0))",
        "            action_result = {'status': 'completed', 'action': 'delay', 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'assign': variables[str(command.get('variable') or '')] = str(command.get('value') or ''); action_result = {'status': 'completed', 'action': 'assign', 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'navigate': await human_pause(0.35); await page.goto(str(command.get('url') or ''), wait_until='domcontentloaded'); await human_pause(0.65); action_result = {'status': 'completed', 'action': 'navigate', 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'text': await page.keyboard.insert_text(str(command.get('text', '')))"
        ,
        "        elif command_type == 'key': await page.keyboard.press(str(command.get('key', '')))"
        ,
        "        elif command_type == 'click': await page.mouse.click(float(command.get('x', 0)), float(command.get('y', 0)))"
        ,
        "        elif command_type == 'selector_fill': await (await wait_for_locator(page, str(command.get('selector') or ''), 'Fill')).fill(str(command.get('value') or '')); action_result = {'status': 'completed', 'action': 'fill', 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'selector_scrape': action_result = {'status': 'completed', 'action': 'scrape', 'scraped_value': await (await wait_for_locator(page, str(command.get('selector') or ''), 'Scrape')).inner_text(), 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'selector_check': action_result = {'status': 'completed', 'action': 'check_element_exists', 'exists': await locator_for(page, str(command.get('selector') or '')).count() > 0, 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'selector_click': await (await wait_for_locator(page, str(command.get('selector') or ''), 'Click')).click(); action_result = {'status': 'completed', 'action': 'click', 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'message_box': await show_message_box(page, str(command.get('message') or '')); action_result = {'status': 'completed', 'action': 'message_box', 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'inspect_move': await highlight_picker_target(page, float(command.get('x', 0)), float(command.get('y', 0)), float(command.get('source_width', 0)), float(command.get('source_height', 0)))"
        ,
        "        elif command_type == 'clear_picker_overlay': await clear_picker_target(page)"
        ,
        "        elif command_type == 'inspect': inspect_x, inspect_y = await picker_point(page, float(command.get('x', 0)), float(command.get('y', 0)), float(command.get('source_width', 0)), float(command.get('source_height', 0))); print('SUI_INSPECT:' + inspect_wire_text(await inspect_picker_at(page, inspect_x, inspect_y)), flush=True)"
        ,
        "        else: return"
        ,
        "        session['page'] = page",
        "        await emit_frame(page, capture_lock)"
        ,
        "        if action_result is not None: print('SUI_ACTION_COMPLETE:' + json.dumps(action_result), flush=True)"
        ,
        "        elif command_type not in {'run_step', 'inspect_move', 'clear_picker_overlay', 'inspect'}: print('SUI_ACTION_COMPLETE:' + json.dumps({'status': 'completed', 'action': command_type, 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}), flush=True)"
        ,
        "    except asyncio.CancelledError:",
        "        print('SUI_ACTION_COMPLETE:' + json.dumps({'status': 'cancelled', 'error': 'Superseded by a newer command', 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}), flush=True)",
        "        raise",
        "    except Exception as exc:",
        "        session['page'] = page",
        "        failed_step = command.get('step') or {}",
        "        print(f\"SUI_LOG:Step failed: {failed_step.get('id', command_type)} ({failed_step.get('type', command_type)}); {page_description(page)}; command_id={command.get('command_id', '')}; error={exc}\", flush=True)",
        "        if command_type in {'run_step', 'delay', 'assign', 'navigate', 'selector_click', 'selector_fill', 'selector_scrape', 'selector_check', 'message_box', 'inspect', 'inspect_move', 'clear_picker_overlay'}: print('SUI_ACTION_COMPLETE:' + json.dumps({'status': 'failed', 'error': str(exc), 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}), flush=True)",
        "    return page",
        "",
        "",
        "async def command_loop(session, capture_lock):",
        "    page = session['page']",
        "    current_task = None",
        "    executable_types = {'run_step', 'delay', 'assign', 'navigate', 'selector_click', 'selector_fill', 'selector_scrape', 'selector_check', 'message_box'}",
        "    while True:",
        "        line = await asyncio.to_thread(sys.stdin.readline)",
        "        if not line:",
        "            if current_task and not current_task.done(): current_task.cancel(); await asyncio.gather(current_task, return_exceptions=True)",
        "            return",
        "        try: command = json.loads(line)",
        "        except json.JSONDecodeError: continue",
        "        if command.get('type') == 'cancel_command':",
        "            if current_task and not current_task.done(): current_task.cancel(); await asyncio.gather(current_task, return_exceptions=True)",
        "            current_task = None",
        "            continue",
        "        if current_task and current_task.done():",
        "            result = await asyncio.gather(current_task, return_exceptions=True)",
        "            if result and not isinstance(result[0], BaseException): page = result[0]; session['page'] = page",
        "            current_task = None",
        "        if current_task and command.get('type') in executable_types:",
        "            current_task.cancel()",
        "            await asyncio.gather(current_task, return_exceptions=True)",
        "        current_task = asyncio.create_task(execute_command(session, command, capture_lock))",
        "",
        "",
    ])
    _generated_window_support(lines)
    lines.extend(
        [
            "",
            "",
            "async def run():",
            "    browser = None",
            "    async with async_playwright() as playwright:",
            f"        foreground_window = preserve_foreground_window() if {json.dumps(mode)} == 'headful' else None",
            "        before_handles = visible_window_handles()",
            f"        browser = await playwright.chromium.launch(headless={json.dumps(mode)} == 'headless')",
            "        page = await browser.new_page(no_viewport=True)",
            f"        maximize_browser_window(before_handles) if {json.dumps(mode)} == 'headful' else None",
            "        restore_foreground_window(foreground_window)",
            "        stop_event = asyncio.Event()",
            "        capture_lock = asyncio.Lock()",
            "        session = {'page': page}",
            "        frame_task = None",
            "        command_task = None",
            "        try:",
            "            await emit_frame(page, capture_lock)",
            "            frame_task = asyncio.create_task(stream_frames(session, stop_event, capture_lock))",
            "            print('SUI_BROWSER_READY', flush=True)",
            "            try:",
            "                page = await execute_steps(page, [selected_step], capture_lock)",
            "                session['page'] = page",
            "                print('SUI_ACTION_COMPLETE:' + json.dumps({'status': 'completed', 'action': selected_step.get('type'), 'command_id': selected_step.get('command_id')}), flush=True)",
            "                print('SUI_SESSION_READY', flush=True)",
            "            except Exception as exc:",
            "                print('SUI_ACTION_COMPLETE:' + json.dumps({'status': 'failed', 'error': str(exc), 'action': selected_step.get('type'), 'command_id': selected_step.get('command_id')}), flush=True)",
            "                print('SUI_LOG:Step failed: ' + str(exc), flush=True)",
            "                print('SUI_SESSION_READY', flush=True)",
            "            command_task = asyncio.create_task(command_loop(session, capture_lock))",
            "            done_tasks, pending_tasks = await asyncio.wait({frame_task, command_task}, return_when=asyncio.FIRST_COMPLETED)",
            "            for task in pending_tasks: task.cancel()",
            "            await asyncio.gather(*pending_tasks, return_exceptions=True)",
            "        finally:",
            "            stop_event.set()",
            "            if frame_task: await frame_task",
            "            if command_task: command_task.cancel()",
            "            try:",
            "                if browser: await browser.close()",
            "            except Exception: pass",
            "",
            "",
            "async def stream_frames(session, stop_event, capture_lock):",
            "    while not stop_event.is_set():",
            "        try:",
            "            page = session['page']",
            "            if page.is_closed(): print('SUI_BROWSER_CLOSED:page is closed', flush=True); return",
            "            await emit_frame(page, capture_lock)",
            "        except Exception as exc: print('SUI_FRAME_ERROR:' + str(exc), flush=True); await asyncio.sleep(0.25)",
            "        await asyncio.sleep(0.25)",
            "",
            "",
            "if __name__ == '__main__':",
            "    asyncio.run(run())",
            "",
        ]
    )
    source = "\n".join(lines)
    # Python treats {x, y} as a set. Rustwright expects evaluate arguments
    # to be JSON objects, matching the picker implementation from 117e289.
    source = source.replace(", {x, y})", ', {"x": x, "y": y})')
    return source.replace(", {x, y, sourceWidth, sourceHeight})", ', {"x": x, "y": y, "sourceWidth": source_width, "sourceHeight": source_height})')


def generate_step_python(definition: dict[str, Any]) -> str:
    return _generate_recursive_step_python(definition)
    step = definition["steps"][0]
    chrome_mode = definition.get("mode") or "headful"
    if step["type"] == "navigate_to_website":
        action_lines = [
            "            await human_pause(0.35)",
            f"            await page.goto({json.dumps(step['settings']['url'])}, wait_until=\"domcontentloaded\")",
            "            await human_pause(0.65)",
        ]
        log_line = f"            print(\"SUI_LOG:Navigated: {step['id']}\", flush=True)"
    elif step["type"] == "fill":
        action_lines = [
            "            await human_pause(0.30)",
            f"            await page.locator({json.dumps(step['settings']['selector'])}).first.fill({json.dumps(step['settings'].get('value', ''))})",
            "            await human_pause(0.35)",
        ]
        log_line = f"            print(\"SUI_LOG:Filled: {step['id']}\", flush=True)"
    elif step["type"] == "delay":
        action_lines = [
            f"            await asyncio.sleep({json.dumps(step['settings'].get('seconds', 1))})",
        ]
        log_line = f"            print(\"SUI_LOG:Delayed: {step['id']}\", flush=True)"
    elif step["type"] == "assign":
        action_lines = [
            f"            variables[{json.dumps(step['settings']['variable'])}] = {json.dumps(step['settings'].get('value', ''))}",
            "            await human_pause(0.25)",
        ]
        log_line = f"            print(\"SUI_LOG:Assigned: {step['id']}\", flush=True)"
    elif step["type"] == "scrape":
        action_lines = [
            f"            variables[{json.dumps(step['settings']['variable'])}] = await page.locator({json.dumps(step['settings']['selector'])}).first.inner_text()",
            "            await human_pause(0.30)",
        ]
        log_line = f"            print(\"SUI_LOG:Scraped: {step['id']}\", flush=True)"
    elif step["type"] == "message_box":
        action_lines = [
            f"            await show_message_box(page, {json.dumps(step['settings'].get('message', ''))})",
            "            await emit_frame(page, capture_lock)",
        ]
        log_line = f"            print(\"SUI_LOG:Message box shown: {step['id']}\", flush=True)"
    else:
        action_lines = [
            "            await human_pause(0.30)",
            f"            await page.locator({json.dumps(step['settings']['selector'])}).first.click()",
            "            await human_pause(0.35)",
        ]
        log_line = f"            print(\"SUI_LOG:Clicked: {step['id']}\", flush=True)"
    return "\n".join(
        [
            "import asyncio",
            "import base64",
            "import ctypes",
            "import json",
            "import os",
            "import sys",
            "import time",
            "from rustwright.async_api import async_playwright",
            "",
            "",
            "async def human_pause(seconds):",
            "    await asyncio.sleep(seconds)",
            "",
            "",
            f"variables = {json.dumps({variable['name']: variable.get('value', '') for variable in definition.get('variables', [])})}",
            "",
            "",
            "async def show_message_box(page, message):",
            "    await page.evaluate(\"\"\"(text) => {",
            "        const old = document.getElementById('sui-message-box');",
            "        if (old) old.remove();",
            "        const overlay = document.createElement('div');",
            "        overlay.id = 'sui-message-box';",
            "        Object.assign(overlay.style, { position: 'fixed', inset: '0', zIndex: '2147483647', display: 'grid', placeItems: 'center', background: 'rgba(20, 25, 22, .35)', fontFamily: 'system-ui, sans-serif' });",
            "        const box = document.createElement('div');",
            "        Object.assign(box.style, { width: 'min(420px, calc(100vw - 40px))', padding: '24px', borderRadius: '12px', background: '#fff', color: '#17201d', boxShadow: '0 18px 50px rgba(0,0,0,.28)' });",
            "        const copy = document.createElement('div');",
            "        copy.textContent = text;",
            "        copy.style.cssText = 'white-space: pre-wrap; line-height: 1.5; margin-bottom: 20px;';",
            "        const ok = document.createElement('button');",
            "        ok.id = 'sui-message-box-ok';",
            "        ok.textContent = 'OK';",
            "        ok.style.cssText = 'border: 0; border-radius: 7px; padding: 9px 18px; background: #FF5B35; color: #fff; font-weight: 700; cursor: pointer;';",
            "        ok.addEventListener('click', () => overlay.remove());",
            "        box.append(copy, ok);",
            "        overlay.append(box);",
            "        document.body.append(overlay);",
            "    }\"\"\", message)",
            "",
            "",
            "async def emit_frame(page, capture_lock):",
            "    async with capture_lock:",
            "        frame = await page.screenshot(type=\"jpeg\", quality=70)",
            "        print(\"SUI_FRAME:\" + base64.b64encode(frame).decode(\"ascii\"), flush=True)",
            "",
            "",
            "def preserve_foreground_window():",
            "    if os.name != \"nt\": return None",
            "    return ctypes.windll.user32.GetForegroundWindow()",
            "",
            "",
            "def restore_foreground_window(hwnd):",
            "    if hwnd and os.name == \"nt\": ctypes.windll.user32.SetForegroundWindow(hwnd)",
            "",
            "",
            "def visible_window_handles():",
            "    if os.name != \"nt\": return set()",
            "    user32 = ctypes.windll.user32",
            "    handles = set()",
            "    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)",
            "    def visit(hwnd, _):",
            "        if user32.IsWindowVisible(hwnd): handles.add(int(hwnd))",
            "        return True",
            "    callback_ref = callback_type(visit)",
            "    user32.EnumWindows(callback_ref, 0)",
            "    return handles",
            "",
            "",
            "def is_chrome_window(hwnd):",
            "    user32 = ctypes.windll.user32",
            "    title = ctypes.create_unicode_buffer(512)",
            "    class_name = ctypes.create_unicode_buffer(256)",
            "    user32.GetWindowTextW(hwnd, title, 512)",
            "    user32.GetClassNameW(hwnd, class_name, 256)",
            "    return \"Chrome_WidgetWin\" in class_name.value or \"Google Chrome\" in title.value",
            "",
            "",
            "def maximize_browser_window(before_handles):",
            "    if os.name != \"nt\": return None",
            "    user32 = ctypes.windll.user32",
            "    candidates = []",
            "    for _ in range(20):",
            "        candidates = [hwnd for hwnd in visible_window_handles() - before_handles if is_chrome_window(hwnd)]",
            "        if candidates: break",
            "        time.sleep(0.05)",
            "    foreground = user32.GetForegroundWindow()",
            "    if not candidates and foreground and is_chrome_window(foreground): candidates = [foreground]",
            "    if not candidates: return None",
            "    hwnd = candidates[-1]",
            "    user32.ShowWindow(hwnd, 3)",
            "    return hwnd",
            "",
            "",
            "async def stream_frames(page, stop_event, capture_lock):",
            "    while not stop_event.is_set():",
            "        try:",
            "            if page.is_closed():",
            "                print(\"SUI_BROWSER_CLOSED:page is closed\", flush=True)",
            "                return",
            "            await emit_frame(page, capture_lock)",
            "        except Exception as exc:",
            "            print(\"SUI_FRAME_ERROR:\" + str(exc), flush=True)",
            "            await asyncio.sleep(0.25)",
            "        await asyncio.sleep(0.25)",
            "",
            "",
            "async def inspect_at(page, x, y):",
            "    return await page.evaluate(\"\"\"(point) => {",
            "        const hovered = document.elementFromPoint(point.x, point.y);",
            "        if (!hovered) return null;",
            "        const target = hovered.closest('button,a,input,textarea,select,[role=button],[role=link]') || hovered;",
            "        const escape = (value) => (window.CSS && CSS.escape ? CSS.escape(value) : value.replace(/[^a-zA-Z0-9_-]/g, '\\\\$&'));",
            "        const parts = [];",
            "        const targetClasses = Array.from(target.classList || []).filter(Boolean).slice(0, 2);",
            "        let node = target;",
            "        while (node && node.nodeType === 1 && node !== document.body) {",
            "            let part = node.tagName.toLowerCase();",
            "            if (node.id) { parts.unshift('#' + escape(node.id)); break; }",
            "            const classes = Array.from(node.classList || []).filter(Boolean).slice(0, 2);",
            "            if (classes.length) part += classes.map((name) => '.' + escape(name)).join('');",
            "            const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((item) => item.tagName === node.tagName) : [];",
            "            if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';",
            "            parts.unshift(part);",
            "            node = node.parentElement;",
            "        }",
            "        const selector = parts.join(' > ');",
            "        const text = String(target.innerText || target.getAttribute('aria-label') || target.value || '').replace(/\\\\s+/g, ' ').trim().slice(0, 120);",
            "        const candidates = [];",
            "        const addCandidate = (kind, value) => {",
            "            if (!value || candidates.some((candidate) => candidate.selector === value)) return;",
            "            let matches = null;",
            "            try { matches = document.querySelectorAll(value).length; } catch (_) {}",
            "            candidates.push({ selector: value, kind, unique: matches === 1, matches });",
            "        };",
            "        if (target.id) addCandidate('ID', '#' + escape(target.id));",
            "        const testId = target.getAttribute('data-testid');",
            "        if (testId) addCandidate('Test ID', '[data-testid=\"' + escape(testId) + '\"]');",
            "        const name = target.getAttribute('name');",
            "        if (name) addCandidate('Name', '[name=\"' + escape(name) + '\"]');",
            "        const ariaLabel = target.getAttribute('aria-label');",
            "        if (ariaLabel) addCandidate('ARIA label', '[aria-label=\"' + escape(ariaLabel) + '\"]');",
            "        const role = target.getAttribute('role');",
            "        if (role) addCandidate('Role', '[role=\"' + escape(role) + '\"]');",
            "        if (targetClasses.length) addCandidate('CSS', target.tagName.toLowerCase() + targetClasses.map((name) => '.' + escape(name)).join(''));",
            "        addCandidate('Path', selector);",
            "        const rect = target.getBoundingClientRect();",
            "        return { selector: candidates[0]?.selector || selector, selectorCandidates: candidates, metadata: { tag: target.tagName.toLowerCase(), text, role: target.getAttribute('role') || '', ariaLabel: target.getAttribute('aria-label') || '' }, boundingRect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } };",
            "    }\"\"\", {\"x\": x, \"y\": y})",
            "",
            "",
            "def json_safe(value):",
            "    if isinstance(value, dict): return {str(key): json_safe(item) for key, item in value.items()}",
            "    if isinstance(value, (list, tuple, set)): return [json_safe(item) for item in value]",
            "    return value",
            "",
            "",
            "async def command_loop(page, capture_lock):",
            "    variables = {}",
            "    while True:",
            "        line = await asyncio.to_thread(sys.stdin.readline)",
            "        if not line:",
            "            return",
            "        try:",
            "            command = json.loads(line)",
            "        except json.JSONDecodeError:",
            "            continue",
        "        try:",
            "            if command.get(\"type\") == \"delay\":",
            "                if command.get(\"until_load_completes\"): await page.wait_for_load_state(\"load\")",
            "                else: await asyncio.sleep(float(command.get(\"seconds\") or 0))",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"delay\"}), flush=True)",
            "            elif command.get(\"type\") == \"assign\":",
            "                variables[str(command.get(\"variable\") or \"\")] = str(command.get(\"value\") or \"\")",
            "                await human_pause(0.25)",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"assign\"}), flush=True)",
            "            elif command.get(\"type\") == \"navigate\":",
            "                await human_pause(0.35)",
            "                await page.goto(str(command.get(\"url\") or \"\"), wait_until=\"domcontentloaded\")",
            "                await human_pause(0.65)",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"navigate\"}), flush=True)",
            "            elif command.get(\"type\") == \"text\":",
            "                await page.keyboard.insert_text(str(command.get(\"text\", \"\")))",
            "            elif command.get(\"type\") == \"key\":",
            "                await page.keyboard.press(str(command.get(\"key\", \"\")))",
            "            elif command.get(\"type\") == \"click\":",
            "                await human_pause(0.20)",
            "                await page.mouse.click(float(command.get(\"x\", 0)), float(command.get(\"y\", 0)))",
            "                await human_pause(0.25)",
            "            elif command.get(\"type\") == \"selector_fill\":",
            "                await human_pause(0.30)",
            "                await page.locator(str(command.get(\"selector\") or \"\")).first.fill(str(command.get(\"value\") or \"\"))",
            "                await human_pause(0.35)",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"fill\", \"trial_id\": command.get(\"trial_id\")}), flush=True)",
            "            elif command.get(\"type\") == \"selector_scrape\":",
            "                scraped_value = await page.locator(str(command.get(\"selector\") or \"\")).first.inner_text()",
            "                await human_pause(0.30)",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"scrape\", \"scraped_value\": scraped_value, \"trial_id\": command.get(\"trial_id\")}), flush=True)",
            "            elif command.get(\"type\") == \"message_box\":",
            "                await show_message_box(page, str(command.get(\"message\") or \"\"))",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"message_box\"}), flush=True)",
            "            elif command.get(\"type\") == \"selector_click\":",
            "                await human_pause(0.30)",
            "                await page.locator(str(command.get(\"selector\") or \"\")).first.click()",
            "                await human_pause(0.35)",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"click\", \"trial_id\": command.get(\"trial_id\")}), flush=True)",
            "            elif command.get(\"type\") == \"inspect\":",
            "                try:",
            "                    result = await inspect_at(page, float(command.get(\"x\", 0)), float(command.get(\"y\", 0)))",
            "                    print(\"SUI_INSPECT:\" + json.dumps(json_safe(result)), flush=True)",
            "                except Exception as exc:",
            "                    print(\"SUI_INSPECT:\" + json.dumps({\"error\": str(exc)}), flush=True)",
            "            else:",
            "                continue",
            "            await emit_frame(page, capture_lock)",
            "        except Exception as exc:",
            "            if command.get(\"type\") in {\"delay\", \"assign\", \"navigate\", \"selector_click\", \"selector_fill\", \"selector_scrape\", \"message_box\"}:",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"failed\", \"error\": str(exc), \"trial_id\": command.get(\"trial_id\")}), flush=True)",
            "",
            "",
            "async def launch_browser(playwright):",
            f"    browser = await playwright.chromium.launch(headless={json.dumps(chrome_mode)} == \"headless\")",
            "    page = await browser.new_page(no_viewport=True)",
            "    return browser, page",
            "",
            "",
            "async def run():",
            "    browser = None",
            "    async with async_playwright() as playwright:",
            f"        foreground_window = preserve_foreground_window() if {json.dumps(chrome_mode)} == \"headful\" else None",
            "        before_handles = visible_window_handles()",
            "        browser, page = await launch_browser(playwright)",
            f"        maximize_browser_window(before_handles) if {json.dumps(chrome_mode)} == \"headful\" else None",
            "        stop_event = asyncio.Event()",
            "        capture_lock = asyncio.Lock()",
        "        frame_task = None",
        "        command_task = None",
            "        try:",
            "            restore_foreground_window(foreground_window)",
            "            await emit_frame(page, capture_lock)",
            "            frame_task = asyncio.create_task(stream_frames(page, stop_event, capture_lock))",
            "            print(\"SUI_BROWSER_READY\", flush=True)",
            "            try:",
            *[line.replace("            ", "                ", 1) for line in action_lines],
            log_line.replace("            ", "                ", 1),
            "                print(\"SUI_READY\", flush=True)",
            "            except Exception as exc:",
            f'                print("SUI_ACTION_COMPLETE:" + json.dumps({{"status": "failed", "action": {json.dumps("navigate" if step["type"] == "navigate_to_website" else step["type"])}, "error": str(exc)}}), flush=True)',
            "                print(\"SUI_LOG:Step failed: \" + str(exc), flush=True)",
            "            command_task = asyncio.create_task(command_loop(page, capture_lock))",
            "            await command_task",
            "        finally:",
            "            stop_event.set()",
            "            if frame_task:",
            "                await frame_task",
            "            if command_task:",
            "                command_task.cancel()",
            "                try:",
            "                    await command_task",
            "                except asyncio.CancelledError:",
            "                    pass",
            "            try:",
            "                if browser: await browser.close()",
            "            except Exception: pass",
            "",
            "",
            "if __name__ == \"__main__\":",
            "    asyncio.run(run())",
            "",
        ]
    )


def recipe_response(row: sqlite3.Row, definition: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "name": row["name"],
        "current_version": row["current_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "definition": definition,
    }


def latest_definition(connection: sqlite3.Connection, recipe_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT definition_json FROM recipe_versions WHERE recipe_id = ? ORDER BY version DESC LIMIT 1",
        (recipe_id,),
    ).fetchone()
    return json.loads(row["definition_json"]) if row else None


def project_response(connection: sqlite3.Connection, project_row: sqlite3.Row, include_definitions: bool = True) -> dict[str, Any]:
    recipes = []
    rows = connection.execute(
        "SELECT * FROM recipes WHERE project_id = ? ORDER BY created_at ASC", (project_row["id"],)
    ).fetchall()
    for recipe in rows:
        definition = latest_definition(connection, recipe["id"]) if include_definitions else None
        recipes.append(recipe_response(recipe, definition))
    return {
        "id": project_row["id"],
        "name": project_row["name"],
        "created_at": project_row["created_at"],
        "updated_at": project_row["updated_at"],
        "recipes": recipes,
    }


def list_projects() -> list[dict[str, Any]]:
    with db_connection() as connection:
        rows = connection.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        return [project_response(connection, row, include_definitions=False) for row in rows]


def save_recipe(project_id: str, recipe_id: str | None, name: str, raw_definition: dict[str, Any]) -> dict[str, Any]:
    recipe_name = str(name or "").strip()
    if not recipe_name:
        raise ValueError("Recipe name is required")
    with db_connection() as connection:
        project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project:
            raise ValueError("Project not found")
        recipe = None
        if recipe_id:
            recipe = connection.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
            if not recipe or recipe["project_id"] != project_id:
                raise ValueError("Recipe not found")
        else:
            recipe = connection.execute(
                "SELECT * FROM recipes WHERE project_id = ? AND name = ?", (project_id, recipe_name)
            ).fetchone()
            if recipe:
                recipe_id = recipe["id"]
        recipe_id = recipe_id or make_id("recipe")
        next_version = int(recipe["current_version"]) + 1 if recipe else 1
        definition = normalize_definition(raw_definition, project_id, recipe_id, recipe_name, next_version)
        generated_path = GENERATED_DIR / project_id / recipe_id / f"v{next_version}.py"
        generated_path.parent.mkdir(parents=True, exist_ok=True)
        generated_path.write_text(generate_python(definition), encoding="utf-8")
        timestamp = now_iso()
        if recipe:
            connection.execute(
                "UPDATE recipes SET name = ?, current_version = ?, updated_at = ? WHERE id = ?",
                (recipe_name, next_version, timestamp, recipe_id),
            )
        else:
            connection.execute(
                "INSERT INTO recipes (id, project_id, name, current_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (recipe_id, project_id, recipe_name, next_version, timestamp, timestamp),
            )
        connection.execute(
            "INSERT INTO recipe_versions (recipe_id, version, definition_json, generated_path, created_at) VALUES (?, ?, ?, ?, ?)",
            (recipe_id, next_version, json.dumps(definition), str(generated_path.relative_to(ROOT)), timestamp),
        )
        connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (timestamp, project_id))
        saved = connection.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        return recipe_response(saved, definition)


def add_job_log(job_id: str, level: str, message: str) -> None:
    with db_connection() as connection:
        connection.execute(
            "INSERT INTO job_logs (job_id, level, message, created_at) VALUES (?, ?, ?, ?)",
            (job_id, level, message, now_iso()),
        )


def execute_generated(path: Path, env: dict[str, str] | None = None) -> tuple[int, list[str], list[str]]:
    try:
        execution_env = os.environ.copy()
        if env:
            execution_env.update(env)
        execution_env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
            check=False,
            env=execution_env,
        )
        return (
            completed.returncode,
            [line for line in completed.stdout.splitlines() if line.strip()],
            [line for line in completed.stderr.splitlines() if line.strip()],
        )
    except subprocess.TimeoutExpired:
        return 124, [], ["Execution timed out after 600 seconds"]
    except OSError as exc:
        return 126, [], [str(exc)]


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def emit_step_event(run_id: str, event: dict[str, Any]) -> None:
    with STEP_RUNS_CHANGED:
        run = STEP_RUNS.get(run_id)
        if not run:
            return
        run["events"].append(_json_safe_value(event))
        STEP_RUNS_CHANGED.notify_all()


def emit_job_event(job_id: str, event: dict[str, Any]) -> None:
    with JOB_RUNS_CHANGED:
        run = JOB_RUNS.get(job_id)
        if not run:
            return
        run["events"].append(_json_safe_value(event))
        JOB_RUNS_CHANGED.notify_all()


def add_streamed_job_log(job_id: str, level: str, message: str) -> None:
    if not message:
        return
    add_job_log(job_id, level, message)
    emit_job_event(job_id, {"type": "log", "level": level, "message": message})


def handle_job_output(job_id: str, line: str) -> None:
    if line.startswith("SUI_FRAME:"):
        with JOB_RUNS_CHANGED:
            run = JOB_RUNS.get(job_id)
            if run:
                run["last_frame_at"] = time.monotonic()
                run["browser_healthy"] = True
        emit_job_event(job_id, {"type": "frame", "data": f"data:image/jpeg;base64,{line.removeprefix('SUI_FRAME:')}"})
    elif line.startswith("SUI_LOG:"):
        add_streamed_job_log(job_id, "info", line.removeprefix("SUI_LOG:"))
    elif line.startswith("SUI_BROWSER_CLOSED:"):
        with JOB_RUNS_CHANGED:
            run = JOB_RUNS.get(job_id)
            if run:
                run["browser_healthy"] = False
        add_streamed_job_log(job_id, "error", line.removeprefix("SUI_BROWSER_CLOSED:"))
    elif line.startswith("SUI_FRAME_ERROR:"):
        add_streamed_job_log(job_id, "warning", f"Frame delayed: {line.removeprefix('SUI_FRAME_ERROR:')}")
    elif line not in {"SUI_BROWSER_READY", "SUI_READY"}:
        add_streamed_job_log(job_id, "info", line)


def stream_job_process(job_id: str, generated_file: Path, definition: dict[str, Any]) -> tuple[int, str | None]:
    execution_env = os.environ.copy()
    execution_env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-u", str(generated_file)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=execution_env,
    )
    with JOB_RUNS_CHANGED:
        if job_id in JOB_RUNS:
            JOB_RUNS[job_id]["process"] = process
            cancelled = bool(JOB_RUNS[job_id].get("cancel_requested"))
        else:
            cancelled = False
    if cancelled:
        terminate_job_process(process)

    stderr_lines: list[str] = []

    def read_stderr() -> None:
        if not process.stderr:
            return
        for raw_line in process.stderr:
            line = raw_line.strip()
            if line:
                stderr_lines.append(line)
                add_streamed_job_log(job_id, "error", line)

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()
    if process.stdout:
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                handle_job_output(job_id, line)
    stderr_thread.join()
    return_code = process.wait()
    return return_code, stderr_lines[-1] if stderr_lines else None


def terminate_job_process(process: subprocess.Popen | None) -> None:
    if not process or process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        except OSError:
            pass
    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def active_browser_run(scope: tuple[str, str, str, str]) -> tuple[str, dict[str, Any]] | None:
    with STEP_RUNS_CHANGED:
        for run_id, run in reversed(list(STEP_RUNS.items())):
            process = run.get("process")
            if run.get("scope") != scope or run.get("done"):
                continue
            if run.get("starting"):
                return run_id, run
            if not process or process.poll() is not None or not process.stdin:
                continue
            if process and process.poll() is None and process.stdin:
                return run_id, run
    return None


def send_browser_command(run_id: str, command: dict[str, Any], reserve_step: bool = False) -> str:
    command = dict(command)
    command_id = str(command.get("command_id") or make_id("command"))
    command["command_id"] = command_id
    with STEP_RUNS_CHANGED:
        run = STEP_RUNS.get(run_id)
        process = run.get("process") if run else None
        if not process or process.poll() is not None or not process.stdin:
            raise RuntimeError("Browser session is no longer active")
        previous_command_id = run.get("busy_command_id") if reserve_step and run.get("command_busy") else None
        if reserve_step:
            run["command_busy"] = True
            run["busy_command_id"] = command_id
        try:
            if previous_command_id:
                run.setdefault("superseded_command_ids", set()).add(previous_command_id)
                process.stdin.write(json.dumps({"type": "cancel_command", "command_id": previous_command_id}) + "\n")
            process.stdin.write(json.dumps(command) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            if reserve_step and run.get("busy_command_id") == command_id:
                run["command_busy"] = False
                run["busy_command_id"] = None
            raise RuntimeError("Browser session is no longer active") from exc
    return command_id


def clear_command_busy_if_matching(run_id: str, command_id: str | None) -> bool:
    if not command_id:
        return False
    with STEP_RUNS_CHANGED:
        run = STEP_RUNS.get(run_id)
        if not run or run.get("busy_command_id") != command_id:
            return False
        run["command_busy"] = False
        run["busy_command_id"] = None
        STEP_RUNS_CHANGED.notify_all()
        return True


def run_step_worker(run_id: str, definition: dict[str, Any], script_path: Path) -> None:
    emit_step_event(run_id, {"type": "status", "status": "running"})
    session_ready = False
    browser_closed_error = None
    try:
        execution_env = os.environ.copy()
        execution_env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-u", str(script_path)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=execution_env,
        )
        with STEP_RUNS_CHANGED:
            if run_id in STEP_RUNS:
                STEP_RUNS[run_id]["process"] = process
                STEP_RUNS[run_id]["starting"] = False
                STEP_RUNS_CHANGED.notify_all()
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line.startswith("SUI_FRAME:"):
                with STEP_RUNS_CHANGED:
                    if run_id in STEP_RUNS:
                        STEP_RUNS[run_id]["last_frame_at"] = time.monotonic()
                emit_step_event(
                    run_id,
                    {"type": "frame", "data": f"data:image/jpeg;base64,{line.removeprefix('SUI_FRAME:')}"},
                )
            elif line.startswith("SUI_FRAME_ERROR:"):
                emit_step_event(run_id, {"type": "log", "message": f"Frame delayed: {line.removeprefix('SUI_FRAME_ERROR:')}", "level": "warning"})
            elif line.startswith("SUI_LOG:"):
                message = line.removeprefix("SUI_LOG:")
                command_match = re.search(r"\bcommand_id=([A-Za-z0-9_-]+)", message)
                if command_match:
                    with STEP_RUNS_CHANGED:
                        run_state = STEP_RUNS.get(run_id) or {}
                        current_command_id = run_state.get("busy_command_id")
                        superseded_command_ids = run_state.get("superseded_command_ids", set())
                    if command_match.group(1) in superseded_command_ids or (current_command_id and command_match.group(1) != current_command_id):
                        continue
                emit_step_event(run_id, {"type": "log", "message": message})
            elif line.startswith("SUI_BROWSER_CLOSED:"):
                browser_closed_error = "Browser target closed; run the first Navigate to website step to launch a fresh browser"
                with STEP_RUNS_CHANGED:
                    if run_id in STEP_RUNS:
                        STEP_RUNS[run_id]["command_busy"] = False
                        STEP_RUNS[run_id]["busy_command_id"] = None
                emit_step_event(run_id, {"type": "session", "status": "dead", "error": browser_closed_error})
                emit_step_event(run_id, {"type": "complete", "status": "failed", "error": browser_closed_error})
                if process.poll() is None:
                    process.terminate()
            elif line.startswith("SUI_INSPECT:"):
                try:
                    element = json.loads(line.removeprefix("SUI_INSPECT:"))
                    emit_step_event(run_id, {"type": "inspect", "element": element})
                except json.JSONDecodeError:
                    emit_step_event(run_id, {"type": "log", "message": "Browser returned invalid picker data", "level": "error"})
            elif line.startswith("SUI_ACTION_COMPLETE:"):
                try:
                    action = json.loads(line.removeprefix("SUI_ACTION_COMPLETE:"))
                    clear_command_busy_if_matching(run_id, action.get("command_id"))
                    emit_step_event(run_id, {"type": "action_complete", **action})
                except json.JSONDecodeError:
                    emit_step_event(run_id, {"type": "action_complete", "status": "failed", "error": "Browser returned invalid action data"})
            elif line in {"SUI_SESSION_READY", "SUI_READY"}:
                session_ready = True
                emit_step_event(run_id, {"type": "session", "status": "ready"})
        stderr = process.stderr.read() if process.stderr else ""
        return_code = process.wait()
        stderr_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        for line in stderr_lines:
            emit_step_event(run_id, {"type": "log", "message": line, "level": "error"})
        status = "completed" if return_code == 0 else "failed"
        error = None
        if return_code != 0:
            error = browser_closed_error or stderr_lines[-1] if stderr_lines else browser_closed_error or f"Browser step exited with code {return_code}"
        if return_code != 0:
            emit_step_event(run_id, {"type": "complete", "status": status, "error": error})
        elif session_ready:
            emit_step_event(run_id, {"type": "session", "status": "dead", "error": "Browser session ended"})
    except OSError as exc:
        emit_step_event(run_id, {"type": "complete", "status": "failed", "error": str(exc)})
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass
        with STEP_RUNS_CHANGED:
            if run_id in STEP_RUNS:
                STEP_RUNS[run_id]["process"] = None
                STEP_RUNS[run_id]["done"] = True
                STEP_RUNS[run_id]["starting"] = False
                STEP_RUNS[run_id]["command_busy"] = False
                STEP_RUNS[run_id]["busy_command_id"] = None
            STEP_RUNS_CHANGED.notify_all()


def start_step_run(payload: dict[str, Any]) -> tuple[str, int, str]:
    step = payload.get("step") or {}
    project_id = str(payload.get("project_id") or "draft-project")
    recipe_id = str(payload.get("recipe_id") or "draft-recipe")
    studio_session_id = str(payload.get("studio_session_id") or "default-studio")
    mode = "headless" if payload.get("mode") == "headless" else "headful"
    recipe_type = validate_recipe_type(payload.get("recipe_type", "standard"))
    scope = (project_id, recipe_id, studio_session_id, mode)
    definition = normalize_definition(
        {"steps": [step], "variables": payload.get("variables") or [], "mode": mode, "recipe_type": recipe_type},
        project_id, recipe_id, "step-run", 1, require_first_navigation=False,
    )


    normalized_step = definition["steps"][0]
    if normalized_step["type"] == "navigate_to_website":
        command = {"type": "navigate", "url": normalized_step["settings"]["url"]}
    elif normalized_step["type"] in {"navigate", "click", "fill", "check_element_exists", "check_text_exists", "if", "for", "use_js", "keypress", "message_box", "scrape", "pattern_matcher", "smartie", "writer", "call_recipe", "invoke_recipe"}:
        command = {"type": "run_step", "step": normalized_step}
    elif normalized_step["type"] == "delay":
        command = {
            "type": "delay",
            "seconds": normalized_step["settings"].get("seconds", 1),
            "until_load_completes": normalized_step["settings"].get("until_load_completes", False),
        }
    elif normalized_step["type"] == "assign":
        command = {"type": "assign", "variable": normalized_step["settings"]["variable"], "value": normalized_step["settings"].get("value", "")}
    else:
        command = {"type": "selector_click", "selector": normalized_step["settings"]["selector"]}
    command_id = make_id("command")
    command["command_id"] = command_id
    normalized_step["command_id"] = command_id
    definition["command_id"] = command_id

    # Serialize the active-session lookup and launch reservation. This prevents
    # two UI clicks arriving together from creating two browser processes.
    with STEP_SESSION_START_LOCK:
        active = active_browser_run(scope)
        if active:
            active_run_id, active_run = active
            if not active_run.get("process"):
                deadline = time.monotonic() + 5
                with STEP_RUNS_CHANGED:
                    while active_run.get("starting") and not active_run.get("done") and not active_run.get("process"):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        STEP_RUNS_CHANGED.wait(timeout=min(0.1, remaining))
                active = active_browser_run(scope)
            if active:
                active_run_id, _ = active
                with STEP_RUNS_CHANGED:
                    event_index = len(STEP_RUNS[active_run_id]["events"])
                send_browser_command(active_run_id, command, reserve_step=True)
                return active_run_id, event_index, command_id

        run_id = make_id("step_run")
        script_path = Path(tempfile.gettempdir()) / f"sui-{run_id}.py"
        script_path.write_text(generate_step_python(definition), encoding="utf-8")
        with STEP_RUNS_CHANGED:
            STEP_RUNS[run_id] = {
                "events": [],
                "done": False,
                "starting": True,
                "command_busy": True,
                "busy_command_id": command_id,
                "superseded_command_ids": set(),
                "last_frame_at": 0.0,
                "scope": scope,
                "project_id": project_id,
                "recipe_id": recipe_id,
                "studio_session_id": studio_session_id,
                "mode": mode,
            }
        threading.Thread(target=run_step_worker, args=(run_id, definition, script_path), daemon=True).start()
        return run_id, 0, command_id


def finish_job(job_id: str, status: str, error: str | None) -> None:
    with db_connection() as connection:
        connection.execute(
            "UPDATE jobs SET status = ?, ended_at = ?, error = ? WHERE id = ?",
            (status, now_iso(), error, job_id),
        )
    if error:
        add_job_log(job_id, "error", error)


def job_cancel_requested(job_id: str) -> bool:
    with JOB_RUNS_CHANGED:
        return bool(JOB_RUNS.get(job_id, {}).get("cancel_requested"))


def complete_stopped_job(job_id: str) -> None:
    message = "Stopped by user"
    with JOB_RUNS_CHANGED:
        run = JOB_RUNS.get(job_id)
        if run and run.get("done"):
            return
    finish_job(job_id, "stopped", message)
    emit_job_event(job_id, {"type": "complete", "status": "stopped", "error": message})
    with JOB_RUNS_CHANGED:
        run = JOB_RUNS.get(job_id)
        if run:
            run["done"] = True
            run["process"] = None
        JOB_RUNS_CHANGED.notify_all()


def run_job(job_id: str) -> None:
    with db_connection() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return
        if job["status"] == "stopped":
            complete_stopped = True
        else:
            complete_stopped = False
        if complete_stopped:
            pass
        else:
            connection.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (now_iso(), job_id))
            version = connection.execute(
                "SELECT generated_path, definition_json FROM recipe_versions WHERE recipe_id = ? AND version = ?",
                (job["recipe_id"], job["recipe_version"]),
            ).fetchone()
    if complete_stopped:
        emit_job_event(job_id, {"type": "status", "status": "stopped"})
        complete_stopped_job(job_id)
        return
    if job_cancel_requested(job_id):
        complete_stopped_job(job_id)
        return
    emit_job_event(job_id, {"type": "status", "status": "running"})
    add_job_log(job_id, "info", f"Running recipe version v{job['recipe_version']}")
    if not version:
        return finish_job(job_id, "failed", "Generated recipe version was not found")
    generated_file = ROOT / version["generated_path"]
    try:
        # Recipe JSON is the source of truth. Refresh the existing version's
        # generated file so recipes saved before a generator fix use the same
        # current Rustwright/Chrome execution path without creating a version.
        definition = json.loads(version["definition_json"])
        generated_file.parent.mkdir(parents=True, exist_ok=True)
        generated_file.write_text(generate_python(definition), encoding="utf-8")
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        return finish_job(job_id, "failed", f"Unable to prepare generated recipe: {exc}")
    add_job_log(job_id, "info", f"Launching {'headless' if definition.get('mode') == 'headless' else 'headful'} Chrome for full recipe")
    emit_job_event(job_id, {"type": "log", "level": "info", "message": f"Launching {'headless' if definition.get('mode') == 'headless' else 'headful'} Chrome for full recipe"})
    if job_cancel_requested(job_id):
        complete_stopped_job(job_id)
        return
    try:
        code, stderr_tail = stream_job_process(job_id, generated_file, definition)
    except OSError as exc:
        code, stderr_tail = 126, str(exc)
    if job_cancel_requested(job_id):
        complete_stopped_job(job_id)
    elif code == 0:
        add_job_log(job_id, "info", "Recipe completed successfully")
        emit_job_event(job_id, {"type": "complete", "status": "completed", "error": None})
        finish_job(job_id, "completed", None)
    else:
        error = stderr_tail or f"Generated recipe exited with code {code}"
        emit_job_event(job_id, {"type": "complete", "status": "failed", "error": error})
        finish_job(job_id, "failed", error)
    with JOB_RUNS_CHANGED:
        run = JOB_RUNS.get(job_id)
        if run:
            run["process"] = None
            run["done"] = True
        JOB_RUNS_CHANGED.notify_all()


def create_job(recipe_id: str, studio_session_id: str = "projects") -> dict[str, Any]:
    with db_connection() as connection:
        recipe = connection.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        if not recipe or not recipe["current_version"]:
            raise ValueError("Recipe must be saved before it can run")
        job_id = make_id("job")
        timestamp = now_iso()
        connection.execute(
            "INSERT INTO jobs (id, project_id, recipe_id, recipe_version, status, created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
            (job_id, recipe["project_id"], recipe_id, recipe["current_version"], timestamp),
        )
        job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    with JOB_RUNS_CHANGED:
        JOB_RUNS[job_id] = {
            "events": [{"type": "status", "status": "queued"}],
            "done": False,
            "cancel_requested": False,
            "process": None,
            "browser_healthy": False,
            "last_frame_at": 0.0,
            "project_id": recipe["project_id"],
            "recipe_id": recipe_id,
            "studio_session_id": studio_session_id,
        }
        JOB_RUNS_CHANGED.notify_all()
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return job_response(job, [])


def stop_job(job_id: str) -> dict[str, Any] | None:
    with db_connection() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        if row["status"] not in {"queued", "running"}:
            logs = connection.execute("SELECT * FROM job_logs WHERE job_id = ? ORDER BY id ASC", (job_id,)).fetchall()
            return job_response(row, logs)
    with JOB_RUNS_CHANGED:
        run = JOB_RUNS.get(job_id)
        if run:
            run["cancel_requested"] = True
            process = run.get("process")
        else:
            process = None
        JOB_RUNS_CHANGED.notify_all()
    with db_connection() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'stopped', ended_at = ?, error = ? WHERE id = ? AND status IN ('queued', 'running')",
            (now_iso(), "Stopped by user", job_id),
        )
    if process and process.poll() is None:
        terminate_job_process(process)
    else:
        complete_stopped_job(job_id)
    return get_job(job_id)


def job_response(row: sqlite3.Row, logs: list[sqlite3.Row]) -> dict[str, Any]:
    with db_connection() as connection:
        project = connection.execute("SELECT name FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
        recipe = connection.execute("SELECT name FROM recipes WHERE id = ?", (row["recipe_id"],)).fetchone()
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "project_name": project["name"] if project else "Unknown project",
        "recipe_id": row["recipe_id"],
        "recipe_name": recipe["name"] if recipe else "Unknown recipe",
        "recipe_version": row["recipe_version"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "created_at": row["created_at"],
        "error": row["error"],
        "logs": [dict(log) for log in logs],
    }


def list_jobs() -> list[dict[str, Any]]:
    with db_connection() as connection:
        rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [job_response(row, []) for row in rows]


def get_job(job_id: str) -> dict[str, Any] | None:
    with db_connection() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        logs = connection.execute("SELECT * FROM job_logs WHERE job_id = ? ORDER BY id ASC", (job_id,)).fetchall()
    return job_response(row, logs) if row else None


class SuiHandler(BaseHTTPRequestHandler):
    server_version = "Sui/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{now_iso()}] {format % args}")

    def send_payload(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_error_payload(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self.send_payload({"error": message}, status)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                return self.send_payload({"ok": True})
            if path == "/api/writer/tables":
                return self.send_payload({"tables": writer_tables()})
            if path == "/api/writer/schema":
                query = parse_qs(urlparse(self.path).query)
                table_name = str(query.get("table", query.get("table_name", [""]))[0] or "")
                return self.send_payload({"table": table_name, "columns": writer_table_schema(table_name)})
            if path == "/api/data/tables":
                return self.send_payload({"tables": viewer_tables()})
            if path == "/api/data/table":
                query = parse_qs(urlparse(self.path).query)
                table_name = str(query.get("table", query.get("name", [""]))[0] or "")
                limit = query.get("limit", [200])[0]
                return self.send_payload(viewer_table_data(table_name, limit))
            if path == "/api/projects":
                return self.send_payload({"projects": list_projects()})
            if path.startswith("/api/projects/"):
                project_id = path.split("/")[3]
                with db_connection() as connection:
                    project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                    if not project:
                        return self.send_error_payload("Project not found", HTTPStatus.NOT_FOUND)
                    return self.send_payload(project_response(connection, project))
            if path.startswith("/api/step-runs/") and path.endswith("/events"):
                query = parse_qs(urlparse(self.path).query)
                return self.stream_step_events(path.split("/")[3], int(query.get("after", [0])[0] or 0))
            if path == "/api/jobs":
                return self.send_payload({"jobs": list_jobs()})
            if path.startswith("/api/jobs/") and path.endswith("/events"):
                query = parse_qs(urlparse(self.path).query)
                return self.stream_job_events(path.split("/")[3], int(query.get("after", [0])[0] or 0))
            if path.startswith("/api/jobs/"):
                job = get_job(path.split("/")[3])
                return self.send_payload(job or {"error": "Job not found"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
            return self.serve_static(path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except (sqlite3.Error, ValueError, KeyError) as exc:
            self.send_error_payload(str(exc), HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = parse_json_body(self)
            if path == "/api/projects":
                name = str(body.get("name") or "").strip()
                if not name:
                    raise ValueError("Project name is required")
                project_id = make_id("project")
                timestamp = now_iso()
                with db_connection() as connection:
                    connection.execute(
                        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                        (project_id, name, timestamp, timestamp),
                    )
                    project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                    return self.send_payload(project_response(connection, project), HTTPStatus.CREATED)
            if path == "/api/data/table":
                result = craft_table(body.get("table", body.get("name", "")), body.get("columns"))
                return self.send_payload(result, HTTPStatus.CREATED if result["created"] else HTTPStatus.OK)
            if path == "/api/data/table/row":
                result = data_table_row(body.get("table", ""), "insert", raw_values=body.get("values"))
                return self.send_payload(result, HTTPStatus.CREATED)
            if path.startswith("/api/jobs/") and path.endswith("/stop"):
                job_id = path.split("/")[3]
                job = stop_job(job_id)
                return self.send_payload(job or {"error": "Job not found"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
            if path.startswith("/api/projects/") and path.endswith("/recipes"):
                project_id = path.split("/")[3]
                saved = save_recipe(project_id, None, body.get("name", ""), body.get("definition") or {})
                return self.send_payload(saved, HTTPStatus.CREATED)
            if path.startswith("/api/recipes/") and path.endswith("/run"):
                return self.send_payload(create_job(path.split("/")[3], str(body.get("studio_session_id") or "projects")), HTTPStatus.ACCEPTED)
            if path.startswith("/api/step-runs/") and path.endswith("/input"):
                run_id = path.split("/")[3]
                command_type = str(body.get("type") or "")
                if command_type not in {"delay", "assign", "navigate", "text", "key", "click", "selector_click", "selector_fill", "selector_scrape", "selector_check", "message_box", "inspect", "inspect_move", "clear_picker_overlay", "run_step"}:
                    raise ValueError("Unsupported browser input type")
                command = {"type": command_type}
                if command_type == "delay":
                    command["seconds"] = validate_delay(body.get("seconds"))
                    command["until_load_completes"] = validate_until_load_completes(body.get("until_load_completes", False))
                elif command_type == "assign":
                    command["variable"] = validate_variable_name(body.get("variable"))
                    command["value"] = str(body.get("value") or "")
                elif command_type == "navigate":
                    command["url"] = validate_url(body.get("url"))
                elif command_type == "text":
                    command["text"] = str(body.get("text") or "")
                elif command_type == "key":
                    command["key"] = str(body.get("key") or "")
                elif command_type in {"selector_click", "selector_fill", "selector_scrape", "selector_check"}:
                    command["selector"] = validate_selector(body.get("selector"))
                    command["trial_id"] = str(body.get("trial_id") or "")
                    if command_type == "selector_fill":
                        command["value"] = str(body.get("value") or "")
                elif command_type == "message_box":
                    command["message"] = str(body.get("message") or "")
                elif command_type == "run_step":
                    raw_step = body.get("step") or {}
                    command["step"] = normalize_definition(
                        {"steps": [raw_step], "variables": body.get("variables") or []},
                        "draft-project", "draft-recipe", "step-run", 1, require_first_navigation=False,
                    )["steps"][0]
                else:
                    command["x"] = float(body.get("x", 0))
                    command["y"] = float(body.get("y", 0))
                    if command_type in {"click", "inspect", "inspect_move"}:
                        command["source_width"] = float(body.get("source_width", 0) or 0)
                        command["source_height"] = float(body.get("source_height", 0) or 0)
                reserve_command = command_type in {"delay", "assign", "navigate", "selector_click", "selector_fill", "selector_scrape", "selector_check", "message_box", "run_step"}
                try:
                    with STEP_RUNS_CHANGED:
                        event_index = len(STEP_RUNS.get(run_id, {}).get("events", []))
                    command_id = send_browser_command(run_id, command, reserve_step=reserve_command)
                except RuntimeError as exc:
                    return self.send_error_payload(str(exc), HTTPStatus.CONFLICT)
                return self.send_payload({"ok": True, "event_index": event_index, "command_id": command_id})
            if path == "/api/step-runs":
                run_id, event_index, command_id = start_step_run(body)
                return self.send_payload({"run_id": run_id, "event_index": event_index, "command_id": command_id, "status": "queued"}, HTTPStatus.ACCEPTED)
            return self.send_error_payload("Route not found", HTTPStatus.NOT_FOUND)
        except sqlite3.IntegrityError:
            self.send_error_payload("A project or recipe with that name already exists")
        except RuntimeError as exc:
            self.send_error_payload(str(exc), HTTPStatus.CONFLICT)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except (sqlite3.Error, ValueError, KeyError) as exc:
            self.send_error_payload(str(exc), HTTPStatus.BAD_REQUEST)

    def stream_step_events(self, run_id: str, start_index: int = 0) -> None:
        with STEP_RUNS_CHANGED:
            if run_id not in STEP_RUNS:
                return self.send_error_payload("Step run not found", HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        index = max(0, int(start_index))
        try:
            while True:
                with STEP_RUNS_CHANGED:
                    run = STEP_RUNS[run_id]
                    events = run["events"][index:]
                    done = run["done"]
                    if not events and not done:
                        STEP_RUNS_CHANGED.wait(timeout=0.5)
                        continue
                for event in events:
                    payload = f"id: {index}\ndata: {json.dumps(event)}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                    index += 1
                if done and index >= len(STEP_RUNS[run_id]["events"]):
                    return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def stream_job_events(self, job_id: str, start_index: int = 0) -> None:
        with JOB_RUNS_CHANGED:
            if job_id not in JOB_RUNS:
                return self.send_error_payload("Job event stream not found", HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        index = max(0, int(start_index))
        try:
            while True:
                with JOB_RUNS_CHANGED:
                    run = JOB_RUNS[job_id]
                    events = run["events"][index:]
                    done = run["done"]
                    if not events and not done:
                        JOB_RUNS_CHANGED.wait(timeout=0.5)
                        continue
                for event in events:
                    payload = f"id: {index}\ndata: {json.dumps(event)}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                    index += 1
                if done and index >= len(JOB_RUNS[job_id]["events"]):
                    return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        try:
            body = parse_json_body(self)
            if path == "/api/data/table/row":
                result = data_table_row(
                    body.get("table", ""),
                    "update",
                    row_id=body.get("row_id"),
                    column_name=body.get("column"),
                    value=body.get("value"),
                )
                return self.send_payload(result)
            if path.startswith("/api/recipes/"):
                recipe_id = path.split("/")[3]
                with db_connection() as connection:
                    recipe = connection.execute("SELECT project_id FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
                if not recipe:
                    return self.send_error_payload("Recipe not found", HTTPStatus.NOT_FOUND)
                return self.send_payload(save_recipe(recipe["project_id"], recipe_id, body.get("name", ""), body.get("definition") or {}))
            return self.send_error_payload("Route not found", HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except sqlite3.IntegrityError:
            self.send_error_payload("A recipe with that name already exists in this project")
        except (sqlite3.Error, ValueError, KeyError) as exc:
            self.send_error_payload(str(exc), HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/data/table":
                body = parse_json_body(self)
                return self.send_payload({"deleted": delete_data_table(body.get("table", ""))})
            if path == "/api/data/table/row":
                body = parse_json_body(self)
                result = data_table_row(body.get("table", ""), "delete", row_id=body.get("row_id"))
                return self.send_payload(result)
            if path.startswith("/api/recipes/"):
                recipe_id = path.split("/")[3]
                with db_connection() as connection:
                    recipe = connection.execute("SELECT id FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
                    if not recipe:
                        return self.send_error_payload("Recipe not found", HTTPStatus.NOT_FOUND)
                    connection.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
                return self.send_payload({"deleted": recipe_id})
            return self.send_error_payload("Route not found", HTTPStatus.NOT_FOUND)
        except sqlite3.IntegrityError:
            self.send_error_payload("Recipe cannot be deleted because it has job history")
        except (sqlite3.Error, ValueError, KeyError) as exc:
            self.send_error_payload(str(exc), HTTPStatus.BAD_REQUEST)

    def serve_static(self, path: str) -> None:
        if not FRONTEND_DIST.exists():
            return self.send_error_payload("Frontend build not found. Run npm run build first.", HTTPStatus.NOT_FOUND)
        candidate = (FRONTEND_DIST / (path.lstrip("/") or "index.html")).resolve()
        if FRONTEND_DIST not in candidate.parents and candidate != FRONTEND_DIST:
            return self.send_error_payload("Invalid path", HTTPStatus.BAD_REQUEST)
        if not candidate.is_file():
            candidate = FRONTEND_DIST / "index.html"
        body = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(str(candidate))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _listening_process_ids(port: int) -> list[int]:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            return []
        process_ids = []
        for raw_line in result.stdout.splitlines():
            parts = raw_line.split()
            if len(parts) < 5 or parts[-2].upper() != "LISTENING":
                continue
            local_address = parts[1].rsplit(":", 1)
            if len(local_address) != 2 or local_address[1] != str(port):
                continue
            try:
                process_ids.append(int(parts[-1]))
            except ValueError:
                continue
        return sorted(set(process_ids))

    try:
        result = subprocess.run(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    process_ids = []
    for value in result.stdout.splitlines():
        try:
            process_ids.append(int(value.strip()))
        except ValueError:
            continue
    return sorted(set(process_ids))


def _process_image_path(process_id: int) -> str:
    if os.name == "nt":
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-Process -Id {int(process_id)} -ErrorAction SilentlyContinue).Path",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            return result.stdout.strip()
        except OSError:
            return ""
    try:
        return os.readlink(f"/proc/{int(process_id)}/exe")
    except OSError:
        return ""


def stop_existing_python_backend(port: int) -> None:
    """Replace a Python listener on this app's port, leaving other services alone."""
    current_pid = os.getpid()
    for process_id in _listening_process_ids(port):
        if process_id == current_pid:
            continue
        image_path = _process_image_path(process_id)
        if "python" not in Path(image_path).name.lower():
            print(f"Port {port} is already used by a non-Python process; leaving it running.")
            continue
        try:
            if os.name == "nt":
                subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"Stop-Process -Id {int(process_id)} -Force -ErrorAction Stop",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                os.kill(process_id, signal.SIGTERM)
            print(f"Stopped existing Python backend process {process_id} on port {port}.")
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Unable to stop existing Python backend process {process_id}: {exc}")


def main() -> None:
    host = os.environ.get("SUI_HOST", "127.0.0.1")
    port = int(os.environ.get("SUI_PORT", "8000"))
    stop_existing_python_backend(port)
    initialize_database()
    server = ThreadingHTTPServer((host, port), SuiHandler)
    print(f"Sui backend running at http://{host}:{port}")
    print("Build the frontend with `npm run build` for the backend-served UI.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Sui backend")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
