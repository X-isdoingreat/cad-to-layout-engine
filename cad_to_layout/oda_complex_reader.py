from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .bounded_inspect import (
    _make_memory_controller,
    _posix_limit,
    _process_private_bytes,
    _terminate_worker,
)


DEFAULT_MEMORY_LIMIT_BYTES = 4 * 1024**3
DEFAULT_TIMEOUT_SECONDS = 900.0
TERMINAL_RECEIPT_NAME = "complex_dwg_oda_receipt.json"
FAILURE_RECEIPT_NAME = "complex_dwg_oda_failure.json"
MODELSPACE_RECORDS_NAME = "complex_dwg_modelspace.jsonl"
PROXY_IDENTITY_RECORDS_NAME = "complex_dwg_proxy_identities.jsonl"
RUN_RECEIPT_NAME = "run_receipt.json"

_ODA_ENV_KEYS = (
    "ODA_FILE_CONVERTER",
    "ODA_FILE_CONVERTER_PATH",
    "ODAFILECONVERTER_PATH",
)
_CONTROL_RECORDS = {
    "SECTION",
    "ENDSEC",
    "EOF",
    "TABLE",
    "ENDTAB",
    "BLOCK",
    "ENDBLK",
    "SEQEND",
}
_TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
_EXTERNAL_REFERENCE_TYPES = {
    "IMAGE",
    "IMAGEDEF",
    "DGNUNDERLAY",
    "DWFUNDERLAY",
    "PDFUNDERLAY",
    "UNDERLAY",
}
_LAUNCHER_CODE = r"""
import os
import subprocess
import sys
import time

gate = sys.argv[1]
deadline = time.monotonic() + 30.0
while not os.path.isfile(gate):
    if time.monotonic() >= deadline:
        raise SystemExit(125)
    time.sleep(0.01)
raise SystemExit(subprocess.call(sys.argv[2:]))
""".strip()


@dataclass(frozen=True)
class OdaConverterConfig:
    executable: Path
    output_version: str = "ACAD2018"
    output_type: str = "DXF"
    recursive: bool = False
    audit: bool = False

    def command(self, source: Path, output_dir: Path) -> list[str]:
        return _build_oda_command(self, source, output_dir)


