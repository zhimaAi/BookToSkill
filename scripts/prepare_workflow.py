#!/usr/bin/env python3
"""Run deterministic DocToSkill preparation stages with compact output."""

from __future__ import annotations

import argparse
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from batch_index import load_state


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


SCRIPT_DIR = Path(__file__).resolve().parent


def run_stage(script: str, arguments: list[str]) -> None:
    command = [sys.executable, str(SCRIPT_DIR / script), *arguments]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode == 0:
        return
    details = (completed.stderr or completed.stdout or "unknown error").strip()
    if len(details) > 2000:
        details = details[-2000:]
    raise RuntimeError(f"{script} failed: {details}")


def compact_state(state: dict, resumed: bool) -> dict:
    return {
        "status": "prepared",
        "next_action": "run_batch_iterator",
        "read_policy": "Do not read chunk files, chunks.jsonl, or workflow state directly.",
        "documents": state["documents"],
        "chunks": state["chunks"],
        "image_only_chunks": state["image_only_chunks"],
        "batches": len(state["batches"]),
        "resumed": resumed,
    }


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"JSONL record at {path}:{line_number} must be an object")
        rows.append(row)
    if not rows:
        raise ValueError(f"JSONL file is empty: {path}")
    return rows


def safe_archive_parts(name: str) -> tuple[str, ...]:
    if not name or "\\" in name or name.startswith("/"):
        raise ValueError(f"unsafe path in existing skill archive: {name}")
    parts = tuple(part for part in name.rstrip("/").split("/") if part)
    if not parts or any(part in {".", ".."} or ":" in part for part in parts):
        raise ValueError(f"unsafe path in existing skill archive: {name}")
    return parts


def validate_archive_member(member: zipfile.ZipInfo) -> tuple[str, ...]:
    parts = safe_archive_parts(member.filename)
    mode = member.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ValueError(f"symbolic links are not allowed in existing skill archive: {member.filename}")
    return parts


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def stage_existing_skill(existing_zip: Path, markdown_dir: Path, assets_dir: Path) -> list[dict]:
    if not existing_zip.is_file():
        raise ValueError(f"existing skill archive is missing: {existing_zip}")

    markdown_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="existing-skill-", dir=markdown_dir.parent) as temporary:
        extract_dir = Path(temporary)
        with zipfile.ZipFile(existing_zip) as archive:
            roots: set[str] = set()
            members: set[str] = set()
            for member in archive.infolist():
                parts = validate_archive_member(member)
                roots.add(parts[0])
                normalized = "/".join(parts)
                if normalized in members:
                    raise ValueError(f"duplicate path in existing skill archive: {member.filename}")
                members.add(normalized)
            if len(roots) != 1:
                raise ValueError("existing skill archive must contain exactly one skill root directory")
            archive.extractall(extract_dir)

        skill_root = extract_dir / next(iter(roots))
        if not (skill_root / "SKILL.md").is_file():
            raise ValueError("existing skill archive is missing SKILL.md")
        index_path = skill_root / "references" / "doc-index.jsonl"
        existing_markdown_dir = skill_root / "references" / "markdown"
        if not index_path.is_file() or not existing_markdown_dir.is_dir():
            raise ValueError("existing skill archive is missing document references")

        index_rows = load_jsonl(index_path)
        documents: dict[str, dict] = {}
        for line_number, row in enumerate(index_rows, 1):
            source = row.get("source")
            if not isinstance(source, dict):
                raise ValueError(f"existing index source at line {line_number} must be an object")
            document_id = str(source.get("document_id", "")).strip()
            source_name = str(source.get("source_name", "")).strip()
            source_markdown = str(source.get("source_markdown", "")).strip()
            if not document_id or not source_name or not source_markdown:
                raise ValueError(f"existing index source is incomplete at line {line_number}")
            markdown_parts = safe_archive_parts(source_markdown)
            if (
                len(markdown_parts) != 3
                or markdown_parts[:2] != ("references", "markdown")
                or Path(markdown_parts[2]).suffix.lower() != ".md"
                or not (existing_markdown_dir / markdown_parts[2]).is_file()
            ):
                raise ValueError(f"existing index Markdown path is invalid at line {line_number}")
            record = {
                "document_id": document_id,
                "source_name": source_name,
                "markdown_name": markdown_parts[2],
            }
            previous = documents.get(document_id)
            if previous and (
                previous["source_name"] != source_name
                or previous["markdown_name"] != markdown_parts[2]
            ):
                raise ValueError(f"existing index has conflicting document records: {document_id}")
            documents[document_id] = record

        shutil.copytree(existing_markdown_dir, markdown_dir, dirs_exist_ok=True)
        existing_assets_dir = skill_root / "references" / "assets"
        if existing_assets_dir.is_dir():
            shutil.copytree(existing_assets_dir, assets_dir, dirs_exist_ok=True)
        for document in documents.values():
            target = markdown_dir / document["markdown_name"]
            if not target.is_file():
                raise ValueError(f"existing Markdown is missing: {target.name}")
            document["markdown_path"] = target.as_posix()
            document.pop("markdown_name")

    return list(documents.values())


