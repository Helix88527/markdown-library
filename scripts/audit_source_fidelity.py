"""为最终 Markdown 建立并校验原材料覆盖清单。

这个脚本不判断整理者写得是否优美，也不声称能够自动理解全部语义。它把
“逐段读原材料、逐项指出成稿落点”变成可复核的机械门：来源被连续分块，
每块必须登记具体信息单元；数字和问句必须逐项处理；每个保留项必须提供
最终 Markdown 中确实存在的短证据片段。语义完整性仍由执行 Skill 的代理负责。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_MAX_CHARS = 1200
MIN_SPLIT_FRACTION = 0.6
INFORMATION_UNIT_DENSITY_CHARS = 450
TIMESTAMP_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?)(?!\d)")
NUMBER_RE = re.compile(
    r"(?<![\d:])(?:19|20)\d{2}年?"
    r"|(?<![\d:])\d+(?:\.\d+)?(?:%|％|万|亿|元|美元|人民币|岁|年|月|日|次|人|家|倍|公里|分钟|小时)(?!\w)"
)
QUESTION_RE = re.compile(r"[^。！？!?\n]{2,}[？?]")
ALLOWED_KINDS = {
    "claim",
    "reasoning",
    "example",
    "anecdote",
    "comparison",
    "number_or_date",
    "person_or_entity",
    "question_answer",
    "prediction",
    "correction",
    "qualification",
    "other",
}
ALLOWED_OMISSION_REASONS = {
    "exact_repetition",
    "filler",
    "transcription_noise",
    "navigation_noise",
}
VAGUE_DETAILS = {
    "本段已覆盖",
    "本段内容已覆盖",
    "相关内容已写入",
    "已纳入正文",
    "见正文",
    "若干内容",
    "若干案例",
    "一些观点",
}
VAGUE_DETAIL_RE = re.compile(
    r"^(?:(?:本|该)?段(?:内容)?(?:已经|已)?(?:覆盖|写入|纳入|处理)(?:完成)?|"
    r"(?:详?见)(?:正文|上文|下文)|"
    r"(?:若干|一些|多个|多位|相关)(?:内容|观点|案例|人物|人士|原因|问题|事情)?)[。.!！]?$"
)


class FidelityError(RuntimeError):
    """覆盖清单无法证明成稿逐段处理了原材料。"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_text(path: Path) -> str:
    if not path.is_file():
        raise FidelityError(f"文件不存在：{path}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as error:
        raise FidelityError(f"文件不是可读的 UTF-8 文本：{path}") from error


def normalize_evidence(text: str) -> str:
    """只折叠空白；不做同义替换，保证证据确实出现在最终稿中。"""

    return re.sub(r"\s+", "", text).strip()


def choose_split(text: str, start: int, max_chars: int) -> int:
    limit = min(len(text), start + max_chars)
    if limit == len(text):
        return limit
    minimum = start + max(1, int(max_chars * MIN_SPLIT_FRACTION))
    candidates = [
        text.rfind("\n\n", minimum, limit),
        text.rfind("\n", minimum, limit),
        text.rfind("。", minimum, limit),
        text.rfind("！", minimum, limit),
        text.rfind("？", minimum, limit),
    ]
    boundary = max(candidates)
    return boundary + (2 if text[boundary : boundary + 2] == "\n\n" else 1) if boundary >= minimum else limit


def split_source(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[tuple[int, int, str]]:
    if max_chars < 400:
        raise FidelityError("--max-chars 不得小于 400。")
    if not text:
        raise FidelityError("来源文本为空。")
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < len(text):
        end = choose_split(text, start, max_chars)
        if end <= start:
            raise FidelityError("来源分块未能前进。")
        chunks.append((start, end, text[start:end]))
        start = end
    return chunks


def unit_locator(chunk: str, start: int, end: int) -> str:
    timestamps = TIMESTAMP_RE.findall(chunk)
    if timestamps:
        return timestamps[0] if len(timestamps) == 1 else f"{timestamps[0]}–{timestamps[-1]}"
    return f"字符 {start + 1}–{end}"


def extract_numeric_anchors(chunk: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    anchors: list[dict[str, str]] = []
    without_timestamps = TIMESTAMP_RE.sub(" ", chunk)
    for match in NUMBER_RE.finditer(without_timestamps):
        value = match.group(0)
        if value in seen:
            continue
        seen.add(value)
        anchors.append(
            {"source_value": value, "status": "pending", "final_evidence": "", "reason": ""}
        )
    return anchors


def extract_question_anchors(chunk: str) -> list[dict[str, str]]:
    anchors: list[dict[str, str]] = []
    for match in QUESTION_RE.finditer(chunk):
        question = match.group(0).strip()
        if len(question) > 180:
            question = question[-180:]
        anchors.append(
            {
                "source_question": question,
                "status": "pending",
                "final_evidence": "",
                "reason": "",
            }
        )
    return anchors


def prepare_report(source: Path, report: Path, *, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    source = source.expanduser().resolve()
    report = report.expanduser().resolve()
    text = read_text(source)
    raw = source.read_bytes()
    units = []
    for index, (start, end, chunk) in enumerate(split_source(text, max_chars), start=1):
        minimum_information_units = max(
            1,
            math.ceil(len(normalize_evidence(chunk)) / INFORMATION_UNIT_DENSITY_CHARS),
        )
        units.append(
            {
                "id": f"U{index:03d}",
                "source_start_char": start,
                "source_end_char": end,
                "source_sha256": sha256_bytes(chunk.encode("utf-8")),
                "source_locator": unit_locator(chunk, start, end),
                "source_text": chunk,
                "minimum_information_units": minimum_information_units,
                "information_units": [],
                "numeric_anchors": extract_numeric_anchors(chunk),
                "question_anchors": extract_question_anchors(chunk),
                "omissions": [],
            }
        )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_iso(),
        "source": {
            "filename": source.name,
            "sha256": sha256_bytes(raw),
            "size_bytes": len(raw),
            "character_count": len(text),
        },
        "instructions": {
            "information_units": "至少达到 minimum_information_units，但这只是机械下限；原文有更多具体主张、推理、例子、故事、比较、人物、限定语、预测或修正时继续逐项填写。source_detail 必须是当前来源块中的连续原文片段，final_evidence 必须是最终 Markdown 中不与其他信息项重复的连续片段。",
            "numeric_anchors": "每项改为 covered、corrected 或 excluded；前两种提供成稿片段，corrected/excluded 说明理由。",
            "question_anchors": "每项改为 covered、partial、unanswered、rhetorical 或 noise；除 noise 外提供成稿片段。",
            "omissions": "只允许 exact_repetition、filler、transcription_noise、navigation_noise，并具体说明删了什么。",
        },
        "units": units,
        "final": None,
        "audit": None,
    }
    atomic_write_json(report, payload)
    return payload


def require_string(value: Any, label: str, *, minimum: int = 1) -> str:
    if not isinstance(value, str) or len(normalize_evidence(value)) < minimum:
        raise FidelityError(f"{label} 缺失或过短。")
    return value


def require_final_evidence(value: Any, label: str, normalized_final: str) -> str:
    evidence = require_string(value, label, minimum=8)
    if normalize_evidence(evidence) not in normalized_final:
        raise FidelityError(f"{label} 不是最终 Markdown 中的连续文字。")
    return evidence


def validate_information_units(
    unit: dict[str, Any],
    normalized_final: str,
    seen_source_details: set[str],
    seen_final_evidence: set[str],
) -> int:
    items = unit.get("information_units")
    if not isinstance(items, list) or not items:
        raise FidelityError(f"{unit['id']} 没有登记任何具体信息单元。")
    minimum = unit.get("minimum_information_units")
    expected_minimum = max(
        1,
        math.ceil(len(normalize_evidence(unit.get("source_text", ""))) / INFORMATION_UNIT_DENSITY_CHARS),
    )
    if minimum != expected_minimum:
        raise FidelityError(f"{unit['id']}.minimum_information_units 与当前来源块不一致。")
    if len(items) < minimum:
        raise FidelityError(
            f"{unit['id']} 只有 {len(items)} 个信息单元，低于机械下限 {minimum}；"
            "这仍不是语义完整性的上限。"
        )
    normalized_source = normalize_evidence(unit["source_text"])
    for index, item in enumerate(items, start=1):
        label = f"{unit['id']}.information_units[{index}]"
        if not isinstance(item, dict):
            raise FidelityError(f"{label} 必须是对象。")
        kind = item.get("kind")
        if kind not in ALLOWED_KINDS:
            raise FidelityError(f"{label}.kind 无效：{kind}")
        detail = require_string(item.get("source_detail"), f"{label}.source_detail", minimum=4)
        normalized_detail = normalize_evidence(detail)
        if normalized_detail in {normalize_evidence(x) for x in VAGUE_DETAILS} or VAGUE_DETAIL_RE.fullmatch(
            normalized_detail
        ):
            raise FidelityError(f"{label}.source_detail 过于笼统，必须写具体内容。")
        if len(normalized_detail) < 8:
            raise FidelityError(f"{label}.source_detail 缺失或过短。")
        if normalized_detail not in normalized_source:
            raise FidelityError(f"{label}.source_detail 不是当前来源块中的连续原文片段。")
        if normalized_detail in seen_source_details:
            raise FidelityError(f"{label}.source_detail 与另一信息单元重复。")
        evidence = require_final_evidence(
            item.get("final_evidence"), f"{label}.final_evidence", normalized_final
        )
        normalized_evidence = normalize_evidence(evidence)
        if normalized_evidence in seen_final_evidence:
            raise FidelityError(f"{label}.final_evidence 与另一信息单元重复，不能重复占位。")
        seen_source_details.add(normalized_detail)
        seen_final_evidence.add(normalized_evidence)
    return len(items)


def validate_numeric_anchors(unit: dict[str, Any], normalized_final: str) -> int:
    anchors = unit.get("numeric_anchors")
    if not isinstance(anchors, list):
        raise FidelityError(f"{unit['id']}.numeric_anchors 必须是列表。")
    for index, item in enumerate(anchors, start=1):
        label = f"{unit['id']}.numeric_anchors[{index}]"
        if not isinstance(item, dict):
            raise FidelityError(f"{label} 必须是对象。")
        source_value = require_string(item.get("source_value"), f"{label}.source_value")
        status = item.get("status")
        if status == "covered":
            evidence = require_final_evidence(item.get("final_evidence"), f"{label}.final_evidence", normalized_final)
            if normalize_evidence(source_value) not in normalize_evidence(evidence):
                raise FidelityError(f"{label} 标为 covered，但成稿片段没有该数字。")
        elif status == "corrected":
            require_final_evidence(item.get("final_evidence"), f"{label}.final_evidence", normalized_final)
            require_string(item.get("reason"), f"{label}.reason", minimum=6)
        elif status == "excluded":
            require_string(item.get("reason"), f"{label}.reason", minimum=6)
        else:
            raise FidelityError(f"{label}.status 仍为 pending 或无效。")
    return len(anchors)


def validate_question_anchors(unit: dict[str, Any], normalized_final: str) -> int:
    anchors = unit.get("question_anchors")
    if not isinstance(anchors, list):
        raise FidelityError(f"{unit['id']}.question_anchors 必须是列表。")
    for index, item in enumerate(anchors, start=1):
        label = f"{unit['id']}.question_anchors[{index}]"
        if not isinstance(item, dict):
            raise FidelityError(f"{label} 必须是对象。")
        require_string(item.get("source_question"), f"{label}.source_question", minimum=3)
        status = item.get("status")
        if status in {"covered", "partial", "unanswered", "rhetorical"}:
            require_final_evidence(item.get("final_evidence"), f"{label}.final_evidence", normalized_final)
        elif status == "noise":
            require_string(item.get("reason"), f"{label}.reason", minimum=6)
        else:
            raise FidelityError(f"{label}.status 仍为 pending 或无效。")
    return len(anchors)


def validate_omissions(unit: dict[str, Any]) -> int:
    omissions = unit.get("omissions")
    if not isinstance(omissions, list):
        raise FidelityError(f"{unit['id']}.omissions 必须是列表。")
    for index, item in enumerate(omissions, start=1):
        label = f"{unit['id']}.omissions[{index}]"
        if not isinstance(item, dict):
            raise FidelityError(f"{label} 必须是对象。")
        require_string(item.get("source_detail"), f"{label}.source_detail", minimum=4)
        if item.get("reason") not in ALLOWED_OMISSION_REASONS:
            raise FidelityError(f"{label}.reason 不是允许的无信息删除理由。")
    return len(omissions)


def audit_report(
    source: Path,
    final: Path,
    report: Path,
    *,
    update_report: bool = True,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    final = final.expanduser().resolve()
    report = report.expanduser().resolve()
    source_text = read_text(source)
    final_text = read_text(final)
    if not report.is_file():
        raise FidelityError(f"覆盖清单不存在：{report}")
    try:
        payload = json.loads(report.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FidelityError(f"覆盖清单不是有效 UTF-8 JSON：{report}") from error
    if isinstance(payload, dict) and payload.get("schema_version") == "fulltext-1":
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from assemble_record import verify
        from project_io import ProjectError
        try:
            return verify(source, final, report, update_report=update_report)
        except (ProjectError, ValueError, KeyError, IndexError) as error:
            raise FidelityError(str(error)) from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise FidelityError("覆盖清单 schema_version 不兼容。")
    source_record = payload.get("source")
    if not isinstance(source_record, dict):
        raise FidelityError("覆盖清单缺少 source。")
    source_raw = source.read_bytes()
    if source_record.get("sha256") != sha256_bytes(source_raw):
        raise FidelityError("来源文件 SHA256 已变化，请重新 prepare 覆盖清单。")
    if source_record.get("character_count") != len(source_text):
        raise FidelityError("来源字符数与覆盖清单不一致。")
    units = payload.get("units")
    if not isinstance(units, list) or not units:
        raise FidelityError("覆盖清单没有来源单元。")
    expected_start = 0
    normalized_final = normalize_evidence(final_text)
    seen_source_details: set[str] = set()
    seen_final_evidence: set[str] = set()
    information_count = numeric_count = question_count = omission_count = 0
    for expected_index, unit in enumerate(units, start=1):
        if not isinstance(unit, dict) or unit.get("id") != f"U{expected_index:03d}":
            raise FidelityError(f"第 {expected_index} 个来源单元编号无效。")
        start = unit.get("source_start_char")
        end = unit.get("source_end_char")
        if not isinstance(start, int) or not isinstance(end, int) or start != expected_start or end <= start:
            raise FidelityError(f"{unit.get('id')} 与上一单元不连续。")
        if end > len(source_text):
            raise FidelityError(f"{unit['id']} 越出来源文本。")
        chunk = source_text[start:end]
        if unit.get("source_text") != chunk:
            raise FidelityError(f"{unit['id']} 的 source_text 已被修改。")
        if unit.get("source_sha256") != sha256_bytes(chunk.encode("utf-8")):
            raise FidelityError(f"{unit['id']} 的来源哈希不一致。")
        information_count += validate_information_units(
            unit,
            normalized_final,
            seen_source_details,
            seen_final_evidence,
        )
        numeric_count += validate_numeric_anchors(unit, normalized_final)
        question_count += validate_question_anchors(unit, normalized_final)
        omission_count += validate_omissions(unit)
        expected_start = end
    if expected_start != len(source_text):
        raise FidelityError("覆盖清单没有连续覆盖到来源文本末尾。")
    summary = {
        "status": "passed",
        "audited_at": now_iso(),
        "source_sha256": sha256_file(source),
        "final_sha256": sha256_file(final),
        "report_sha256_before_audit": sha256_file(report),
        "unit_count": len(units),
        "information_unit_count": information_count,
        "numeric_anchor_count": numeric_count,
        "question_anchor_count": question_count,
        "omission_count": omission_count,
        "mechanical_gate_only": True,
    }
    if update_report:
        payload["final"] = {
            "filename": final.name,
            "sha256": summary["final_sha256"],
            "size_bytes": final.stat().st_size,
        }
        payload["audit"] = summary
        atomic_write_json(report, payload)
        summary["report_sha256"] = sha256_file(report)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="建立并校验最终 Markdown 的原材料覆盖清单。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="把校正稿／正文连续分块为待填写覆盖清单")
    prepare.add_argument("source", type=Path)
    prepare.add_argument("report", type=Path)
    prepare.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    prepare.add_argument("--json", action="store_true")
    audit = subparsers.add_parser("audit", help="核对来源、逐项登记和最终 Markdown 落点")
    audit.add_argument("source", type=Path)
    audit.add_argument("final", type=Path)
    audit.add_argument("report", type=Path)
    audit.add_argument("--delete-after-success", action="store_true")
    audit.add_argument("--json", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            payload = prepare_report(args.source, args.report, max_chars=args.max_chars)
            result = {"status": "prepared", "report": str(args.report.resolve()), "unit_count": len(payload["units"])}
        else:
            result = audit_report(args.source, args.final, args.report)
            if args.delete_after_success:
                args.report.expanduser().resolve().unlink()
                result["report_deleted"] = True
    except FidelityError as error:
        parser.exit(2, f"Error: {error}\n")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