class OdaComplexReadError(RuntimeError):
    def __init__(self, kind: str, stage: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.stage = stage


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=True, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    stat_before = path.stat()
    digest = _sha256(path)
    stat_after = path.stat()
    if (stat_before.st_size, stat_before.st_mtime_ns) != (
        stat_after.st_size,
        stat_after.st_mtime_ns,
    ):
        raise OdaComplexReadError(
            "source_changed",
            "source_identity",
            f"file changed while its identity was recorded: {path}",
        )
    return {
        "path": str(path),
        "size_bytes": stat_after.st_size,
        "modified_time_ns": stat_after.st_mtime_ns,
        "sha256": digest,
    }


def _same_source(path: Path, identity: Mapping[str, Any]) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    return (
        stat.st_size == identity.get("size_bytes")
        and stat.st_mtime_ns == identity.get("modified_time_ns")
    )


def _candidate_executable(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None or not str(value).strip():
        return None
    candidate = Path(value).expanduser()
    if candidate.is_dir():
        candidate = candidate / "ODAFileConverter.exe"
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_file() else None


def discover_oda_file_converter(
    explicit: str | os.PathLike[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    """Find a local ODA File Converter without downloading or installing it."""

    direct = _candidate_executable(explicit)
    if explicit is not None:
        return direct

    env = os.environ if environment is None else environment
    for key in _ODA_ENV_KEYS:
        candidate = _candidate_executable(env.get(key))
        if candidate is not None:
            return candidate

    roots: list[Path] = []
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        value = env.get(key)
        if value:
            roots.append(Path(value))
    patterns = (
        "ODA/ODAFileConverter*/ODAFileConverter.exe",
        "Programs/ODA/ODAFileConverter*/ODAFileConverter.exe",
        "Open Design Alliance/ODAFileConverter*/ODAFileConverter.exe",
        "ODAFileConverter*/ODAFileConverter.exe",
    )
    candidates: list[Path] = []
    for root in roots:
        for pattern in patterns:
            try:
                candidates.extend(path.resolve() for path in root.glob(pattern) if path.is_file())
            except OSError:
                continue
    if not candidates:
        return None
    return sorted(set(candidates), key=lambda path: str(path).casefold())[-1]


def resolve_oda_config(
    converter_path: str | os.PathLike[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    audit: bool = False,
) -> OdaConverterConfig:
    executable = discover_oda_file_converter(converter_path, environment=environment)
    if executable is None:
        requested = str(converter_path) if converter_path is not None else "configured/install paths"
        raise OdaComplexReadError(
            "converter_unavailable",
            "converter_discovery",
            f"ODA File Converter was not found at {requested}",
        )
    return OdaConverterConfig(executable=executable, audit=bool(audit))


def _build_oda_command(config: OdaConverterConfig, source: Path, output_dir: Path) -> list[str]:
    # ODA File Converter CLI contract:
    # input folder, output folder, output version, output type, recurse, audit, filter.
    return [
        str(config.executable),
        str(source.parent),
        str(output_dir),
        config.output_version,
        config.output_type,
        "1" if config.recursive else "0",
        "1" if config.audit else "0",
        source.name,
    ]


def _launcher_command(command: Sequence[str], gate: Path) -> list[str]:
    return [sys.executable, "-X", "utf8", "-c", _LAUNCHER_CODE, str(gate), *command]


def _tail(path: Path, limit: int = 16 * 1024) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _run_bounded_command(
    command: Sequence[str],
    staging: Path,
    *,
    memory_limit_bytes: int,
    timeout_seconds: float,
    environment: Mapping[str, str] | None,
) -> dict[str, Any]:
    gate = staging / "converter.start.gate"
    stdout_path = staging / "conversion.stdout.log"
    stderr_path = staging / "conversion.stderr.log"
    controller = _make_memory_controller(memory_limit_bytes)
    process: subprocess.Popen[bytes] | None = None
    peak = 0
    timed_out = False
    supervisor_error: str | None = None
    return_code: int | None = None
    started = time.monotonic()
    child_environment = os.environ.copy()
    if environment:
        child_environment.update(environment)
    # ODA File Converter is a Qt application even in documented command-line
    # mode.  Forcing Qt's ``offscreen`` platform makes the real Windows binary
    # open an idle application window instead of consuming the CLI arguments.
    # ``CREATE_NO_WINDOW`` already suppresses a console window and does not
    # alter ODA's Qt platform selection.

    try:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                _launcher_command(command, gate),
                cwd=str(staging),
                env=child_environment,
                stdout=stdout,
                stderr=stderr,
                creationflags=creationflags,
                preexec_fn=_posix_limit(memory_limit_bytes),
                start_new_session=os.name != "nt",
            )
            try:
                controller.assign(process)
            except Exception as exc:
                supervisor_error = f"cannot assign converter launcher to memory job: {type(exc).__name__}: {exc}"
                _terminate_worker(process, controller)
            else:
                gate.write_bytes(b"start\n")
                deadline = time.monotonic() + timeout_seconds
                while process.poll() is None:
                    peak = max(peak, controller.peak_bytes(), _process_private_bytes(process))
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        _terminate_worker(process, controller)
                        break
                    time.sleep(min(0.05, remaining))
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_worker(process, controller)
                return_code = process.wait(timeout=5)
            peak = max(peak, controller.peak_bytes())
    except Exception as exc:
        supervisor_error = f"{type(exc).__name__}: {exc}"
        if process is not None and process.poll() is None:
            _terminate_worker(process, controller)
    finally:
        peak = max(peak, controller.peak_bytes())
        controller.close()

    elapsed = time.monotonic() - started
    stdout_tail = _tail(stdout_path)
    stderr_tail = _tail(stderr_path)
    unsigned_code = None if return_code is None else return_code & 0xFFFFFFFF
    if timed_out:
        failure_kind = "timeout"
    elif supervisor_error:
        failure_kind = "supervisor_error"
    elif (
        "out of memory" in stderr_tail.casefold()
        or unsigned_code in {0xC0000017, 0xC000012D}
        or (return_code not in {None, 0} and peak >= int(memory_limit_bytes * 0.90))
    ):
        failure_kind = "out_of_memory_under_process_limit"
    elif return_code not in {None, 0}:
        failure_kind = "converter_failed"
    else:
        failure_kind = None
    return {
        "status": "complete" if failure_kind is None else "controlled_failure",
        "failure_kind": failure_kind,
        "supervisor_error": supervisor_error,
        "return_code": return_code,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "elapsed_seconds": round(elapsed, 6),
        "peak_private_memory_bytes": peak,
        "memory_limit_bytes": memory_limit_bytes,
        "timeout_seconds": timeout_seconds,
        "memory_limit_enforcement": "windows_job_object" if os.name == "nt" else "posix_rlimit_as",
    }


def _iter_raw_tags(path: Path) -> Iterator[tuple[int, int, bytes]]:
    with path.open("rb") as stream:
        pair_index = 0
        first_line = True
        while True:
            code_line = stream.readline()
            if code_line == b"":
                return
            if first_line:
                code_line = code_line.removeprefix(b"\xef\xbb\xbf")
                first_line = False
            if not code_line.strip():
                continue
            value_line = stream.readline()
            if value_line == b"":
                raise OdaComplexReadError(
                    "invalid_ascii_dxf",
                    "dxf_scan",
                    f"DXF ends with an incomplete group-code pair at pair {pair_index + 1}",
                )
            pair_index += 1
            try:
                code = int(code_line.strip().decode("ascii", "strict"))
            except (UnicodeError, ValueError) as exc:
                raise OdaComplexReadError(
                    "invalid_ascii_dxf",
                    "dxf_scan",
                    f"invalid DXF group code at pair {pair_index}",
                ) from exc
            yield pair_index, code, value_line.rstrip(b"\r\n")


def _detect_dxf_encoding(path: Path) -> tuple[str | None, str | None, str]:
    with path.open("rb") as stream:
        signature = stream.read(22)
    if signature.startswith(b"AutoCAD Binary DXF") or b"\x00" in signature:
        raise OdaComplexReadError(
            "binary_dxf_not_supported",
            "dxf_scan",
            "ODA output is binary DXF; AC1032 ASCII DXF is required",
        )
    acad_version: str | None = None
    codepage: str | None = None
    expected: bytes | None = None
    for _index, code, raw in _iter_raw_tags(path):
        if code == 9 and raw in {b"$ACADVER", b"$DWGCODEPAGE"}:
            expected = raw
            continue
        if expected == b"$ACADVER" and code == 1:
            acad_version = raw.decode("ascii", "replace").strip()
            expected = None
        elif expected == b"$DWGCODEPAGE" and code == 3:
            codepage = raw.decode("ascii", "replace").strip()
            expected = None
        if acad_version and codepage:
            break
    if acad_version and acad_version >= "AC1021":
        encoding = "utf-8"
    else:
        try:
            from ezdxf.tools.codepage import toencoding

            encoding = toencoding(codepage or "ANSI_1252")
        except Exception:
            encoding = "cp1252"
    return acad_version, codepage, encoding


def _decode_tag(raw: bytes, encoding: str) -> tuple[str, str | None]:
    try:
        return raw.decode(encoding, "strict"), None
    except (LookupError, UnicodeDecodeError):
        return raw.decode(encoding, "backslashreplace"), raw.hex()


def _first(tags: Sequence[Mapping[str, Any]], code: int, default: Any = None) -> Any:
    for tag in tags:
        if tag["code"] == code:
            return tag["value"]
    return default


def _values(tags: Sequence[Mapping[str, Any]], codes: Iterable[int]) -> list[str]:
    accepted = set(codes)
    return [str(tag["value"]) for tag in tags if int(tag["code"]) in accepted]


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return default


def _bounded_sample(target: list[dict[str, Any]], row: dict[str, Any], limit: int) -> None:
    if len(target) < limit:
        target.append(row)


def scan_ascii_dxf(
    dxf_path: str | Path,
    modelspace_jsonl_path: str | Path,
    *,
    proxy_identity_jsonl_path: str | Path | None = None,
    source_dwg_sha256: str | None = None,
    sample_limit: int = 100,
) -> dict[str, Any]:
    """Scan every ASCII DXF tag/record while retaining only one record in memory.

    Every model-space entity is written to JSONL with all of its source tags.
    Summary samples are bounded. Complete model-space records and compact
    identities for every proxy/unsupported object remain in separate JSONL
    streams, so proxy evidence is not truncated by ``sample_limit``.
    """

    source = Path(dxf_path).resolve()
    modelspace_path = Path(modelspace_jsonl_path).resolve()
    proxy_identity_path = (
        Path(proxy_identity_jsonl_path).resolve()
        if proxy_identity_jsonl_path is not None
        else modelspace_path.with_name(PROXY_IDENTITY_RECORDS_NAME)
    )
    if proxy_identity_path == modelspace_path:
        raise ValueError("proxy_identity_jsonl_path must differ from modelspace_jsonl_path")
    if sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")
    if not source.is_file():
        raise OdaComplexReadError("converted_dxf_missing", "dxf_scan", f"DXF does not exist: {source}")
    identity = _file_identity(source)
    acad_version, codepage, encoding = _detect_dxf_encoding(source)
    if acad_version != "AC1032":
        raise OdaComplexReadError(
            "unexpected_dxf_version",
            "dxf_scan",
            f"expected AC1032 ASCII DXF, found {acad_version or 'no_evidence'}",
        )

    tag_count = 0
    decode_error_count = 0
    all_record_count = 0
    section = "OUTSIDE"
    active_block: dict[str, Any] | None = None
    section_tag_counts: Counter[str] = Counter()
    section_record_counts: Counter[str] = Counter()
    record_type_counts: dict[str, Counter[str]] = defaultdict(Counter)
    entity_type_counts: Counter[str] = Counter()
    layer_names: set[str] = set()
    layer_table_names: set[str] = set()
    blocks: list[dict[str, Any]] = []
    duplicate_block_name_count = 0
    seen_blocks: set[str] = set()
    modelspace_count = 0
    paperspace_count = 0
    insert_count = 0
    text_count = 0
    dimension_count = 0
    proxy_count = 0
    proxy_identity_count = 0
    xref_count = 0
    insert_samples: list[dict[str, Any]] = []
    text_samples: list[dict[str, Any]] = []
    dimension_samples: list[dict[str, Any]] = []
    proxy_samples: list[dict[str, Any]] = []
    xref_samples: list[dict[str, Any]] = []
    header_variables: dict[str, list[dict[str, Any]]] = {}
    current_type: str | None = None
    current_tags: list[dict[str, Any]] = []
    saw_eof = False
    preamble_tag_count = 0

    modelspace_path.parent.mkdir(parents=True, exist_ok=True)
    proxy_identity_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = modelspace_path.with_name(f".{modelspace_path.name}.{uuid.uuid4().hex}.tmp")
    proxy_temporary = proxy_identity_path.with_name(
        f".{proxy_identity_path.name}.{uuid.uuid4().hex}.tmp"
    )

    def consume(
        record_type: str,
        tags: list[dict[str, Any]],
        stream: Any,
        proxy_stream: Any,
    ) -> None:
        nonlocal section, active_block, all_record_count, saw_eof
        nonlocal duplicate_block_name_count, modelspace_count, paperspace_count
        nonlocal insert_count, text_count, dimension_count, proxy_count, proxy_identity_count, xref_count

        record_type = record_type.strip().upper()
        record_section = section
        if saw_eof:
            raise OdaComplexReadError(
                "invalid_ascii_dxf",
                "dxf_scan",
                f"record {record_type!r} appears after EOF",
            )
        all_record_count += 1

        if record_type == "SECTION":
            section = str(_first(tags, 2, "UNKNOWN")).strip().upper() or "UNKNOWN"
            section_tag_counts[section] += len(tags)
            section_record_counts[section] += 1
            record_type_counts[section][record_type] += 1
            if section == "HEADER":
                current_name: str | None = None
                for tag in tags:
                    code = int(tag["code"])
                    value = str(tag["value"])
                    if code == 9:
                        current_name = value
                        header_variables.setdefault(current_name, [])
                    elif current_name is not None and code not in {0, 2}:
                        header_variables[current_name].append({"code": code, "value": value})
            return
        section_tag_counts[record_section] += len(tags)
        section_record_counts[record_section] += 1
        record_type_counts[record_section][record_type] += 1
        if record_type == "ENDSEC":
            section = "OUTSIDE"
            active_block = None
            return
        if record_type == "EOF":
            saw_eof = True
            return

        handle = str(_first(tags, 5, "") or "").strip().upper() or None
        layer = str(_first(tags, 8, "") or "").strip() or None
        owner = str(_first(tags, 330, "") or "").strip().upper() or None
        if layer:
            layer_names.add(layer)

        if record_section == "TABLES" and record_type == "LAYER":
            name = str(_first(tags, 2, "") or "").strip()
            if name:
                layer_table_names.add(name)
                layer_names.add(name)

        if record_section == "BLOCKS" and record_type == "BLOCK":
            name = str(_first(tags, 2, "") or "").strip() or "<NO_NAME>"
            flags = _integer(_first(tags, 70, 0))
            path = str(_first(tags, 1, "") or "").strip() or None
            folded = name.casefold()
            if folded in seen_blocks:
                duplicate_block_name_count += 1
            seen_blocks.add(folded)
            active_block = {
                "name": name,
                "handle": handle,
                "flags": flags,
                "xref": bool(flags & (4 | 8)),
                "xref_path": path,
                "entity_count": 0,
            }
            blocks.append(active_block)
            if active_block["xref"]:
                xref_count += 1
                _bounded_sample(
                    xref_samples,
                    {
                        "kind": "block_xref",
                        "block_name": name,
                        "handle": handle,
                        "flags": flags,
                        "path": path,
                    },
                    sample_limit,
                )
            return
        if record_section == "BLOCKS" and record_type == "ENDBLK":
            active_block = None
            return
        if record_section == "BLOCKS" and active_block is not None and record_type not in _CONTROL_RECORDS:
            active_block["entity_count"] += 1

        is_entity = record_section in {"ENTITIES", "BLOCKS", "OBJECTS"} and record_type not in _CONTROL_RECORDS
        if is_entity:
            entity_type_counts[record_type] += 1

        common = {
            "section": record_section,
            "source_block": active_block["name"] if active_block else None,
            "source_block_handle": active_block["handle"] if active_block else None,
            "entity_type": record_type,
            "handle": handle,
            "layer": layer,
            "owner_handle": owner,
        }

        if record_type == "INSERT":
            insert_count += 1
            _bounded_sample(
                insert_samples,
                {
                    **common,
                    "block_name": str(_first(tags, 2, "") or "").strip() or None,
                    "position": {
                        "x": _number(_first(tags, 10)),
                        "y": _number(_first(tags, 20)),
                        "z": _number(_first(tags, 30)),
                    },
                    "rotation": _number(_first(tags, 50)),
                },
                sample_limit,
            )

        if record_type in _TEXT_TYPES:
            text_count += 1
            _bounded_sample(
                text_samples,
                {**common, "text": "".join(_values(tags, (3, 1)))},
                sample_limit,
            )

        if record_type == "DIMENSION" or record_type.endswith("DIMENSION"):
            dimension_count += 1
            _bounded_sample(
                dimension_samples,
                {
                    **common,
                    "dimension_type": _integer(_first(tags, 70, 0)),
                    "text": str(_first(tags, 1, "") or ""),
                    "actual_measurement": _number(_first(tags, 42)),
                    "definition_point": {
                        "x": _number(_first(tags, 10)),
                        "y": _number(_first(tags, 20)),
                        "z": _number(_first(tags, 30)),
                    },
                },
                sample_limit,
            )

        subclasses = _values(tags, (100,))
        if "PROXY" in record_type or any("PROXY" in value.upper() for value in subclasses):
            proxy_count += 1
            _bounded_sample(proxy_samples, common, sample_limit)
            proxy_row = {
                "schema_version": "1.0",
                "record_id": f"DXF-PROXY-{proxy_count:09d}",
                "record_index": all_record_count,
                "source_dwg_sha256": source_dwg_sha256,
                "intermediate_dxf_sha256": identity["sha256"],
                **common,
                "subclass_markers": subclasses,
                "detection_basis": (
                    "entity_type_and_subclass_marker"
                    if "PROXY" in record_type
                    and any("PROXY" in value.upper() for value in subclasses)
                    else "entity_type"
                    if "PROXY" in record_type
                    else "subclass_marker"
                ),
                "payload_decode_status": "not_decoded_by_ascii_dxf_scanner",
            }
            proxy_stream.write(json.dumps(proxy_row, ensure_ascii=True, separators=(",", ":")))
            proxy_stream.write("\n")
            proxy_identity_count += 1

        if record_type in _EXTERNAL_REFERENCE_TYPES:
            xref_count += 1
            _bounded_sample(
                xref_samples,
                {**common, "kind": record_type.casefold(), "path": _first(tags, 1)},
                sample_limit,
            )

        if record_section == "ENTITIES" and record_type not in _CONTROL_RECORDS:
            paper_flag = _integer(_first(tags, 67, 0))
            layout_name = str(_first(tags, 410, "") or "").strip()
            is_paper = paper_flag == 1 or (layout_name and layout_name.casefold() != "model")
            if is_paper:
                paperspace_count += 1
                return
            modelspace_count += 1
            row = {
                "schema_version": "1.0",
                "record_id": f"DXF-MSP-{modelspace_count:09d}",
                "source_dwg_sha256": source_dwg_sha256,
                "intermediate_dxf_sha256": identity["sha256"],
                **common,
                "space": "modelspace",
                "tags": tags,
            }
            stream.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")))
            stream.write("\n")

    try:
        with (
            temporary.open("w", encoding="utf-8", newline="\n") as output,
            proxy_temporary.open("w", encoding="utf-8", newline="\n") as proxy_output,
        ):
            for _pair_index, code, raw in _iter_raw_tags(source):
                tag_count += 1
                value, raw_hex = _decode_tag(raw, encoding)
                if raw_hex is not None:
                    decode_error_count += 1
                tag: dict[str, Any] = {"code": code, "value": value}
                if raw_hex is not None:
                    tag["raw_hex"] = raw_hex
                if code == 0:
                    if current_type is not None:
                        consume(current_type, current_tags, output, proxy_output)
                    current_type = value
                    current_tags = [tag]
                elif current_type is None:
                    # Preamble comments remain covered by tag counts. Valid DXF
                    # entity records start with group code 0.
                    preamble_tag_count += 1
                    continue
                else:
                    current_tags.append(tag)
            if current_type is not None:
                consume(current_type, current_tags, output, proxy_output)
            if not saw_eof:
                raise OdaComplexReadError(
                    "invalid_ascii_dxf",
                    "dxf_scan",
                    "ASCII DXF has no terminal EOF record",
                )
            if section != "OUTSIDE":
                raise OdaComplexReadError(
                    "invalid_ascii_dxf",
                    "dxf_scan",
                    f"ASCII DXF ended while section {section!r} was still open",
                )
            output.flush()
            os.fsync(output.fileno())
            proxy_output.flush()
            os.fsync(proxy_output.fileno())
        os.replace(temporary, modelspace_path)
        os.replace(proxy_temporary, proxy_identity_path)
    finally:
        temporary.unlink(missing_ok=True)
        proxy_temporary.unlink(missing_ok=True)

    modelspace_identity = _file_identity(modelspace_path)
    proxy_identity = _file_identity(proxy_identity_path)
    reconciled_records = sum(section_record_counts.values()) == all_record_count
    if not reconciled_records:
        raise OdaComplexReadError(
            "scan_reconciliation_failed",
            "dxf_scan",
            "section record counts do not reconcile with the full record count",
        )
    proxy_records_reconciled = proxy_count == proxy_identity_count
    if not proxy_records_reconciled:
        raise OdaComplexReadError(
            "scan_reconciliation_failed",
            "dxf_scan",
            "proxy identity record count does not reconcile with the detected proxy count",
        )
    return {
        "schema_version": "1.0",
        "status": "complete",
        "capability_boundary": (
            "lossless_tag_inventory_plus_modelspace_record_stream_plus_complete_proxy_identity_stream"
        ),
        "intermediate_dxf": {
            **identity,
            "format": "ASCII_DXF",
            "acad_version": acad_version,
            "dwg_codepage": codepage,
            "decoded_encoding": encoding,
        },
        "coverage": {
            "tag_count": tag_count,
            "preamble_tag_count": preamble_tag_count,
            "all_record_count": all_record_count,
            "section_tag_counts": dict(sorted(section_tag_counts.items())),
            "section_record_counts": dict(sorted(section_record_counts.items())),
            "record_type_counts_by_section": {
                name: dict(counter.most_common()) for name, counter in sorted(record_type_counts.items())
            },
            "entity_type_counts": dict(entity_type_counts.most_common()),
            "record_counts_reconciled": reconciled_records,
            "decode_error_count": decode_error_count,
        },
        "layers": {
            "count": len(layer_names),
            "names": sorted(layer_names, key=str.casefold),
            "table_count": len(layer_table_names),
            "table_names": sorted(layer_table_names, key=str.casefold),
        },
        "header_variables": header_variables,
        "blocks": {
            "count": len(blocks),
            "duplicate_name_count": duplicate_block_name_count,
            "definitions": blocks,
        },
        "modelspace": {
            "record_count": modelspace_count,
            "paperspace_record_count": paperspace_count,
            "records_path": str(modelspace_path),
            "records_size_bytes": modelspace_identity["size_bytes"],
            "records_sha256": modelspace_identity["sha256"],
            "records_are_complete_all_tag_rows": True,
        },
        "proxy_objects": {
            "record_count": proxy_identity_count,
            "records_path": str(proxy_identity_path),
            "records_size_bytes": proxy_identity["size_bytes"],
            "records_sha256": proxy_identity["sha256"],
            "records_count_reconciled": proxy_records_reconciled,
            "records_are_complete_compact_identities": True,
        },
        "evidence": {
            "sample_limit": sample_limit,
            "insert_count": insert_count,
            "insert_samples": insert_samples,
            "insert_samples_truncated": insert_count > len(insert_samples),
            "text_count": text_count,
            "text_samples": text_samples,
            "text_samples_truncated": text_count > len(text_samples),
            "dimension_count": dimension_count,
            "dimension_samples": dimension_samples,
            "dimension_samples_truncated": dimension_count > len(dimension_samples),
            "proxy_count": proxy_count,
            "proxy_samples": proxy_samples,
            "proxy_samples_truncated": proxy_count > len(proxy_samples),
            "xref_count": xref_count,
            "xref_samples": xref_samples,
            "xref_samples_truncated": xref_count > len(xref_samples),
        },
    }


def _locate_converted_dxf(directory: Path, source: Path) -> Path:
    matches = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.casefold() == ".dxf" and path.stem.casefold() == source.stem.casefold()
    ]
    if len(matches) != 1:
        raise OdaComplexReadError(
            "converted_dxf_missing" if not matches else "ambiguous_converted_dxf",
            "conversion_output",
            f"expected exactly one converted DXF for {source.name}, found {len(matches)}",
        )
    return matches[0]


def _safe_move_run(staging: Path, destination: Path, output_root: Path) -> None:
    staging = staging.resolve()
    destination = destination.resolve()
    output_root = output_root.resolve()
    if staging.parent != output_root or destination.parent.parent != output_root:
        raise RuntimeError("refusing to move a run outside the declared output directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, destination)


def _preserve_failed_run(staging: Path, output_root: Path, run_id: str) -> Path | None:
    if not staging.exists():
        return None
    failed = output_root / "failed_runs" / run_id
    try:
        _safe_move_run(staging, failed, output_root)
    except OSError:
        return None
    return failed


def run_oda_complex_read(
    source: str | Path,
    output_dir: str | Path,
    *,
    converter_path: str | os.PathLike[str] | None = None,
    memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    audit: bool = False,
    environment: Mapping[str, str] | None = None,
    sample_limit: int = 100,
) -> dict[str, Any]:
    """Convert one DWG to AC1032 ASCII DXF and stream-scan it atomically."""

    if memory_limit_bytes <= 0:
        raise ValueError("memory_limit_bytes must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    source_path = Path(source).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    terminal_path = output / TERMINAL_RECEIPT_NAME
    failure_path = output / FAILURE_RECEIPT_NAME
    previous_terminal = {
        "path": str(terminal_path),
        "exists": terminal_path.is_file(),
        "sha256": _sha256(terminal_path) if terminal_path.is_file() else None,
    }
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    staging = Path(tempfile.mkdtemp(prefix=".oda-complex-", dir=output))
    stage = "source_identity"
    source_identity: dict[str, Any] = {"path": str(source_path)}
    process_result: dict[str, Any] | None = None

    def fail(kind: str, message: str, failure_stage: str) -> dict[str, Any]:
        failed_run = _preserve_failed_run(staging, output, run_id)
        receipt = {
            "schema_version": "1.0",
            "status": "controlled_failure",
            "run_id": run_id,
            "failure_kind": kind,
            "failure_stage": failure_stage,
            "message": message,
            "source": source_identity,
            "resources": process_result
            or {
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "memory_limit_bytes": memory_limit_bytes,
                "timeout_seconds": timeout_seconds,
            },
            "failed_run_path": str(failed_run) if failed_run else None,
            "partial_artifacts_not_accepted": True,
            "terminal_receipt": {
                "updated": False,
                "previous_preserved": previous_terminal,
            },
        }
        _atomic_write_json(failure_path, receipt)
        return receipt

    try:
        if not source_path.is_file():
            return fail("source_invalid", "input DWG does not exist or is not a file", stage)
        source_identity = _file_identity(source_path)
        with source_path.open("rb") as stream:
            source_identity["format_version"] = stream.read(6).decode("ascii", "replace")
        if source_identity["format_version"] != "AC1032":
            return fail(
                "unsupported_source_version",
                f"expected AC1032 DWG, found {source_identity['format_version']}",
                stage,
            )

        stage = "converter_discovery"
        config = resolve_oda_config(converter_path, environment=environment, audit=audit)
        converter_identity = _file_identity(config.executable)
        converted_dir = staging / "converted"
        converted_dir.mkdir(parents=True, exist_ok=False)

        stage = "bounded_conversion"
        command = config.command(source_path, converted_dir)
        process_result = _run_bounded_command(
            command,
            staging,
            memory_limit_bytes=memory_limit_bytes,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        if process_result["status"] != "complete":
            return fail(
                str(process_result.get("failure_kind") or "converter_failed"),
                "ODA File Converter did not complete successfully",
                stage,
            )
        if not _same_source(source_path, source_identity):
            return fail("source_changed", "source DWG changed during conversion", stage)

        stage = "conversion_output"
        converted = _locate_converted_dxf(converted_dir, source_path)
        modelspace = staging / MODELSPACE_RECORDS_NAME
        proxy_identities = staging / PROXY_IDENTITY_RECORDS_NAME

        stage = "dxf_scan"
        scan = scan_ascii_dxf(
            converted,
            modelspace,
            proxy_identity_jsonl_path=proxy_identities,
            source_dwg_sha256=source_identity["sha256"],
            sample_limit=sample_limit,
        )
        if scan["coverage"]["record_counts_reconciled"] is not True:
            return fail("scan_reconciliation_failed", "DXF record counts did not reconcile", stage)

        stage = "publication"
        accepted = output / "accepted_runs" / run_id
        final_converted = accepted / "converted" / converted.name
        final_modelspace = accepted / MODELSPACE_RECORDS_NAME
        final_proxy_identities = accepted / PROXY_IDENTITY_RECORDS_NAME
        scan["modelspace"]["records_path"] = str(final_modelspace)
        scan["proxy_objects"]["records_path"] = str(final_proxy_identities)
        scan["intermediate_dxf"]["path"] = str(final_converted)
        run_receipt = {
            "schema_version": "1.0",
            "status": "complete",
            "run_id": run_id,
            "backend": {
                "name": "ODA File Converter",
                "executable": converter_identity,
                "requested_output_version": config.output_version,
                "requested_output_type": "ASCII_DXF",
                "audit_enabled": config.audit,
                "command": command,
            },
            "source": source_identity,
            "conversion": process_result,
            "scan": scan,
            "artifacts": {
                "converted_ascii_dxf": str(final_converted),
                "modelspace_records_jsonl": str(final_modelspace),
                "proxy_identity_records_jsonl": str(final_proxy_identities),
                "proxy_identity_records": {
                    "path": str(final_proxy_identities),
                    "record_count": scan["proxy_objects"]["record_count"],
                    "size_bytes": scan["proxy_objects"]["records_size_bytes"],
                    "sha256": scan["proxy_objects"]["records_sha256"],
                },
                "stdout_log": str(accepted / "conversion.stdout.log"),
                "stderr_log": str(accepted / "conversion.stderr.log"),
            },
            "capability_boundary": (
                "conversion, lossless_DXF_tag_inventory, complete model-space records, and complete "
                "proxy identities only; business device classification, semantic writeback, and final "
                "source-aligned CAD remain downstream gates"
            ),
        }
        _atomic_write_json(staging / RUN_RECEIPT_NAME, run_receipt)
        _safe_move_run(staging, accepted, output)
        terminal = {
            **run_receipt,
            "run_receipt": {
                "path": str(accepted / RUN_RECEIPT_NAME),
                "sha256": _sha256(accepted / RUN_RECEIPT_NAME),
            },
            "elapsed_seconds_total": round(time.monotonic() - started, 6),
        }
        try:
            failure_path.unlink(missing_ok=True)
        except OSError:
            pass
        _atomic_write_json(terminal_path, terminal)
        return terminal
    except OdaComplexReadError as exc:
        return fail(exc.kind, str(exc), exc.stage)
    except Exception as exc:
        return fail("unexpected_error", f"{type(exc).__name__}: {exc}", stage)