def merge_document_manifests(markdown_dir: Path, existing_documents: list[dict]) -> None:
    manifest = markdown_dir / "documents.jsonl"
    new_documents = load_jsonl(manifest)
    combined = [*existing_documents, *new_documents]
    document_ids: set[str] = set()
    markdown_paths: set[Path] = set()
    for record in combined:
        document_id = str(record.get("document_id", "")).strip()
        markdown_path = Path(str(record.get("markdown_path", ""))).resolve()
        if not document_id or document_id in document_ids:
            raise ValueError(f"duplicate or empty document ID while updating: {document_id}")
        if markdown_path in markdown_paths:
            raise ValueError(f"duplicate Markdown path while updating: {markdown_path}")
        document_ids.add(document_id)
        markdown_paths.add(markdown_path)
    write_jsonl(manifest, combined)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a standalone DocToSkill workflow.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--chunks", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--online-ocr", action="store_true")
    parser.add_argument("--existing-skill", type=Path)
    parser.add_argument("--max-chars", type=int, default=5000)
    parser.add_argument("--max-batch-chars", type=int, default=15000)
    parser.add_argument("--max-batch-chunks", type=int, default=8)
    args = parser.parse_args()
    if args.max_chars <= 0 or args.max_batch_chars <= 0 or args.max_batch_chunks <= 0:
        parser.error("character and batch limits must be positive")
    return args


def main() -> int:
    args = parse_args()
    manifest = args.chunks / "chunks.jsonl"
    if args.state.is_file():
        if not manifest.is_file():
            raise ValueError(f"workflow state exists but the chunk manifest is missing: {manifest}")
        state = load_state(args.state, manifest)
        print(json.dumps(compact_state(state, resumed=True), ensure_ascii=False, separators=(",", ":")))
        return 0

    existing_documents: list[dict] = []
    if args.existing_skill:
        existing_documents = stage_existing_skill(
            args.existing_skill,
            args.markdown,
            args.assets,
        )

    convert_arguments = [
        "--input", str(args.input),
        "--output", str(args.markdown),
        "--assets", str(args.assets),
        "--log", str(args.log),
    ]
    if args.online_ocr:
        convert_arguments.append("--online-ocr")
    run_stage("convert_documents.py", convert_arguments)
    if existing_documents:
        merge_document_manifests(args.markdown, existing_documents)
    run_stage("split_markdown.py", [
        "--input", str(args.markdown),
        "--output", str(args.chunks),
        "--max-chars", str(args.max_chars),
        "--log", str(args.log),
    ])
    run_stage("batch_index.py", [
        "--plan",
        "--manifest", str(manifest),
        "--state", str(args.state),
        "--max-batch-chars", str(args.max_batch_chars),
        "--max-batch-chunks", str(args.max_batch_chunks),
        "--log", str(args.log),
    ])
    state = load_state(args.state, manifest)
    print(json.dumps(compact_state(state, resumed=False), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
