"""资料媒体第一阶段的分段转写、交接与续跑管理器。

本脚本解决长视频／音频在单次 Codex 额度内难以完成的问题，也提供项目材料
的只读清点和外部机器逐字稿导入。完整转写会被拆成可独立提交的子阶段，并以
``00_阶段交接/state.json`` 作为机器转写路线的唯一事实源。
中文交接说明、分段清单和最终逐字稿都由该状态生成或校验，绝不靠解析自然
语言说明来判断完成度。

设计上的关键安全约束：

* 短媒体默认连续；长媒体或 staged 按本次选择或已有明确授权初始化；
  多种材料并存且路线未明时先列出选项；
* 外部逐字稿只由显式 ``import-transcript`` 导入，原文件永不覆盖；
* 分阶段模式每完成一段就进入等待状态，下一段必须显式 ``approve-next``；
* 音频、分段稿和最终四格式均先写同目录临时文件，再原子替换；
* 已完成段按来源指纹和输出 SHA256 复用，失败只重做当前段；
* 新工作流在正式总逐字稿提交后清理分段中间件，但始终保留阶段交接；
* 第二阶段只有在最终 Markdown 及其引用附件已归档并验证后才可完成；
* 状态中的持久化路径都相对于“Markdown资料库”，资料库整体移动后可续跑。

运行本脚本应使用资料库工具包的固定 Python 环境，因为该环境已包含 PyAV、
imageio-ffmpeg、CTranslate2 和 faster-whisper。问答由 Skill 负责，本脚本不在
终端内向用户交互。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import uuid
import wave
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))


# VERSION 是发布版本唯一事实源。状态结构兼容由 SCHEMA_VERSION 单独控制，
# 因此升级文档或功能版本不会被误解为必须重写旧 state.json。
WORKFLOW_VERSION = (
    Path(__file__).resolve().parents[1] / "VERSION"
).read_text(encoding="utf-8-sig").strip()
SCHEMA_VERSION = 1
ROOT_NAME = "Markdown资料库"
DATABASE_MARKER = ".markdown-library-database.json"
TOOLKIT_RELATIVE = Path("工具") / "资料整理工具"
HANDOFF_DIRNAME = "00_阶段交接"
PARTS_DIRNAME = "01_转写分段"
STATE_FILENAME = "state.json"
PROGRESS_FILENAME = "总进度.md"
MANIFEST_FILENAME = "manifest.json"
FINAL_BUNDLE_MANIFEST = "01_原始逐字稿.manifest.json"
COVERAGE_REPORT_FILENAME = "内容覆盖清单.json"
# 只有“完整转写”使用这个长任务阈值。严格大于 90 分钟才算长媒体；
# 恰好 01:30:00.000 仍按普通媒体处理。
LONG_MEDIA_THRESHOLD_MS = 90 * 60 * 1000
DEFAULT_TARGET_MINUTES = 60
DEFAULT_OVERLAP_SECONDS = 10
DEFAULT_CONTEXT_SEGMENTS = 8
DEFAULT_CONTEXT_CHARS = 240

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v"}
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus"}
TRANSCRIPT_EXTENSIONS = {".txt", ".md", ".srt", ".vtt", ".json", ".jsonl"}

# 这些文件是本工作流自己的成果或状态，不应在下一次预检时再次被误报为
# “外部逐字稿”。其他 TXT/MD 仍作为候选列出，并交由用户确认其真实用途。
GENERATED_TRANSCRIPT_NAMES = {
    "01_原始逐字稿.md",
    "01_原始逐字稿.srt",
    "01_原始逐字稿.json",
    "01_原始逐字稿.jsonl",
    FINAL_BUNDLE_MANIFEST,
    "02_校正逐字稿.md",
}


class WorkflowError(RuntimeError):
    """表示可向使用者解释的工作流错误，而不是代码崩溃。"""


class Stage1CompactionError(WorkflowError):
    """表示正式总稿已提交，但第一阶段中间件尚未完全收口。"""


def now_iso() -> str:
    """返回带本地时区的秒级 ISO 时间，便于跨会话追踪。"""

    return datetime.now().astimezone().isoformat(timespec="seconds")


def stamp_ms(milliseconds: int, *, srt: bool = False) -> str:
    """把整数毫秒渲染为 Markdown 或 SRT 时间戳。"""

    milliseconds = max(0, int(milliseconds))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    separator = "," if srt else "."
    if srt:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """在目标同目录写临时文件，刷盘后用 ``os.replace`` 原子提交。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str, *, bom: bool = False) -> None:
    """原子写入 UTF-8 文本；逐字稿可按既有约定选择 UTF-8 BOM。"""

    encoding = "utf-8-sig" if bom else "utf-8"
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path, payload: Any) -> None:
    """以稳定缩进原子写入 JSON，确保中断后旧状态仍可读取。"""

    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, text)


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA256，避免大媒体占用额外内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def path_within(path: Path, parent: Path) -> bool:
    """按解析后的路径判断 ``path`` 是否位于 ``parent`` 内。"""

    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def relative_to_database(path: Path, database_root: Path) -> str:
    """将数据库内路径规范化为可移动的 POSIX 风格相对路径。"""

    resolved = path.resolve()
    if not path_within(resolved, database_root):
        raise WorkflowError(f"路径越出资料库根目录，拒绝写入状态：{resolved}")
    return resolved.relative_to(database_root.resolve()).as_posix()


def database_path(relative: str, database_root: Path) -> Path:
    """安全解析状态中的数据库相对路径，并拒绝 ``..`` 越界。"""

    candidate = (database_root / Path(relative)).resolve()
    if not path_within(candidate, database_root):
        raise WorkflowError(f"状态中的路径越界：{relative}")
    return candidate


def root_from(path: Path) -> Path | None:
    """从一个文件或目录向上寻找资料库标记。"""

    candidate = path.expanduser()
    if candidate.is_file():
        candidate = candidate.parent
    for parent in (candidate, *candidate.parents):
        if parent.name == ROOT_NAME or (parent / DATABASE_MARKER).is_file():
            return parent.resolve()
    return None


def validate_database_root(path: Path) -> Path:
    """确认根目录带标记或完整工具包配置。"""

    root = path.expanduser().resolve()
    config = root / TOOLKIT_RELATIVE / "config.json"
    if not root.is_dir() or not (
        root.name == ROOT_NAME or (root / DATABASE_MARKER).is_file() or config.is_file()
    ):
        raise WorkflowError(f"不是有效的“{ROOT_NAME}”根目录：{root}")
    if not config.is_file():
        raise WorkflowError(f"资料库缺少工具包配置：{config}")
    return root


def resolve_input(value: Path, database_root: Path | None) -> tuple[Path, Path]:
    """解析绝对路径或从资料库名开始的可移动相对路径。"""

    raw = value.expanduser()
    if raw.is_absolute():
        resolved = raw.resolve()
        root = validate_database_root(database_root) if database_root else root_from(resolved)
        if root is None:
            raise WorkflowError("无法从输入路径找到“Markdown资料库”；请添加 --database-root。")
        root = validate_database_root(root)
    else:
        if database_root is None:
            root = root_from(Path.cwd())
            if root is None:
                raise WorkflowError("相对路径需要 --database-root，或从资料库内部运行。")
        else:
            root = validate_database_root(database_root)
        parts = raw.parts
        if parts and parts[0] == ROOT_NAME:
            raw = Path(*parts[1:]) if len(parts) > 1 else Path(".")
        resolved = (root / raw).resolve()
    if not path_within(resolved, root):
        raise WorkflowError(f"输入路径越出资料库：{resolved}")
    return resolved, root


def choose_media(path: Path) -> Path:
    """从文件或单媒体资料夹中选择主媒体，排除已提取的 ``_音频``。"""

    supported = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
    if path.is_file():
        if path.suffix.lower() not in supported:
            raise WorkflowError(f"不支持的媒体格式：{path.suffix}")
        return path.resolve()
    if not path.is_dir():
        raise WorkflowError(f"找不到输入路径：{path}")
    all_candidates = sorted(
        (
            item
            for item in path.iterdir()
            if item.is_file() and item.suffix.lower() in supported
        ),
        key=lambda item: item.name.lower(),
    )
    candidates = [item for item in all_candidates if not item.stem.endswith("_音频")]
    if not candidates and len(all_candidates) == 1:
        candidates = all_candidates
    if len(candidates) != 1:
        names = "、".join(item.name for item in all_candidates) or "无"
        raise WorkflowError(f"资料夹内应恰好有一个主媒体；当前媒体文件：{names}")
    return candidates[0].resolve()


def _read_text_sample(path: Path, limit: int = 256 * 1024) -> str:
    """读取小段 UTF-8 文本用于只读分类；乱码时返回空字符串。

    材料清点不应为了判断一个大 JSON 是否像逐字稿而把它全部读入内存，因此
    这里只读取有限字节。该函数不修改文件，也不把抽样内容写入清单。
    """

    try:
        return path.read_bytes()[:limit].decode("utf-8-sig", errors="strict")
    except (OSError, UnicodeError):
        return ""


def _json_looks_like_transcript(path: Path, sample: str) -> bool:
    """保守判断 JSON/JSONL 是否具有逐字稿结构。

    文件名含“逐字稿／字幕”等线索时由调用方直接认定；这里补充识别常见的
    ``segments`` 数组和逐行 ``text`` 记录，避免把普通配置 JSON 列入清单。
    """

    if not sample.strip():
        return False
    if path.suffix.lower() == ".jsonl":
        for line in sample.splitlines()[:20]:
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return True
        return False
    # 大 JSON 的抽样可能不是完整文档；先检查典型字段，再在小文件上精确解析。
    if re.search(r'"segments"\s*:', sample) and re.search(r'"text"\s*:', sample):
        return True
    if path.stat().st_size > 2 * 1024 * 1024:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    segments = payload.get("segments") if isinstance(payload, dict) else payload
    return bool(
        isinstance(segments, list)
        and segments
        and isinstance(segments[0], dict)
        and isinstance(segments[0].get("text"), str)
    )


def _transcript_candidate(path: Path) -> tuple[bool, str, bool]:
    """返回“是否候选、置信度、是否检测到时间戳”。

    TXT/Markdown 只能被称作候选而非已确认逐字稿；SRT/VTT 结构明确，置信度
    较高。最终仍由用户选择，预检不会据此自动启动任何处理路线。
    """

    suffix = path.suffix.lower()
    if suffix not in TRANSCRIPT_EXTENSIONS or path.name in GENERATED_TRANSCRIPT_NAMES:
        return False, "none", False
    clue = any(
        token in path.stem.lower()
        for token in ("逐字", "字幕", "转写", "transcript", "subtitle", "caption")
    )
    sample = _read_text_sample(path)
    has_timestamps = bool(
        re.search(
            r"(?:\[|\b)(?:\d{1,2}:)?\d{2}:\d{2}(?:[.,]\d{3})?(?:\]|\b)|\s-->\s",
            sample,
        )
    )
    if suffix in {".json", ".jsonl"} and not clue and not _json_looks_like_transcript(path, sample):
        return False, "none", False
    confidence = "high" if suffix in {".srt", ".vtt"} or clue else "possible"
    return True, confidence, has_timestamps or suffix in {".srt", ".vtt"}


def _material_record(path: Path, database_root: Path, kind: str, **extra: Any) -> dict[str, Any]:
    """生成不含文件哈希的只读清单项，避免预检对大媒体做昂贵扫描。"""

    stat = path.stat()
    return {
        "path": relative_to_database(path, database_root),
        "name": path.name,
        "kind": kind,
        "format": path.suffix.lower().lstrip("."),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        **extra,
    }


def material_choice_options(
    *, video_count: int, audio_count: int, transcript_count: int
) -> dict[str, Any]:
    """为多资料项目生成用户可选路线，不设置任何默认选择。

    返回值可直接嵌入 ``inspect --json``。``recommended_option_id`` 只是成本提示，
    ``selected_option_id`` 始终为空；调用方必须取得用户明确选择后才能写入。
    """

    media_count = video_count + audio_count
    total_count = media_count + transcript_count
    if total_count <= 1:
        return {
            "required": False,
            "reason_codes": [],
            "selected_option_id": None,
            "recommended_option_id": None,
            "options": [],
        }

    reasons: list[str] = []
    if transcript_count > 1:
        reasons.append("multiple_transcript_candidates")
    if media_count > 1:
        reasons.append("multiple_media_candidates")
    if transcript_count and media_count:
        reasons.append("media_and_transcript_coexist")

    if transcript_count and media_count:
        options = [
            {
                "id": "reuse_transcript_selective_verification",
                "label": "复用已有逐字稿并选择性听校",
                "cost": "low",
                "description": "以用户指定逐字稿为主，只用音频或视频核对高风险片段。",
            },
            {
                "id": "transcript_only",
                "label": "仅处理已有逐字稿",
                "cost": "lowest",
                "description": "不听校、不重新转写；成果必须注明未经原音频核验。",
            },
            {
                "id": "retranscribe_and_compare",
                "label": "重新转写并对照",
                "cost": "high",
                "description": "从用户指定媒体完整转写，再与指定外部逐字稿比较。",
            },
            {
                "id": "process_separately_then_compare",
                "label": "分别处理后比较",
                "cost": "highest",
                "description": "各版本独立保留和整理，不自动合并。",
            },
            {
                "id": "custom",
                "label": "用户自定义",
                "cost": "variable",
                "description": "由用户指定主文件、参考文件、听校和比较范围。",
            },
        ]
        recommended = "reuse_transcript_selective_verification"
    elif video_count and audio_count:
        options = [
            {
                "id": "use_existing_audio",
                "label": "使用已有音频",
                "cost": "low",
                "description": "使用用户指定音频转写，不再从视频重复提取音频。",
            },
            {
                "id": "extract_audio_from_video",
                "label": "从视频提取音频",
                "cost": "medium",
                "description": "忽略独立音频，从用户指定视频重新提取。",
            },
            {
                "id": "process_separately_then_compare",
                "label": "分别处理后比较",
                "cost": "high",
                "description": "两种媒体分别转写并比较，不自动合并。",
            },
            {
                "id": "custom",
                "label": "用户自定义",
                "cost": "variable",
                "description": "由用户指定主媒体和参考媒体。",
            },
        ]
        recommended = "use_existing_audio"
    elif transcript_count > 1:
        options = [
            {
                "id": "select_one_transcript",
                "label": "指定一个逐字稿为主",
                "cost": "lowest",
                "description": "用户显式选择主稿，其他版本只作参考。",
            },
            {
                "id": "process_separately_then_compare",
                "label": "分别处理后比较",
                "cost": "medium",
                "description": "各稿独立保留并生成差异说明，不自动合并。",
            },
            {
                "id": "custom",
                "label": "用户自定义",
                "cost": "variable",
                "description": "由用户明确各逐字稿的用途。",
            },
        ]
        recommended = "select_one_transcript"
    else:
        options = [
            {
                "id": "select_one_media",
                "label": "指定一个主媒体",
                "cost": "low",
                "description": "用户明确选择一个媒体进入完整转写。",
            },
            {
                "id": "process_separately_then_compare",
                "label": "分别处理后比较",
                "cost": "high",
                "description": "各媒体分别处理，不自动合并结果。",
            },
            {
                "id": "custom",
                "label": "用户自定义",
                "cost": "variable",
                "description": "由用户指定每个媒体的用途。",
            },
        ]
        recommended = "select_one_media"
    return {
        "required": True,
        "reason_codes": reasons,
        "selected_option_id": None,
        "recommended_option_id": recommended,
        "automatic_selection_allowed": False,
        "automatic_transcript_merge_allowed": False,
        "options": options,
    }


def discover_project_materials(path: Path, database_root: Path) -> dict[str, Any]:
    """只读列出同一任务目录中的视频、音频和外部逐字稿候选。

    扫描范围刻意限制在输入文件所在目录（或输入目录本身）的第一层，避免把
    整个资料库中的无关材料混入当前项目。预检不计算大媒体哈希，也不写状态。
    """

    resolved = path.expanduser().resolve()
    if not path_within(resolved, database_root):
        raise WorkflowError(f"材料路径越出资料库：{resolved}")
    if not resolved.exists():
        raise WorkflowError(f"找不到输入路径：{resolved}")
    project_dir = resolved if resolved.is_dir() else resolved.parent
    videos: list[dict[str, Any]] = []
    audios: list[dict[str, Any]] = []
    transcripts: list[dict[str, Any]] = []
    for item in sorted(project_dir.iterdir(), key=lambda candidate: candidate.name.lower()):
        if not item.is_file():
            continue
        suffix = item.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            videos.append(_material_record(item, database_root, "video"))
        elif suffix in AUDIO_EXTENSIONS:
            # ``*_音频`` 是旧工作流从同目录视频提取出的约定名称。它仍会出现在
            # 清单中，但当原视频也在时不算第二个独立来源，避免旧任务续跑时被
            # 错误拦在“多资料选择”关卡。
            derived_from_video = item.stem.endswith("_音频")
            audios.append(
                _material_record(
                    item,
                    database_root,
                    "audio",
                    origin_hint="workflow_extracted_audio" if derived_from_video else "independent",
                    independent_source=not derived_from_video,
                )
            )
        elif suffix in TRANSCRIPT_EXTENSIONS:
            candidate, confidence, has_timestamps = _transcript_candidate(item)
            if candidate:
                transcripts.append(
                    _material_record(
                        item,
                        database_root,
                        "transcript_candidate",
                        candidate_confidence=confidence,
                        has_detected_timestamps=has_timestamps,
                    )
                )
    decision_audio_count = (
        sum(bool(item["independent_source"]) for item in audios) if videos else len(audios)
    )
    choice = material_choice_options(
        video_count=len(videos),
        audio_count=decision_audio_count,
        transcript_count=len(transcripts),
    )
    return {
        "project_directory": relative_to_database(project_dir, database_root),
        "scan_scope": "project_directory_top_level_only",
        "read_only": True,
        "materials": {"videos": videos, "audios": audios, "transcripts": transcripts},
        "counts": {
            "videos": len(videos),
            "audios": len(audios),
            "transcripts": len(transcripts),
        },
        "decision_counts": {
            "videos": len(videos),
            "independent_audios": decision_audio_count,
            "transcripts": len(transcripts),
        },
        "user_choice": choice,
    }


def probe_duration_ms(media: Path) -> int:
    """读取可转写时长；无压缩 WAV 可用标准库，其他媒体坚持使用 PyAV。

    快速测试和部分录音设备会产生标准 PCM WAV。它的帧数、采样率已足以可靠
    计算时长，因此在没有 PyAV 的轻量环境中也能检查状态机。视频、M4A、MP3
    等容器仍必须由固定转写环境的 PyAV 探测，不能用文件大小或码率猜时长。
    """

    if media.suffix.lower() in {".wav", ".wave"}:
        try:
            with wave.open(str(media), "rb") as handle:
                frame_rate = handle.getframerate()
                frame_count = handle.getnframes()
            if frame_rate <= 0:
                raise WorkflowError(f"WAV 采样率无效，无法探测时长：{media}")
            duration = round(frame_count / frame_rate * 1000)
            if duration <= 0:
                raise WorkflowError(f"WAV 时长无效：{media}")
            return duration
        except (wave.Error, EOFError):
            # 非标准或压缩 WAV 继续交给 PyAV；不要把扩展名当成格式事实。
            pass

    try:
        import av  # 固定转写环境提供；延迟导入便于纯逻辑单元测试。
    except ImportError as error:
        raise WorkflowError("固定转写环境缺少 PyAV，无法可靠探测媒体时长。") from error

    try:
        with av.open(str(media)) as container:
            audio_durations: list[int] = []
            other_durations: list[int] = []
            for stream in container.streams:
                if stream.type not in {"audio", "video"}:
                    continue
                if stream.duration is not None and stream.time_base is not None:
                    duration = round(float(stream.duration * stream.time_base) * 1000)
                    (audio_durations if stream.type == "audio" else other_durations).append(duration)
            if audio_durations:
                duration_ms = max(audio_durations)
            elif other_durations:
                duration_ms = max(other_durations)
            elif container.duration is not None:
                duration_ms = round(container.duration / 1000)
            else:
                raise WorkflowError("媒体没有可读的时长元数据。")
    except WorkflowError:
        raise
    except Exception as error:
        raise WorkflowError(f"无法读取媒体时长：{media.name}（{error}）") from error
    if duration_ms <= 0:
        raise WorkflowError(f"媒体时长无效：{duration_ms} ms")
    return duration_ms


def suggest_chunk_count(duration_ms: int, target_minutes: int = DEFAULT_TARGET_MINUTES) -> int:
    """按约一小时一段给出建议数，并确保长媒体至少两段。"""

    if target_minutes <= 0:
        raise ValueError("target_minutes 必须大于 0")
    count = max(1, math.ceil(duration_ms / (target_minutes * 60_000)))
    if duration_ms > LONG_MEDIA_THRESHOLD_MS:
        count = max(2, count)
    return count


def build_chunk_plan(duration_ms: int, chunk_count: int, overlap_ms: int) -> list[dict[str, int | str]]:
    """等分核心责任区间，并在实际识别区间两端加入重叠。

    核心区间无缝覆盖 ``[0, duration)``；重叠只用于避免切点漏字。合并时按
    语句中点归属核心区间，因此同一句不会仅因重叠而重复进入最终稿。
    """

    if duration_ms <= 0 or chunk_count <= 0 or overlap_ms < 0:
        raise ValueError("时长和分段数必须为正，重叠不得为负。")
    if chunk_count > duration_ms:
        raise ValueError("分段数不能大于总毫秒数。")
    boundaries = [round(index * duration_ms / chunk_count) for index in range(chunk_count + 1)]
    plan: list[dict[str, int | str]] = []
    for offset in range(chunk_count):
        core_start = boundaries[offset]
        core_end = boundaries[offset + 1]
        plan.append(
            {
                "index": offset + 1,
                "core_start_ms": core_start,
                "core_end_ms": core_end,
                "decode_start_ms": max(0, core_start - overlap_ms),
                "decode_end_ms": min(duration_ms, core_end + overlap_ms),
                "status": "pending",
            }
        )
    return plan


def inspect_existing_workflow(
    media: Path, database_root: Path, state_path: Path
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """只读验证旧状态，区分“可续跑”与“存在异常状态文件”。

    ``inspect`` 是用户确认模式前唯一允许执行的命令，因此这里不能修复或覆盖
    任何文件。状态文件即使存在，也只有在模式版本、来源路径、来源身份和关键
    阶段结构都有效时才可称为 ``existing_workflow``。否则返回结构化异常，提醒
    对话层停下报告，避免把别的媒体或损坏 JSON 当成有效断点。
    """

    if not state_path.is_file():
        return None, None
    issue_prefix = {
        "kind": "invalid_existing_workflow",
        "state_path": relative_to_database(state_path, database_root),
    }
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        if not isinstance(state, dict):
            raise WorkflowError("状态顶层不是 JSON 对象。")
        if state.get("schema_version") != SCHEMA_VERSION:
            raise WorkflowError(
                f"状态模式版本为 {state.get('schema_version')}，当前仅支持 {SCHEMA_VERSION}。"
            )
        if not isinstance(state.get("workflow_id"), str) or not state["workflow_id"].strip():
            raise WorkflowError("状态缺少有效的 workflow_id。")
        source = state.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("identity"), dict):
            raise WorkflowError("状态缺少完整的 source.identity。")
        execution = state.get("execution")
        if not isinstance(execution, dict) or execution.get("mode") not in {"continuous", "staged"}:
            raise WorkflowError("状态缺少有效的 execution.mode。")
        expected_source = relative_to_database(media, database_root)
        if source.get("path") != expected_source:
            raise WorkflowError(
                f"状态来源为 {source.get('path')!r}，当前媒体为 {expected_source!r}。"
            )
        if not isinstance(state.get("stage1"), dict) or not isinstance(state.get("stage2"), dict):
            raise WorkflowError("状态缺少 stage1 或 stage2。")

        identity = source["identity"]
        if (
            not isinstance(identity.get("size_bytes"), int)
            or not isinstance(identity.get("mtime_ns"), int)
            or not isinstance(identity.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", identity["sha256"]) is None
        ):
            raise WorkflowError("状态中的来源大小、mtime 或 SHA256 无效。")
        allowed_stage1 = {
            "ready",
            "chunk_running",
            "failed_recoverable",
            "awaiting_continue",
            "ready_to_merge",
            "complete",
        }
        allowed_stage2 = {"pending", "running", "complete"}
        chunks = state["stage1"].get("chunks")
        stage1_status = state["stage1"].get("status")
        stage2_status = state["stage2"].get("status")
        if stage1_status not in allowed_stage1 or not isinstance(chunks, list) or not chunks:
            raise WorkflowError("状态中的 stage1.status 或 chunks 无效。")
        if stage2_status not in allowed_stage2:
            raise WorkflowError("状态中的 stage2.status 无效。")
        duration_ms = source.get("duration_ms")
        chunk_count = execution.get("chunk_count")
        if not isinstance(duration_ms, int) or duration_ms <= 0:
            raise WorkflowError("状态中的 source.duration_ms 无效。")
        if not isinstance(chunk_count, int) or chunk_count <= 0 or chunk_count != len(chunks):
            raise WorkflowError("状态中的 execution.chunk_count 与子阶段数量不一致。")
        required_ranges = (
            "core_start_ms",
            "core_end_ms",
            "decode_start_ms",
            "decode_end_ms",
        )
        if any(
            not isinstance(chunk, dict)
            or not isinstance(chunk.get("index"), int)
            or chunk.get("status") not in {"pending", "running", "complete", "failed_recoverable"}
            or any(not isinstance(chunk.get(key), int) for key in required_ranges)
            for chunk in chunks
        ):
            raise WorkflowError("状态中存在无效的子阶段记录。")
        if [chunk["index"] for chunk in chunks] != list(range(1, len(chunks) + 1)):
            raise WorkflowError("子阶段编号不是从 1 开始的连续序列。")
        expected_core_start = 0
        for chunk in chunks:
            if (
                chunk["core_start_ms"] != expected_core_start
                or chunk["core_end_ms"] <= chunk["core_start_ms"]
                or not 0 <= chunk["decode_start_ms"] <= chunk["core_start_ms"]
                or not chunk["core_end_ms"] <= chunk["decode_end_ms"] <= duration_ms
            ):
                raise WorkflowError(f"子阶段 {chunk['index']} 的核心或识别范围无效。")
            expected_core_start = chunk["core_end_ms"]
        if expected_core_start != duration_ms:
            raise WorkflowError("子阶段核心范围没有完整覆盖媒体时长。")
        if execution.get("plan_hash") != plan_hash(chunks):
            raise WorkflowError("状态中的分段计划哈希不一致。")
        merge = state["stage1"].get("merge")
        if not isinstance(merge, dict) or merge.get("status") not in {
            "pending",
            "failed_recoverable",
            "complete",
        }:
            raise WorkflowError("状态中的 stage1.merge 无效。")
        all_chunks_complete = all(chunk["status"] == "complete" for chunk in chunks)
        if stage1_status in {"ready_to_merge", "complete"} and not all_chunks_complete:
            raise WorkflowError(f"stage1={stage1_status}，但仍有未完成子阶段。")
        if stage1_status == "complete" and merge.get("status") != "complete":
            raise WorkflowError("第一阶段已标记 complete，但正式合并尚未完成。")
        if stage2_status != "pending" and stage1_status != "complete":
            raise WorkflowError("第二阶段已开始，但第一阶段尚未完成。")
        context_config = execution.get("previous_chunk_context")
        if context_config is not None:
            if not isinstance(context_config, dict) or not isinstance(context_config.get("enabled"), bool):
                raise WorkflowError("状态中的 previous_chunk_context 无效。")
            previous_context_policy(state)
        cleanup = state["stage1"].get("intermediate_cleanup")
        if cleanup is not None and (
            not isinstance(cleanup, dict)
            or cleanup.get("status")
            not in {
                "pending",
                "running",
                "blocked_unknown_files",
                "failed_recoverable",
                "complete",
                "deferred",
            }
        ):
            raise WorkflowError("状态中的 stage1.intermediate_cleanup 无效。")
        gate = state["stage1"].get("gate")
        if stage1_status == "awaiting_continue":
            pending = [chunk["index"] for chunk in chunks if chunk["status"] == "pending"]
            if (
                not isinstance(gate, dict)
                or gate.get("kind") != "continue_next_chunk"
                or not pending
                or gate.get("target_chunk") != pending[0]
            ):
                raise WorkflowError("等待下一段的用户关卡与实际待办子阶段不一致。")
        elif (
            stage1_status == "complete"
            and stage2_status == "pending"
            and execution["mode"] == "staged"
        ):
            cleanup_pending = (
                stage1_compaction_required(state)
                and stage1_compaction_status(state) != "complete"
            )
            if cleanup_pending:
                if gate is not None:
                    raise WorkflowError("第一阶段中间件尚未收口，不应提前开放第二阶段关卡。")
            elif not isinstance(gate, dict) or gate.get("kind") != "enter_stage2":
                raise WorkflowError("分阶段模式缺少进入第二阶段的用户关卡。")
        elif gate is not None:
            raise WorkflowError("当前阶段不应带有用户关卡。")
        validate_owned_temporary_paths(state, media, database_root)
        stat = media.stat()
        same_fast_identity = (
            identity.get("size_bytes") == stat.st_size
            and identity.get("mtime_ns") == stat.st_mtime_ns
        )
        if not same_fast_identity:
            recorded_hash = identity.get("sha256")
            if not isinstance(recorded_hash, str) or sha256_file(media) != recorded_hash:
                raise WorkflowError("当前媒体内容与状态中记录的来源 SHA256 不同。")

        existing = {
            "workflow_id": state.get("workflow_id"),
            "stage1_status": state["stage1"].get("status"),
            "stage2_status": state["stage2"].get("status"),
            "next_action": next_action(state),
        }
        return existing, None
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        KeyError,
        AttributeError,
        ValueError,
        WorkflowError,
    ) as error:
        return None, {**issue_prefix, "message": str(error)}


def inspect_payload(media: Path, database_root: Path, target_minutes: int, overlap_seconds: int) -> dict[str, Any]:
    """生成单个明确媒体的只读时长报告、材料清单和建议分段方案。

    为兼容 2.1.x 调用方，JSON 暂时保留 ``is_over_two_hours`` 键；它现在是
    “是否超过当前长媒体阈值”的废弃别名，不再表达字面上的两小时。
    新调用方应读取 ``is_over_long_media_threshold``。
    """

    duration_ms = probe_duration_ms(media)
    count = suggest_chunk_count(duration_ms, target_minutes)
    plan = build_chunk_plan(duration_ms, count, overlap_seconds * 1000)
    state_path = media.parent / HANDOFF_DIRNAME / STATE_FILENAME
    existing, existing_issue = inspect_existing_workflow(media, database_root, state_path)
    is_long = duration_ms > LONG_MEDIA_THRESHOLD_MS
    inventory = discover_project_materials(media, database_root)
    return {
        "workflow_version": WORKFLOW_VERSION,
        "source": relative_to_database(media, database_root),
        "kind": "audio" if media.suffix.lower() in AUDIO_EXTENSIONS else "video",
        "duration_ms": duration_ms,
        "duration": stamp_ms(duration_ms),
        "long_media_threshold_ms": LONG_MEDIA_THRESHOLD_MS,
        "long_media_threshold": "01:30:00.000",
        "is_over_long_media_threshold": is_long,
        "is_over_ninety_minutes": is_long,
        "is_over_two_hours": is_long,
        "deprecated_fields": {
            "is_over_two_hours": "兼容 2.1.x 的别名；请改用 is_over_long_media_threshold。"
        },
        "suggested_chunk_count": count if is_long else None,
        "suggested_ranges": plan if is_long else [],
        "material_inventory": inventory,
        "requires_user_material_choice": inventory["user_choice"]["required"],
        "existing_workflow": existing,
        "existing_workflow_issue": existing_issue,
    }


def inspect_project_payload(
    path: Path, database_root: Path, target_minutes: int, overlap_seconds: int
) -> dict[str, Any]:
    """预检文件或项目目录；无法唯一确定媒体时只返回材料清单。

    该入口使 ``inspect`` 可以安全接受“视频＋音频＋多个逐字稿”的目录。存在
    多个媒体时不会按文件名或排序擅自选主文件，用户应在对话中选择后再把明确
    文件路径传给 ``init`` 或 ``import-transcript``。
    """

    inventory = discover_project_materials(path, database_root)
    resolved = path.resolve()
    explicit_media = (
        resolved
        if resolved.is_file() and resolved.suffix.lower() in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
        else None
    )
    media_records = inventory["materials"]["videos"] + inventory["materials"]["audios"]
    selected_media: Path | None = explicit_media
    if selected_media is None and len(media_records) == 1:
        selected_media = database_path(media_records[0]["path"], database_root)
    if selected_media is not None:
        payload = inspect_payload(selected_media, database_root, target_minutes, overlap_seconds)
        # 使用本次项目路径的清单（目前与媒体父目录通常相同），避免未来扩展时
        # 因调用两次发现逻辑而返回不一致选择。
        payload["material_inventory"] = inventory
        payload["requires_user_material_choice"] = inventory["user_choice"]["required"]
        return payload
    return {
        "workflow_version": WORKFLOW_VERSION,
        "source": None,
        "kind": None,
        "duration_ms": None,
        "duration": None,
        "long_media_threshold_ms": LONG_MEDIA_THRESHOLD_MS,
        "long_media_threshold": "01:30:00.000",
        "is_over_long_media_threshold": None,
        "is_over_ninety_minutes": None,
        "is_over_two_hours": None,
        "suggested_chunk_count": None,
        "suggested_ranges": [],
        "material_inventory": inventory,
        "requires_user_material_choice": inventory["user_choice"]["required"],
        "existing_workflow": None,
        "existing_workflow_issue": None,
        "notice": "没有唯一媒体可供时长探测；请先由用户明确选择处理材料。",
    }


def load_config(database_root: Path) -> dict[str, Any]:
    """读取资料库工具包配置。"""

    path = database_root / TOOLKIT_RELATIVE / "config.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def config_path(config: dict[str, Any], key: str, database_root: Path) -> Path:
    """把配置中的可移动相对路径解析为绝对路径。"""

    value = Path(config[key]).expanduser()
    candidate = value.resolve() if value.is_absolute() else (database_root / value).resolve()
    if not path_within(candidate, database_root):
        raise WorkflowError(f"配置项 {key} 越出资料库：{candidate}")
    return candidate


def file_record(path: Path, database_root: Path, *, include_hash: bool = True) -> dict[str, Any]:
    """记录文件身份；小成果默认保存完整 SHA256。"""

    stat = path.stat()
    record: dict[str, Any] = {
        "path": relative_to_database(path, database_root),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if include_hash:
        record["sha256"] = sha256_file(path)
    return record


def validate_record(record: dict[str, Any], database_root: Path) -> Path:
    """验证状态中登记的文件存在且大小、SHA256 未变。"""

    path = database_path(record["path"], database_root)
    if not path.is_file():
        raise WorkflowError(f"已登记的成果缺失：{record['path']}")
    if path.stat().st_size != record.get("size_bytes"):
        raise WorkflowError(f"已登记的成果大小改变：{record['path']}")
    expected = record.get("sha256")
    if expected and sha256_file(path) != expected:
        raise WorkflowError(f"已登记的成果哈希不符：{record['path']}")
    return path


def terminology_prompt(config: dict[str, Any], database_root: Path) -> str:
    """读取专名词典并构造 Whisper 初始提示。"""

    default = "以下为中文政治时事节目，请准确识别人名、机构名和政治术语。"
    try:
        path = config_path(config, "terminology_file", database_root)
    except (KeyError, WorkflowError):
        return default
    if not path.is_file():
        return default
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    terms = payload.get("prompt_terms", [])
    return default[:-1] + "：" + "、".join(terms) + "。" if terms else default


def text_sha256(text: str) -> str:
    """计算 UTF-8 文本哈希，用于证明跨子阶段上下文没有被静默替换。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def previous_context_policy(state: dict[str, Any]) -> dict[str, Any]:
    """读取上一子阶段上下文策略；缺少策略的 2.0 旧任务保持关闭。

    状态模式版本仍为 1，因为新增字段均为可选字段。这里不把旧任务自动迁移
    到新行为，既避免追溯改变既有转写，也避免在用户不知情时删除旧中间件。
    """

    raw = state.get("execution", {}).get("previous_chunk_context")
    if not isinstance(raw, dict):
        return {
            "enabled": False,
            "max_segments": DEFAULT_CONTEXT_SEGMENTS,
            "max_chars": DEFAULT_CONTEXT_CHARS,
            "compatibility": "legacy_disabled",
        }
    max_segments = raw.get("max_segments", DEFAULT_CONTEXT_SEGMENTS)
    max_chars = raw.get("max_chars", DEFAULT_CONTEXT_CHARS)
    if not isinstance(max_segments, int) or max_segments <= 0:
        raise WorkflowError("上一子阶段上下文 max_segments 必须是正整数。")
    if not isinstance(max_chars, int) or max_chars <= 0:
        raise WorkflowError("上一子阶段上下文 max_chars 必须是正整数。")
    return {
        "enabled": raw.get("enabled") is True,
        "max_segments": max_segments,
        "max_chars": max_chars,
        "compatibility": "configured",
    }


def stage1_compaction_required(state: dict[str, Any]) -> bool:
    """仅对明确登记了新策略的工作流自动清理第一阶段分段中间件。"""

    return state.get("execution", {}).get("compact_stage1_intermediates") is True


def stage1_compaction_status(state: dict[str, Any]) -> str:
    """返回第一阶段中间件收口状态；旧任务视为 deferred。"""

    cleanup = state.get("stage1", {}).get("intermediate_cleanup")
    if isinstance(cleanup, dict) and isinstance(cleanup.get("status"), str):
        return cleanup["status"]
    return "pending" if stage1_compaction_required(state) else "deferred"


def load_previous_chunk_context(
    state: dict[str, Any],
    chunk: dict[str, Any],
    database_root: Path,
) -> tuple[str, dict[str, Any]]:
    """从上一段 JSON 取有限尾部语境，并返回可审计的来源说明。

    只读取紧邻的上一子阶段，而不是把所有旧文本塞给模型。文本长度和句段数
    同时设上限；若上一段成果缺失、哈希改变或属于别的工作流，则拒绝继续，
    防止在内容有前后依赖时静默失去语境。
    """

    index = int(chunk["index"])
    policy = previous_context_policy(state)
    base_info: dict[str, Any] = {
        "policy_enabled": policy["enabled"],
        "max_segments": policy["max_segments"],
        "max_chars": policy["max_chars"],
    }
    if index == 1:
        return "", {**base_info, "status": "not_applicable", "reason": "first_chunk"}
    if not policy["enabled"]:
        return "", {**base_info, "status": "disabled", "reason": policy["compatibility"]}

    previous = state["stage1"]["chunks"][index - 2]
    if previous.get("status") != "complete":
        raise WorkflowError(f"子阶段 {index} 需要读取子阶段 {index-1}，但上一段尚未完成。")
    json_records = [
        record
        for record in previous.get("outputs", [])
        if isinstance(record, dict) and str(record.get("path", "")).lower().endswith(".json")
        and not str(record.get("path", "")).lower().endswith(".meta.json")
    ]
    if len(json_records) != 1:
        raise WorkflowError(f"子阶段 {index-1} 缺少唯一的 JSON 逐字稿记录，无法建立前文语境。")
    source_path = validate_record(json_records[0], database_root)
    payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    if payload.get("workflow_id") != state.get("workflow_id"):
        raise WorkflowError(f"子阶段 {index-1} 的 JSON 属于另一个工作流。")
    if payload.get("source_media") != state.get("source", {}).get("path"):
        raise WorkflowError(f"子阶段 {index-1} 的 JSON 来源媒体与当前工作流不一致。")
    if int(payload.get("chunk", {}).get("index", -1)) != index - 1:
        raise WorkflowError(f"子阶段 {index-1} 的 JSON 编号与状态不一致。")
    expected_ranges = {
        key: previous[key]
        for key in ("index", "core_start_ms", "core_end_ms", "decode_start_ms", "decode_end_ms")
    }
    if payload.get("chunk") != expected_ranges:
        raise WorkflowError(f"子阶段 {index-1} 的 JSON 时间范围与状态不一致。")
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise WorkflowError(f"子阶段 {index-1} 的 JSON 缺少 segments。")
    if any(not isinstance(item, dict) or not isinstance(item.get("text"), str) for item in segments):
        raise WorkflowError(f"子阶段 {index-1} 的 JSON 含无效文字段。")
    nonempty = [item for item in segments if item["text"].strip()]
    selected_items = nonempty[-policy["max_segments"] :]
    truncated_by_segment_limit = len(nonempty) > policy["max_segments"]

    # 字符超限时先整句丢弃最老语境；只有最新一句本身仍超限时才保留其尾部。
    truncated_by_char_limit = False
    while len(selected_items) > 1 and len(
        " ".join(str(item["text"]).strip() for item in selected_items)
    ) > policy["max_chars"]:
        selected_items.pop(0)
        truncated_by_char_limit = True
    selected = [str(item["text"]).strip() for item in selected_items]
    context_text = " ".join(selected).strip()
    if len(context_text) > policy["max_chars"]:
        context_text = (
            "…"
            if policy["max_chars"] == 1
            else "…" + context_text[-(policy["max_chars"] - 1) :].lstrip()
        )
        truncated_by_char_limit = True
    return context_text, {
        **base_info,
        "status": "used" if context_text else "empty_previous_chunk",
        "source_chunk": index - 1,
        "source_path": json_records[0]["path"],
        "source_sha256": json_records[0].get("sha256"),
        "selected_segment_count": len(selected_items),
        "selected_segment_ids": [item.get("id") for item in selected_items],
        "selected_start_ms": selected_items[0].get("start_ms") if selected_items else None,
        "selected_end_ms": selected_items[-1].get("end_ms") if selected_items else None,
        "text_chars": len(context_text),
        "text_sha256": text_sha256(context_text),
        "truncated_by_segment_limit": truncated_by_segment_limit,
        "truncated_by_char_limit": truncated_by_char_limit,
    }


def build_transcription_prompt(
    config: dict[str, Any],
    database_root: Path,
    previous_text: str,
) -> str:
    """把固定术语提示与有限前文语境组合成单一初始提示。"""

    prompt = terminology_prompt(config, database_root)
    if previous_text:
        prompt += (
            "\n上一子阶段结尾语境（只用于承接人名、指代与话题，不要复述）："
            + previous_text
        )
    return prompt


def _token_ids(tokenizer: Any, text: str) -> list[int]:
    """兼容 Hugging Face ``tokenizers`` 与普通 tokenizer 的编码返回值。"""

    encoded = tokenizer.encode(text)
    ids = encoded.ids if hasattr(encoded, "ids") else encoded
    return [int(item) for item in ids]


def _decode_token_ids(tokenizer: Any, ids: list[int]) -> str:
    """把截取后的 token 安全解码为可读提示。"""

    if not ids:
        return ""
    return str(tokenizer.decode(ids)).strip()


def prepare_bounded_initial_prompt(
    model: Any,
    terminology_text: str,
    previous_text: str,
) -> tuple[str, dict[str, Any]]:
    """在 Whisper 的半上下文限制内给术语与前文分别分配 token 配额。

    faster-whisper 会把 initial_prompt 限制为模型最大文本长度的一半左右。若
    先简单拼接，长词典会让前文或术语被静默截断。这里预留少量说明开销，约
    60% 给专名词典、40% 给前文；术语同时保留首尾，前文保留最接近切点的尾部。
    无法取得 tokenizer 时退回字符级提示，但仍受初始化时的 240 字上限约束。
    """

    tokenizer = getattr(model, "hf_tokenizer", None)
    max_length = int(getattr(model, "max_length", 448) or 448)
    total_budget = max(32, max_length // 2 - 1)
    if tokenizer is None:
        prompt = build_transcription_prompt_from_parts(terminology_text, previous_text)
        return prompt, {
            "token_budget": total_budget,
            "tokenizer_available": False,
            "prompt_sha256": text_sha256(prompt),
        }

    prefix = "专名与术语："
    context_prefix = "\n上一子阶段结尾语境（只承接人名、指代与话题，不要复述）："
    overhead = len(_token_ids(tokenizer, prefix + (context_prefix if previous_text else "")))
    content_budget = max(16, total_budget - overhead)
    context_budget = round(content_budget * 0.4) if previous_text else 0
    terminology_budget = content_budget - context_budget
    term_ids = _token_ids(tokenizer, terminology_text)
    context_ids = _token_ids(tokenizer, previous_text) if previous_text else []

    if len(term_ids) > terminology_budget:
        head = (terminology_budget + 1) // 2
        tail = terminology_budget // 2
        term_text = _decode_token_ids(tokenizer, term_ids[:head])
        if tail:
            term_text += "……" + _decode_token_ids(tokenizer, term_ids[-tail:])
    else:
        term_text = terminology_text
    context_text = (
        _decode_token_ids(tokenizer, context_ids[-context_budget:])
        if context_budget and len(context_ids) > context_budget
        else previous_text
    )
    prompt = build_transcription_prompt_from_parts(term_text, context_text)
    final_ids = _token_ids(tokenizer, prompt)
    # 文本分段解码可能增加极少量标点 token；只在超限时从最远的开头收紧。
    if len(final_ids) > total_budget:
        final_ids = final_ids[-total_budget:]
        prompt = _decode_token_ids(tokenizer, final_ids)
    return prompt, {
        "token_budget": total_budget,
        "tokenizer_available": True,
        "terminology_tokens_available": len(term_ids),
        "terminology_tokens_used": min(len(term_ids), terminology_budget),
        "context_tokens_available": len(context_ids),
        "context_tokens_used": min(len(context_ids), context_budget),
        "prompt_token_count": len(_token_ids(tokenizer, prompt)),
        "prompt_sha256": text_sha256(prompt),
    }


def build_transcription_prompt_from_parts(terminology_text: str, previous_text: str) -> str:
    """组合已经分别限额的术语与前文组件。"""

    prompt = "专名与术语：" + terminology_text
    if previous_text:
        prompt += "\n上一子阶段结尾语境（只承接人名、指代与话题，不要复述）：" + previous_text
    return prompt


def extract_audio_atomic(
    media: Path,
    source_duration_ms: int,
    bitrate: str,
    *,
    force: bool = False,
) -> Path:
    """从视频安全提取整段音频；音频输入直接复用自身。

    只复用与主视频 stem 精确对应的 ``_音频.m4a``，不再猜测同目录的任意音频。
    新音频先写 ``*.partial.m4a``，时长验证通过后才替换正式文件。
    """

    if media.suffix.lower() in AUDIO_EXTENSIONS:
        return media
    audio = media.with_name(f"{media.stem}_音频.m4a")
    tolerance = max(3_000, round(source_duration_ms * 0.002))
    if audio.is_file() and not force:
        audio_duration = probe_duration_ms(audio)
        if abs(audio_duration - source_duration_ms) <= tolerance:
            print(f"复用已验证音频：{audio}", flush=True)
            return audio
        raise WorkflowError(
            f"现有音频时长与视频不符（相差 {abs(audio_duration-source_duration_ms)/1000:.1f} 秒）；"
            "请核对后使用 --force-audio 重建。"
        )

    try:
        import imageio_ffmpeg
    except ImportError as error:
        raise WorkflowError("固定转写环境缺少 imageio-ffmpeg。") from error
    temporary = audio.with_name(f"{audio.stem}.partial{audio.suffix}")
    temporary.unlink(missing_ok=True)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(media),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        bitrate,
        str(temporary),
    ]
    print(f"正在安全提取完整音频：{media.name}", flush=True)
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        extracted_duration = probe_duration_ms(temporary)
        if abs(extracted_duration - source_duration_ms) > tolerance:
            raise WorkflowError(
                f"提取音频时长校验失败：视频 {source_duration_ms} ms，音频 {extracted_duration} ms。"
            )
        os.replace(temporary, audio)
        return audio
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()[-1200:]
        raise WorkflowError(f"FFmpeg 提取音频失败：{detail}") from error
    finally:
        temporary.unlink(missing_ok=True)


def extract_chunk_audio(audio: Path, chunk: dict[str, Any], destination: Path) -> None:
    """提取带重叠的 16 kHz 单声道 WAV，并在完成后原子提交。"""

    try:
        import imageio_ffmpeg
    except ImportError as error:
        raise WorkflowError("固定转写环境缺少 imageio-ffmpeg。") from error
    start_ms = int(chunk["decode_start_ms"])
    length_ms = int(chunk["decode_end_ms"]) - start_ms
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.stem}.partial{destination.suffix}")
    temporary.unlink(missing_ok=True)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(audio),
        "-t",
        f"{length_ms / 1000:.3f}",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(temporary),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        measured = probe_duration_ms(temporary)
        if abs(measured - length_ms) > max(2_000, round(length_ms * 0.005)):
            raise WorkflowError(
                f"子阶段 {chunk['index']} 音频时长校验失败：计划 {length_ms} ms，实际 {measured} ms。"
            )
        os.replace(temporary, destination)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()[-1200:]
        raise WorkflowError(f"FFmpeg 提取子阶段音频失败：{detail}") from error
    finally:
        temporary.unlink(missing_ok=True)


def resolve_device(config: dict[str, Any], requested: str | None, compute: str | None) -> tuple[str, str]:
    """选择 CTranslate2 设备；显式 CUDA 不可静默降级。"""

    try:
        import ctranslate2
    except ImportError as error:
        raise WorkflowError("固定转写环境缺少 CTranslate2。") from error
    preference = requested or str(config.get("preferred_device", "auto"))
    if preference == "auto":
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    else:
        device = preference
    if device == "cpu" and preference == "auto" and not config.get("allow_cpu_fallback", False):
        raise WorkflowError("未检测到 CUDA；自动模式不静默使用 CPU。先运行 asr_runtime.py probe 修复环境，或在用户选择 CPU 后明确传入 --device cpu。")
    if compute:
        compute_type = compute
    elif device == "cuda":
        compute_type = str(config.get("cuda_compute_type", "float16"))
    else:
        compute_type = str(config.get("cpu_compute_type", "int8"))
    return device, compute_type


def transcribe_chunk(
    chunk_audio: Path,
    media: Path,
    chunk: dict[str, Any],
    config: dict[str, Any],
    database_root: Path,
    *,
    device_arg: str | None,
    compute_arg: str | None,
    terminology_text: str | None = None,
    previous_context_text: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """转写一个子阶段，并把局部时间戳转换为原媒体全局时间。

    仅保留语句中点落入本段核心责任区间的结果；最后一段的右端点闭合。这样
    重叠区可提供语境，却不会直接制造重复句。
    """

    from asr_runtime import prepare_libraries, get_model
    prepare_libraries(config, database_root)
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise WorkflowError("固定转写环境缺少 faster-whisper。") from error
    model_path = config_path(config, "whisper_model", database_root)
    required = [model_path / "model.bin", model_path / "config.json", model_path / "tokenizer.json"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise WorkflowError("固定语音模型不完整：" + "、".join(missing))

    device, compute_type = resolve_device(config, device_arg, compute_arg)
    beam_size = int(config.get("beam_size", 3))
    cpu_threads = int(config.get("cpu_threads", 0)) or min(24, max(1, os.cpu_count() or 1))
    print(f"正在加载固定模型；设备={device}，精度={compute_type}", flush=True)
    try:
        model = get_model(model_path, device, compute_type, cpu_threads)
    except Exception as error:
        hint = "；若自动选择 CUDA 但环境库缺失，可明确使用 --device cpu 重试当前段" if device == "cuda" else ""
        raise WorkflowError(f"模型加载失败（{device}/{compute_type}）：{error}{hint}") from error

    initial_prompt, prompt_metrics = prepare_bounded_initial_prompt(
        model,
        terminology_text or terminology_prompt(config, database_root),
        previous_context_text,
    )
    batch_size = int(config.get("asr_batch_size", 1))
    if batch_size < 1 or batch_size > 16:
        raise WorkflowError("asr_batch_size 必须在 1 至 16 之间。")
    if batch_size > 1 and not config.get("asr_batch_validated", False):
        raise WorkflowError("批量 ASR 尚未经本机完整性对照验证；保持 asr_batch_size=1。只有检查全文与时间轴通过后才能启用 asr_batch_validated。")
    runner = model
    batch_options = {}
    if batch_size > 1:
        from faster_whisper import BatchedInferencePipeline
        runner = BatchedInferencePipeline(model=model)
        batch_options["batch_size"] = batch_size
    transcription_started = time.perf_counter()
    last_progress = transcription_started
    segments_iter, info = runner.transcribe(
        str(chunk_audio),
        language="zh",
        task="transcribe",
        beam_size=beam_size,
        best_of=beam_size,
        temperature=0,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
        initial_prompt=initial_prompt,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        repetition_penalty=1.1,
        no_repeat_ngram_size=3,
        **batch_options,
    )

    decode_start = int(chunk["decode_start_ms"])
    core_start = int(chunk["core_start_ms"])
    core_end = int(chunk["core_end_ms"])
    is_last = int(chunk["index"]) == int(chunk["total_chunks"])
    segments: list[dict[str, Any]] = []
    for raw in segments_iter:
        text = raw.text.strip()
        if not text:
            continue
        start_ms = decode_start + round(raw.start * 1000)
        end_ms = decode_start + round(raw.end * 1000)
        midpoint = (start_ms + end_ms) // 2
        owned = core_start <= midpoint < core_end or (is_last and core_start <= midpoint <= core_end)
        if not owned:
            continue
        item = {
            "id": len(segments) + 1,
            "start": start_ms / 1000,
            "end": end_ms / 1000,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
            "avg_logprob": raw.avg_logprob,
            "no_speech_prob": raw.no_speech_prob,
            "compression_ratio": raw.compression_ratio,
            "chunk_index": int(chunk["index"]),
        }
        segments.append(item)
        if time.perf_counter() - last_progress >= 60:
            print(f"子阶段 {chunk['index']} 已转写到全局 {stamp_ms(end_ms)}", flush=True)
            last_progress = time.perf_counter()

    environment = {
        "model": relative_to_database(model_path, database_root),
        "device": device,
        "compute_type": compute_type,
        "cpu_threads": cpu_threads,
        "beam_size": beam_size,
        "language": info.language,
        "language_probability": info.language_probability,
        "initial_prompt": prompt_metrics,
        "batch_size": batch_size,
        "transcription_seconds": round(time.perf_counter() - transcription_started, 3),
    }
    metrics = {
        "decoded_duration_ms": round(info.duration * 1000),
        "duration_after_vad_ms": round(info.duration_after_vad * 1000),
        "segment_count": len(segments),
    }
    return {"segments": segments, "metrics": metrics}, environment


def render_transcript_markdown(title: str, metadata: Iterable[str], segments: Iterable[dict[str, Any]]) -> str:
    """渲染带全局时间戳的 Markdown 逐字稿。"""

    lines = [f"# {title}", "", *metadata, ""]
    for segment in segments:
        lines.append(
            f"**[{stamp_ms(int(segment['start_ms']))}–{stamp_ms(int(segment['end_ms']))}]** "
            f"{segment['text']}"
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_srt(segments: Iterable[dict[str, Any]]) -> str:
    """渲染 SRT；编号由调用方保证连续。"""

    blocks = []
    for segment in segments:
        blocks.append(
            f"{segment['id']}\n"
            f"{stamp_ms(int(segment['start_ms']), srt=True)} --> "
            f"{stamp_ms(int(segment['end_ms']), srt=True)}\n"
            f"{segment['text']}\n"
        )
    return "\n".join(blocks)


def render_jsonl(segments: Iterable[dict[str, Any]]) -> str:
    """渲染一行一段的 UTF-8 JSONL。"""

    return "".join(json.dumps(segment, ensure_ascii=False) + "\n" for segment in segments)


def precise_stamp_ms(milliseconds: int) -> str:
    """以 ``HH:MM:SS.mmm`` 输出时间，供外部字幕无损保留毫秒信息。"""

    milliseconds = max(0, int(milliseconds))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def parse_caption_timestamp(value: str) -> int:
    """解析 SRT/VTT 的 ``HH:MM:SS,mmm`` 或 ``MM:SS.mmm`` 时间戳。"""

    cleaned = value.strip().replace(",", ".")
    fields = cleaned.split(":")
    if len(fields) == 2:
        hours = 0
        minutes_text, seconds_text = fields
    elif len(fields) == 3:
        hours_text, minutes_text, seconds_text = fields
        try:
            hours = int(hours_text)
        except ValueError as error:
            raise WorkflowError(f"字幕时间戳无效：{value}") from error
    else:
        raise WorkflowError(f"字幕时间戳无效：{value}")
    try:
        minutes = int(minutes_text)
        seconds_float = float(seconds_text)
    except ValueError as error:
        raise WorkflowError(f"字幕时间戳无效：{value}") from error
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds_float < 60:
        raise WorkflowError(f"字幕时间戳超出范围：{value}")
    return round((hours * 3600 + minutes * 60 + seconds_float) * 1000)


def parse_caption_cues(text: str, source_format: str) -> list[dict[str, Any]]:
    """解析 SRT/VTT 字幕并完整保留每个 cue 的时间和多行文字。

    VTT 的 NOTE/STYLE/REGION 块属于显示元数据而非讲述文本，会被跳过；其他
    无法识别的非空块会触发错误，避免静默丢掉可能有价值的逐字稿内容。
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = re.split(r"\n[ \t]*\n", normalized.strip())
    cues: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n")]
        if not lines or not any(line.strip() for line in lines):
            continue
        first = lines[0].strip()
        if source_format == "vtt" and (
            first.startswith("WEBVTT")
            or first.startswith("NOTE")
            or first in {"STYLE", "REGION"}
        ):
            continue
        timing_index = next((index for index, line in enumerate(lines[:2]) if "-->" in line), None)
        if timing_index is None:
            raise WorkflowError(f"{source_format.upper()} 中存在无法识别的字幕块：{first[:80]}")
        timing = lines[timing_index]
        start_text, end_and_settings = timing.split("-->", 1)
        end_text = end_and_settings.strip().split()[0]
        start_ms = parse_caption_timestamp(start_text)
        end_ms = parse_caption_timestamp(end_text)
        if end_ms < start_ms:
            raise WorkflowError(f"字幕结束时间早于开始时间：{timing}")
        cue_lines = lines[timing_index + 1 :]
        cue_text = "<br>".join(line.strip() for line in cue_lines if line.strip())
        if not cue_text:
            raise WorkflowError(f"字幕时间段没有正文：{timing}")
        cues.append({"start_ms": start_ms, "end_ms": end_ms, "text": cue_text})
    if not cues:
        raise WorkflowError(f"{source_format.upper()} 中没有可导入的字幕段。")
    return cues


def render_imported_transcript(
    source: Path, raw_text: str, *, source_software: str | None = None
) -> tuple[str, dict[str, Any]]:
    """把 TXT/MD/SRT/VTT 规范化为一个 Markdown 原始逐字稿。

    字幕格式会转成统一的毫秒级 Markdown 时间戳；TXT/MD 只规范换行并原样
    保留正文，不会凭空添加时间戳。返回的第二项用于 manifest 记录转换事实。
    """

    suffix = source.suffix.lower()
    if suffix not in {".txt", ".md", ".srt", ".vtt"}:
        raise WorkflowError(
            f"外部逐字稿导入暂不支持 {suffix or '无扩展名'}；支持 TXT、MD、SRT、VTT。"
        )
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if not normalized.strip():
        raise WorkflowError(f"外部逐字稿为空：{source}")
    if suffix in {".srt", ".vtt"}:
        cues = parse_caption_cues(normalized, suffix.lstrip("."))
        body_lines: list[str] = []
        for cue in cues:
            body_lines.extend(
                [
                    f"**[{precise_stamp_ms(cue['start_ms'])}–{precise_stamp_ms(cue['end_ms'])}]** "
                    f"{cue['text']}",
                    "",
                ]
            )
        body = "\n".join(body_lines).rstrip()
        timestamp_info = {
            "status": "preserved",
            "source_style": suffix.lstrip("."),
            "output_style": "HH:MM:SS.mmm",
            "cue_count": len(cues),
        }
    else:
        body = normalized.strip()
        has_timestamp = bool(
            re.search(
                r"(?:\[|\b)(?:\d{1,2}:)?\d{2}:\d{2}(?:[.,]\d{3})?(?:\]|\b)|\s-->\s",
                body,
            )
        )
        timestamp_info = {
            "status": "preserved_as_source_text" if has_timestamp else "absent_not_fabricated",
            "source_style": "embedded_text" if has_timestamp else None,
            "output_style": "unchanged",
            "cue_count": None,
        }
    metadata = [
        f"- 外部逐字稿原文件：`{source.name}`",
        f"- 原始格式：`{suffix.lstrip('.').upper()}`",
        f"- 来源软件：`{source_software}`" if source_software else "- 来源软件：`未提供`",
        "- 校核状态：`未经原音频人工核验`",
        (
            "- 时间戳：`已从原文件保留，未重新生成`"
            if timestamp_info["status"] != "absent_not_fabricated"
            else "- 时间戳：`原文件没有可识别时间戳；未伪造`"
        ),
        "- 说明：本文件是规范化导入稿；原始文件保持不变。",
    ]
    markdown = "\n".join(["# 原始逐字稿（外部导入）", "", *metadata, "", "---", "", body]).rstrip() + "\n"
    return markdown, timestamp_info


def read_external_transcript_text(path: Path) -> tuple[str, str]:
    """读取常见中文逐字稿编码，并返回文本与实际采用的编码标签。

    优先尊重 UTF BOM，其次尝试无 BOM UTF-8，最后兼容部分 Windows 下载软件
    仍会输出的 GB18030。编码转换只发生在规范化副本中，原文件字节保持不变。
    """

    payload = path.read_bytes()
    attempts: list[tuple[str, str]] = []
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        attempts.append(("utf-16", "utf-16-bom"))
    if payload.startswith(b"\xef\xbb\xbf"):
        attempts.append(("utf-8-sig", "utf-8-bom"))
    attempts.extend((encoding, label) for encoding, label in (("utf-8", "utf-8"), ("gb18030", "gb18030")))
    seen: set[str] = set()
    for encoding, label in attempts:
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            return payload.decode(encoding), label
        except UnicodeError:
            continue
    raise WorkflowError(f"外部逐字稿编码无法识别：{path}")


def select_external_transcript(path: Path, database_root: Path) -> Path:
    """解析显式文件，或在目录中仅有一个候选时选中它。

    目录中出现多个逐字稿时必须拒绝自动合并或自动择一；用户需要重新传入
    一个明确文件路径。显式文件路径本身即代表用户已完成选择。
    """

    resolved = path.resolve()
    if resolved.is_file():
        if resolved.suffix.lower() not in {".txt", ".md", ".srt", ".vtt"}:
            raise WorkflowError("请明确指定 TXT、MD、SRT 或 VTT 外部逐字稿。")
        return resolved
    if not resolved.is_dir():
        raise WorkflowError(f"找不到外部逐字稿：{resolved}")
    inventory = discover_project_materials(resolved, database_root)
    candidates = [
        database_path(item["path"], database_root)
        for item in inventory["materials"]["transcripts"]
        if Path(item["path"]).suffix.lower() in {".txt", ".md", ".srt", ".vtt"}
    ]
    if len(candidates) != 1:
        names = "、".join(candidate.name for candidate in candidates) or "无"
        raise WorkflowError(
            "目录中的外部逐字稿不是唯一候选，拒绝自动选择或合并；"
            f"请显式传入一个文件。当前候选：{names}"
        )
    return candidates[0]


def import_external_transcript(
    source: Path,
    task_dir: Path,
    database_root: Path,
    *,
    source_software: str | None = None,
) -> dict[str, Any]:
    """安全导入一个已由用户选定的外部逐字稿。

    原文件只读；目标固定为 ``01_原始逐字稿.md`` 和 manifest。任何目标冲突
    都会中止而非覆盖。TXT/MD 不生成虚假的 SRT/JSON/JSONL，manifest 会如实
    记录只产生 Markdown 这一种规范化成果。
    """

    source = source.resolve()
    task_dir = task_dir.resolve()
    for label, candidate in (("外部逐字稿", source), ("任务目录", task_dir)):
        if not path_within(candidate, database_root):
            raise WorkflowError(f"{label}越出资料库：{candidate}")
    if not source.is_file():
        raise WorkflowError(f"找不到外部逐字稿：{source}")
    if source.suffix.lower() not in {".txt", ".md", ".srt", ".vtt"}:
        raise WorkflowError("外部逐字稿导入支持 TXT、MD、SRT、VTT。")
    task_dir.mkdir(parents=True, exist_ok=True)
    destination = task_dir / "01_原始逐字稿.md"
    manifest_path = task_dir / FINAL_BUNDLE_MANIFEST
    if source == destination.resolve():
        raise WorkflowError("原文件与规范化目标同名，拒绝覆盖；请把原文件保存在其他名称或子目录。")
    conflicts = [path for path in (destination, manifest_path) if path.exists()]
    if conflicts:
        raise WorkflowError("目标成果已存在，拒绝覆盖：" + "、".join(str(path) for path in conflicts))

    before_stat = source.stat()
    before_hash = sha256_file(source)
    raw_text, source_encoding = read_external_transcript_text(source)
    markdown, timestamp_info = render_imported_transcript(
        source, raw_text, source_software=source_software
    )
    atomic_write_text(destination, markdown, bom=True)
    try:
        after_stat = source.stat()
        after_hash = sha256_file(source)
        if (
            before_stat.st_size != after_stat.st_size
            or before_stat.st_mtime_ns != after_stat.st_mtime_ns
            or before_hash != after_hash
        ):
            raise WorkflowError("导入期间原始逐字稿发生变化；已放弃本次成果，请重新预检。")
        output_record = file_record(destination, database_root)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "workflow_version": WORKFLOW_VERSION,
            "route": "external_transcript_import",
            "import_id": str(uuid.uuid4()),
            "source": {
                **file_record(source, database_root),
                "format": source.suffix.lower().lstrip("."),
                "encoding": source_encoding,
                "source_software": source_software,
                "preserved_unchanged": True,
            },
            "normalization": {
                "timestamps": timestamp_info,
                "audio_verification": "not_performed",
                "generated_formats": ["md"],
                "fabricated_formats": [],
            },
            "outputs": [output_record],
            "imported_at": now_iso(),
        }
        atomic_write_json(manifest_path, manifest)
    except Exception:
        # 只撤销本函数刚创建、尚无配套 manifest 的规范化文件；原文件不动。
        destination.unlink(missing_ok=True)
        raise
    return {
        "status": "imported",
        "source": manifest["source"],
        "output": output_record,
        "manifest": file_record(manifest_path, database_root),
        "timestamps": timestamp_info,
        "requires_audio_verification_disclosure": True,
    }


def write_part_bundle(
    parts_dir: Path,
    media: Path,
    chunk: dict[str, Any],
    payload: dict[str, Any],
    environment: dict[str, Any],
    previous_context: dict[str, Any],
    state: dict[str, Any],
    database_root: Path,
) -> list[dict[str, Any]]:
    """原子提交一个子阶段的 MD/SRT/JSON/JSONL 与元数据。"""

    index = int(chunk["index"])
    base = parts_dir / f"part-{index:03d}"
    paths = {
        "md": base.with_suffix(".md"),
        "srt": base.with_suffix(".srt"),
        "json": base.with_suffix(".json"),
        "jsonl": base.with_suffix(".jsonl"),
    }
    segments = payload["segments"]
    metadata = [
        f"- 原始媒体：`{media.name}`",
        f"- 子阶段：`{index}/{len(state['stage1']['chunks'])}`",
        f"- 核心时间范围：`{stamp_ms(chunk['core_start_ms'])}–{stamp_ms(chunk['core_end_ms'])}`",
        f"- 实际识别范围：`{stamp_ms(chunk['decode_start_ms'])}–{stamp_ms(chunk['decode_end_ms'])}`",
        f"- 模型：`faster-whisper-large-v3-turbo`；设备：`{environment['device']}`；精度：`{environment['compute_type']}`",
        f"- 前文语境：`{previous_context['status']}`"
        + (
            f"；来自子阶段 `{previous_context.get('source_chunk')}`，`{previous_context.get('text_chars', 0)}` 字"
            if previous_context.get("status") == "used"
            else ""
        ),
        "- 说明：机器转写、未经人工校订；时间戳已换算为原媒体全局时间。",
    ]
    part_json = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": state["workflow_id"],
        "source_media": state["source"]["path"],
        "chunk": {key: chunk[key] for key in ("index", "core_start_ms", "core_end_ms", "decode_start_ms", "decode_end_ms")},
        "environment": environment,
        "previous_chunk_context": previous_context,
        "metrics": payload["metrics"],
        "processed_at": now_iso(),
        "segments": segments,
    }
    atomic_write_text(paths["md"], render_transcript_markdown("原始逐字稿（机器转写子阶段）", metadata, segments), bom=True)
    atomic_write_text(paths["srt"], render_srt(segments), bom=True)
    atomic_write_json(paths["json"], part_json)
    atomic_write_text(paths["jsonl"], render_jsonl(segments))
    records = [file_record(path, database_root) for path in paths.values()]
    meta_path = base.with_suffix(".meta.json")
    meta = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": state["workflow_id"],
        "chunk_index": index,
        "source_sha256": state["source"]["identity"]["sha256"],
        "plan_hash": state["execution"]["plan_hash"],
        "ranges": part_json["chunk"],
        "environment": environment,
        "previous_chunk_context": previous_context,
        "metrics": payload["metrics"],
        "outputs": records,
        "completed_at": now_iso(),
    }
    atomic_write_json(meta_path, meta)
    records.append(file_record(meta_path, database_root))
    return records


def state_paths(media: Path) -> tuple[Path, Path, Path, Path]:
    """返回任务目录、交接目录、分段目录和状态文件。"""

    task_dir = media.parent
    handoff_dir = task_dir / HANDOFF_DIRNAME
    parts_dir = task_dir / PARTS_DIRNAME
    return task_dir, handoff_dir, parts_dir, handoff_dir / STATE_FILENAME


def register_owned(state: dict[str, Any], path: Path, database_root: Path) -> None:
    """登记仅由管理器创建、第二阶段完成后可清理的文件。"""

    relative = relative_to_database(path, database_root)
    owned = state.setdefault("temporary_owned_files", [])
    if relative not in owned:
        owned.append(relative)


def validate_owned_temporary_paths(
    state: dict[str, Any], media: Path, database_root: Path
) -> list[Path]:
    """解析清理白名单，并强制每项属于当前任务的两个临时目录。

    状态文件即使损坏或被误改，也不能借 ``temporary_owned_files`` 把资料库内
    的永久原媒体、正式逐字稿或其他任务文件变成删除目标。这里在任何 unlink
    之前完成全量验证；符号链接也拒绝，避免解析后跳出临时目录。
    """

    raw_owned = state.get("temporary_owned_files")
    if not isinstance(raw_owned, list):
        raise WorkflowError("状态中的 temporary_owned_files 不是列表。")
    _, handoff_dir, parts_dir, state_path = state_paths(media)
    allowed_roots = (handoff_dir.resolve(), parts_dir.resolve())
    lock_path = (handoff_dir / ".lock").resolve()
    validated: list[Path] = []
    seen: set[Path] = set()
    for item in raw_owned:
        if not isinstance(item, str) or not item.strip():
            raise WorkflowError("清理白名单含无效路径。")
        unresolved = database_root / Path(item)
        if unresolved.is_symlink():
            raise WorkflowError(f"清理白名单不得包含符号链接：{item}")
        path = database_path(item, database_root).resolve()
        if path == lock_path:
            raise WorkflowError(".lock 由并发控制单独管理，不得写入清理白名单。")
        if path in allowed_roots or not any(path_within(path, root) for root in allowed_roots):
            raise WorkflowError(f"清理白名单路径不属于当前任务临时目录：{item}")
        if path in seen:
            raise WorkflowError(f"清理白名单含重复路径：{item}")
        seen.add(path)
        validated.append(path)
    if state_path.resolve() not in seen:
        raise WorkflowError("清理白名单缺少当前任务的 state.json。")
    return validated


def plan_hash(chunks: list[dict[str, Any]]) -> str:
    """计算不可静默改变的分段计划哈希。"""

    essential = [
        {key: chunk[key] for key in ("index", "core_start_ms", "core_end_ms", "decode_start_ms", "decode_end_ms")}
        for chunk in chunks
    ]
    data = json.dumps(essential, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def next_action(state: dict[str, Any]) -> str:
    """根据状态机生成明确的下一步，供总进度和终端状态共用。"""

    stage1 = state.get("stage1", {})
    stage2 = state.get("stage2", {})
    status = stage1.get("status")
    if status == "ready":
        pending = next((chunk["index"] for chunk in stage1.get("chunks", []) if chunk["status"] == "pending"), None)
        return f"运行第一阶段子阶段 {pending}/{len(stage1.get('chunks', []))}" if pending else "合并第一阶段逐字稿"
    if status == "chunk_running":
        return f"恢复或重做当前子阶段 {stage1.get('current_chunk')}，不要重做更早的完成段"
    if status == "failed_recoverable":
        return f"修复错误后重试当前子阶段 {stage1.get('current_chunk')}"
    if status == "awaiting_continue":
        target = stage1.get("gate", {}).get("target_chunk")
        return f"等待用户确认是否继续子阶段 {target}/{len(stage1.get('chunks', []))}"
    if status == "ready_to_merge":
        return "校验全部子阶段并合并四种 01_原始逐字稿"
    if (
        status == "complete"
        and stage1_compaction_required(state)
        and stage1_compaction_status(state) != "complete"
    ):
        cleanup_status = stage1_compaction_status(state)
        if cleanup_status == "blocked_unknown_files":
            return "移走 01_转写分段中的未登记文件后，重试第一阶段中间件收口"
        return "重试第一阶段中间件收口；正式总逐字稿无需重做"
    if status == "complete" and stage2.get("status") == "pending":
        return "等待用户确认是否进入第二阶段" if state.get("execution", {}).get("mode") == "staged" else "直接进入第二阶段"
    if stage2.get("status") == "running":
        return "完成校正逐字稿、最终研究记录和哈希核对；按当前任务要求与已有授权决定入库"
    if stage2.get("status") == "complete":
        return "第二阶段已完成；清理第一阶段临时交接"
    return "检查状态和错误记录"


def render_progress(state: dict[str, Any]) -> str:
    """从 ``state.json`` 渲染人类可读的总进度交接说明。"""

    chunks = state["stage1"]["chunks"]
    completed = [chunk for chunk in chunks if chunk["status"] == "complete"]
    total_ms = int(state["source"]["duration_ms"])
    completed_ms = sum(int(chunk["core_end_ms"]) - int(chunk["core_start_ms"]) for chunk in completed)
    percent = completed_ms / total_ms * 100 if total_ms else 0
    lines = [
        "# 媒体处理总进度（阶段交接）",
        "",
        "> 本文件由 `state.json` 自动生成，仅供人工阅读；机器续跑以 `state.json` 为准。",
        "",
        "## 当前结论",
        "",
        f"- 第一阶段状态：`{state['stage1']['status']}`",
        f"- 第二阶段状态：`{state['stage2']['status']}`",
        f"- 已完成：`{len(completed)}/{len(chunks)}` 个子阶段，约 `{percent:.1f}%` 媒体时长",
        f"- 下一步：**{next_action(state)}**",
        "",
        "## 来源与环境",
        "",
        f"- 原媒体：`{state['source']['path']}`",
        f"- 类型：`{state['source']['kind']}`；时长：`{stamp_ms(total_ms)}`",
        f"- 来源 SHA256：`{state['source']['identity']['sha256']}`",
        f"- 完整音频：`{state['audio']['path']}`",
        f"- 模式：`{state['execution']['mode']}`；计划确认时间：`{state['execution']['plan_confirmed_at']}`",
        f"- 分段计划哈希：`{state['execution']['plan_hash']}`",
        f"- 上一段文字语境：`{'启用' if previous_context_policy(state)['enabled'] else '关闭'}`；"
        f"最多 `{previous_context_policy(state)['max_segments']}` 段／`{previous_context_policy(state)['max_chars']}` 字",
        f"- 第一阶段中间件自动收口：`{'启用' if stage1_compaction_required(state) else '旧任务兼容保留'}`；"
        f"当前状态：`{stage1_compaction_status(state)}`",
        "",
        "## 第一阶段子阶段",
        "",
        "| 编号 | 核心范围 | 实际识别范围 | 状态 | 尝试 | 转写段数 |",
        "|---:|---|---|---|---:|---:|",
    ]
    for chunk in chunks:
        lines.append(
            f"| {chunk['index']:03d} | {stamp_ms(chunk['core_start_ms'])}–{stamp_ms(chunk['core_end_ms'])} | "
            f"{stamp_ms(chunk['decode_start_ms'])}–{stamp_ms(chunk['decode_end_ms'])} | "
            f"{chunk['status']} | {chunk.get('attempts', 0)} | {chunk.get('segment_count', '')} |"
        )
    lines.extend(["", "## 续跑提示", ""])
    if completed:
        completed_labels = "、".join(f"{chunk['index']:03d}" for chunk in completed)
        lines.append(f"- 不要重新提取完整音频，也不要重做已校验完成的子阶段：{completed_labels}。")
    else:
        lines.append("- 完整音频已登记；开始转写前应先核对状态。")
    lines.extend(
        [
            "- `.partial` 或 `.tmp` 文件不代表完成；若上次中断，只重做状态为运行中／失败的当前段。",
            "- 来源大小、修改时间或哈希变化时，禁止沿用旧计划。",
            "- 第一阶段总逐字稿验证后，新工作流只清理 `01_转写分段`；`00_阶段交接` 继续保留。",
            "- 第二阶段完整成功并核对成稿库哈希之前，不删除 `00_阶段交接`。",
            "",
        ]
    )
    if state.get("errors"):
        lines.extend(["## 最近错误", ""])
        for error in state["errors"][-5:]:
            lines.append(f"- `{error['at']}`：{error['message']}")
        lines.append("")
    lines.extend(
        [
            "## 状态元数据",
            "",
            f"- 工作流 ID：`{state['workflow_id']}`",
            f"- 工作流版本：`{state['workflow_version']}`；状态模式版本：`{state['schema_version']}`",
            f"- 状态修订号：`{state['revision']}`",
            f"- 本说明生成时间：`{now_iso()}`",
            "",
        ]
    )
    return "\n".join(lines)


def render_manifest(state: dict[str, Any]) -> dict[str, Any]:
    """生成分段目录中的精简机器清单。"""

    return {
        "schema_version": state["schema_version"],
        "workflow_id": state["workflow_id"],
        "source": state["source"],
        "execution": state["execution"],
        "audio": state["audio"],
        "stage1_status": state["stage1"]["status"],
        "chunks": state["stage1"]["chunks"],
        "updated_at": now_iso(),
    }


def persist_state(state: dict[str, Any], media: Path, database_root: Path) -> None:
    """先更新派生视图，最后原子写状态文件作为本次提交点。"""

    _, handoff_dir, parts_dir, state_path = state_paths(media)
    progress_path = handoff_dir / PROGRESS_FILENAME
    manifest_path = parts_dir / MANIFEST_FILENAME
    # 第一阶段分段目录完成收口后，后续 stage2-start/status 更新不得把
    # ``01_转写分段/manifest.json`` 再创建出来。
    write_parts_manifest = not (
        state.get("stage1", {}).get("status") == "complete"
        and state.get("stage1", {}).get("merge", {}).get("status") == "complete"
        and stage1_compaction_status(state) == "complete"
    )
    for path in (state_path, progress_path):
        register_owned(state, path, database_root)
    if write_parts_manifest:
        register_owned(state, manifest_path, database_root)
    state["revision"] = int(state.get("revision", 0)) + 1
    state["updated_at"] = now_iso()
    if write_parts_manifest:
        atomic_write_json(manifest_path, render_manifest(state))
    atomic_write_text(progress_path, render_progress(state), bom=True)
    atomic_write_json(state_path, state)


def load_state(media: Path) -> dict[str, Any]:
    """读取并检查状态模式版本。"""

    state_path = state_paths(media)[3]
    if not state_path.is_file():
        raise WorkflowError(f"没有阶段状态：{state_path}；请先在用户确认方案后执行 init。")
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    if state.get("schema_version") != SCHEMA_VERSION:
        raise WorkflowError(
            f"不支持的状态模式版本：{state.get('schema_version')}；当前脚本支持 {SCHEMA_VERSION}。"
        )
    return state


def validate_source(state: dict[str, Any], media: Path, database_root: Path) -> None:
    """快速检查来源；stat 改变时才重算完整哈希。"""

    expected_path = database_path(state["source"]["path"], database_root)
    if expected_path.resolve() != media.resolve():
        raise WorkflowError("当前媒体与交接状态登记的来源不是同一个文件。")
    stat = media.stat()
    identity = state["source"]["identity"]
    if stat.st_size == identity["size_bytes"] and stat.st_mtime_ns == identity["mtime_ns"]:
        return
    current_hash = sha256_file(media)
    if current_hash != identity["sha256"]:
        raise WorkflowError("原媒体已经改变；为防止错接，旧交接状态被拒绝。")
    identity["size_bytes"] = stat.st_size
    identity["mtime_ns"] = stat.st_mtime_ns


def process_is_alive(pid: int) -> bool:
    """尽力判断同机进程是否仍存在；仅用于识别崩溃遗留锁。"""

    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows 的 os.kill 对非控制信号会调用 TerminateProcess，不能拿 signal=0
        # 做无害探测；改用只查询权限的 OpenProcess。
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


@contextlib.contextmanager
def task_lock(handoff_dir: Path, workflow_id: str | None = None) -> Iterator[None]:
    """使用原子创建文件防止两个写进程同时处理同一任务。

    同一主机且 PID 已不存在的锁会自动视为上次异常中断并移除；活锁或来自其他
    主机的锁则拒绝继续，避免破坏状态。
    """

    handoff_dir.mkdir(parents=True, exist_ok=True)
    lock_path = handoff_dir / ".lock"
    if lock_path.exists():
        try:
            old = json.loads(lock_path.read_text(encoding="utf-8-sig"))
        except Exception:
            old = {}
        same_host = old.get("host") == socket.gethostname()
        if same_host and not process_is_alive(int(old.get("pid", -1))):
            lock_path.unlink(missing_ok=True)
        else:
            raise WorkflowError(f"任务已有活动锁：{lock_path}；不要并发处理同一媒体。")
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "created_at": now_iso(),
        "workflow_id": workflow_id,
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(lock_path, flags)
    except FileExistsError as error:
        raise WorkflowError(f"任务刚被另一个进程锁定：{lock_path}") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def render_chunk_receipt(state: dict[str, Any], chunk: dict[str, Any]) -> str:
    """生成完成后不再修改的单段中文交接凭据。"""

    completed = [item["index"] for item in state["stage1"]["chunks"] if item["status"] == "complete"]
    pending = [item["index"] for item in state["stage1"]["chunks"] if item["status"] != "complete"]
    lines = [
        f"# 第一阶段子阶段 {chunk['index']:03d} 完成说明",
        "",
        f"- 完成时间：`{chunk['completed_at']}`",
        f"- 原媒体：`{state['source']['path']}`",
        f"- 原媒体 SHA256：`{state['source']['identity']['sha256']}`",
        f"- 核心责任范围：`{stamp_ms(chunk['core_start_ms'])}–{stamp_ms(chunk['core_end_ms'])}`",
        f"- 实际识别范围：`{stamp_ms(chunk['decode_start_ms'])}–{stamp_ms(chunk['decode_end_ms'])}`",
        f"- 转写段数：`{chunk.get('segment_count', 0)}`",
        f"- 模型／设备／精度：`{chunk['environment']['model']}` / `{chunk['environment']['device']}` / `{chunk['environment']['compute_type']}`",
        f"- 尝试次数：`{chunk.get('attempts', 1)}`",
        "",
        "## 与前一子阶段的衔接",
        "",
    ]
    context = chunk.get("previous_chunk_context", {})
    if context.get("status") == "used":
        lines.extend(
            [
                f"- 已读取子阶段 `{context['source_chunk']:03d}` 的末尾语境。",
                f"- 语境上限：`{context['max_segments']}` 段／`{context['max_chars']}` 字；"
                f"实际：`{context.get('selected_segment_count', 0)}` 段／`{context.get('text_chars', 0)}` 字。",
                f"- 上一段 JSON：`{context['source_path']}` — `{context.get('source_sha256', '未记录')}`",
                f"- 实际语境文本 SHA256：`{context.get('text_sha256', '未记录')}`",
                f"- 模型初始提示 SHA256：`{context.get('prompt_sha256', '未记录')}`；"
                f"使用 `{context.get('context_tokens_used', '未知')}` 个前文 token。",
            ]
        )
    else:
        lines.append(f"- 本段前文语境状态：`{context.get('status', '未记录')}`。")
    lines.extend(
        [
            "- 前文只用于专名、指代和话题承接；识别引擎仍关闭自动承接，避免重复扩散。",
            "",
        "## 输出与哈希",
        "",
        ]
    )
    for record in chunk.get("outputs", []):
        lines.append(f"- `{record['path']}` — `{record.get('sha256', '未记录')}`")
    lines.extend(
        [
            "",
            "## 接手说明",
            "",
            f"- 已完成子阶段：`{completed}`",
            f"- 尚未完成子阶段：`{pending}`",
            f"- 下一步：{next_action(state)}。",
            "- 不要重新提取完整音频，也不要重做已完成并通过哈希校验的子阶段。",
            "- 本文件是人工交接凭据；实际续跑判断以 `state.json` 为准。",
            "",
        ]
    )
    return "\n".join(lines)


def render_stage1_complete(state: dict[str, Any]) -> str:
    """生成第一阶段完成说明，第二阶段完整成功前保留。"""

    cleanup = state["stage1"].get("intermediate_cleanup", {})
    lines = [
        "# 第一阶段完成说明",
        "",
        f"- 完成时间：`{state['stage1']['completed_at']}`",
        f"- 原媒体：`{state['source']['path']}`",
        f"- 第一阶段子阶段：`{len(state['stage1']['chunks'])}` 个，均已通过哈希校验",
        f"- 合并段数：`{state['stage1']['merge']['segment_count']}`",
        "- 已生成并交叉验证：`01_原始逐字稿.md/.srt/.json/.jsonl` 及正式提交清单。",
        f"- 第一阶段中间件收口：`{cleanup.get('status', 'deferred')}`。",
        "- 下一步：进入第二阶段，生成／复核 `02_校正逐字稿.md` 和最终研究记录；按当前任务要求与已有授权安全入库。",
        "- 原媒体、完整音频、正式总逐字稿五项均为永久成果，不属于自动清理范围。",
        "- `00_阶段交接` 继续保留；第二阶段项目成果验证完成后可删除；若本次已授权入库，还必须先通过成稿副本哈希验证。",
        "",
        "## 永久保留的第一阶段成果",
        "",
    ]
    for record in state["stage1"]["merge"]["outputs"]:
        lines.append(
            f"- `{record['path']}` — `{record.get('size_bytes', 0)}` 字节 — `{record['sha256']}`"
        )
    lines.extend(["", "## 子阶段衔接与来源证明", ""])
    policy = previous_context_policy(state)
    lines.append(
        f"- 策略：`{'启用' if policy['enabled'] else '关闭'}`；最多 `{policy['max_segments']}` 段／"
        f"`{policy['max_chars']}` 字，并在 Whisper token 上限内给术语与前文分别配额。"
    )
    for chunk in state["stage1"]["chunks"]:
        context = chunk.get("previous_chunk_context", {})
        if context.get("status") == "used":
            lines.append(
                f"- 子阶段 `{chunk['index']:03d}` ← `{context['source_chunk']:03d}`："
                f"`{context.get('text_chars', 0)}` 字，文本哈希 `{context.get('text_sha256', '未记录')}`，"
                f"提示哈希 `{context.get('prompt_sha256', '未记录')}`。"
            )
        else:
            lines.append(
                f"- 子阶段 `{chunk['index']:03d}`：前文语境 `{context.get('status', '未记录')}`。"
            )
    lines.extend(["", "## 中间文件收口记录", ""])
    if cleanup.get("status") == "complete":
        lines.extend(
            [
                f"- 清理范围：`{cleanup.get('scope', PARTS_DIRNAME)}`，不含 `00_阶段交接`。",
                f"- 完成时间：`{cleanup.get('completed_at')}`。",
                f"- 已删除：`{cleanup.get('deleted_file_count', 0)}` 个登记文件，"
                f"共 `{cleanup.get('deleted_size_bytes', 0)}` 字节。",
                "- 已删除内容包括分段 WAV、分段 MD/SRT/JSON/JSONL、meta 与分段派生清单。",
                "- 正式总逐字稿的提交清单已保存每一子阶段的范围、环境、上下文来源和原输出哈希。",
            ]
        )
    elif cleanup.get("status") == "deferred":
        lines.append("- 这是兼容旧任务的保留策略；分段中间件暂不追溯删除。")
    else:
        lines.extend(
            [
                f"- 当前收口尚未完成：`{cleanup.get('status', 'pending')}`。",
                f"- 原因：{cleanup.get('last_error', '等待重试')}。",
                "- 正式总逐字稿已经提交；下次只重试中间件收口，不重做转写与合并。",
            ]
        )
    lines.extend(["", "## 下一次接手", "", f"- {next_action(state)}。", ""])
    return "\n".join(lines)


def normalize_text(text: str) -> str:
    """仅用于边界重复检查的保守归一化。"""

    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text).lower()


def merge_segments(part_payloads: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """合并全局时间戳段，并保守去除跨分段重叠产生的同句。

    只有来自不同子阶段、时间确实重叠且文本高度相似时才去重。宁可把不确定
    的边界重复留给第二阶段回听，也不宽松删除讲述者真实的重复表达。
    """

    combined = [dict(segment) for payload in part_payloads for segment in payload.get("segments", [])]
    combined.sort(key=lambda item: (int(item["start_ms"]), int(item["end_ms"]), int(item.get("chunk_index", 0))))
    result: list[dict[str, Any]] = []
    warnings: list[str] = []
    for current in combined:
        if result:
            previous = result[-1]
            different_chunk = previous.get("chunk_index") != current.get("chunk_index")
            overlap = min(int(previous["end_ms"]), int(current["end_ms"])) - max(
                int(previous["start_ms"]), int(current["start_ms"])
            )
            left = normalize_text(str(previous["text"]))
            right = normalize_text(str(current["text"]))
            similarity = SequenceMatcher(None, left, right).ratio() if left and right else 0.0
            if different_chunk and overlap > 0 and similarity >= 0.9:
                previous_score = float(previous.get("avg_logprob", -99))
                current_score = float(current.get("avg_logprob", -99))
                if current_score > previous_score:
                    result[-1] = current
                continue
            if different_chunk and overlap > 0 and similarity >= 0.65:
                warnings.append(
                    f"边界 {stamp_ms(int(current['start_ms']))} 存在可能重复，已保留供第二阶段回听。"
                )
        result.append(current)
    for index, segment in enumerate(result, start=1):
        segment["id"] = index
        segment["start"] = int(segment["start_ms"]) / 1000
        segment["end"] = int(segment["end_ms"]) / 1000
    return result, warnings


def validate_bundle(paths: dict[str, Path], expected_count: int) -> None:
    """交叉检查最终 JSON、JSONL、SRT 和 MD 的段数、ID 与时间顺序。"""

    payload = json.loads(paths["json"].read_text(encoding="utf-8-sig"))
    segments = payload.get("segments", [])
    jsonl = [json.loads(line) for line in paths["jsonl"].read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    # 不能只统计 SRT 中的“纯数字行”：讲述内容本身可能恰好是年份、金额或
    # 其他数字，这会被误判为字幕编号。编号行后必须紧跟标准时间轴行，才算
    # 一个字幕块；同时保留捕获值，供下面核对编号和时间戳。
    srt_cues = re.findall(
        r"(?m)^(\d+)\r?\n"
        r"(\d{2,}:\d{2}:\d{2},\d{3}) --> (\d{2,}:\d{2}:\d{2},\d{3})\s*$",
        paths["srt"].read_text(encoding="utf-8-sig"),
    )
    srt_count = len(srt_cues)
    md_count = len(re.findall(r"(?m)^\*\*\[", paths["md"].read_text(encoding="utf-8-sig")))
    counts = {len(segments), len(jsonl), srt_count, md_count, expected_count}
    if len(counts) != 1:
        raise WorkflowError(
            f"最终逐字稿四格式段数不一致：JSON={len(segments)} JSONL={len(jsonl)} SRT={srt_count} MD={md_count}。"
        )
    for expected_id, segment in enumerate(segments, start=1):
        if int(segment.get("id", -1)) != expected_id:
            raise WorkflowError("最终 JSON 的段编号不连续。")
        cue_id, cue_start, cue_end = srt_cues[expected_id - 1]
        if int(cue_id) != expected_id:
            raise WorkflowError("最终 SRT 的段编号不连续。")
        if cue_start != stamp_ms(int(segment["start_ms"]), srt=True) or cue_end != stamp_ms(
            int(segment["end_ms"]), srt=True
        ):
            raise WorkflowError(f"最终 SRT 第 {expected_id} 段的时间戳与 JSON 不一致。")
        if int(segment["end_ms"]) < int(segment["start_ms"]):
            raise WorkflowError(f"第 {expected_id} 段结束时间早于开始时间。")
        if expected_id > 1 and int(segment["start_ms"]) < int(segments[expected_id - 2]["start_ms"]):
            raise WorkflowError("最终 JSON 的开始时间戳不单调。")


def merge_stage1(state: dict[str, Any], media: Path, database_root: Path) -> None:
    """校验全部子阶段，从 JSON 重建四种正式 01 文件并提交完成说明。"""

    task_dir, handoff_dir, parts_dir, _ = state_paths(media)
    if (
        state.get("stage1", {}).get("status") == "complete"
        and state.get("stage1", {}).get("merge", {}).get("status") == "complete"
    ):
        # 清理完成后的 merge 必须幂等，不能再依赖已经删除的 part JSON。
        verify_stage1_outputs(state, database_root)
        if stage1_compaction_required(state) and stage1_compaction_status(state) != "complete":
            compact_stage1_intermediates(state, media, database_root)
        return
    if any(chunk["status"] != "complete" for chunk in state["stage1"]["chunks"]):
        raise WorkflowError("仍有未完成子阶段，禁止合并。")
    state["stage1"]["status"] = "ready_to_merge"
    persist_state(state, media, database_root)
    payloads: list[dict[str, Any]] = []
    for chunk in state["stage1"]["chunks"]:
        for record in chunk["outputs"]:
            validate_record(record, database_root)
        part_json = parts_dir / f"part-{int(chunk['index']):03d}.json"
        payload = json.loads(part_json.read_text(encoding="utf-8-sig"))
        if payload.get("workflow_id") != state["workflow_id"]:
            raise WorkflowError(f"子阶段 {chunk['index']} 属于另一个工作流。")
        payloads.append(payload)
    segments, warnings = merge_segments(payloads)
    base = task_dir / "01_原始逐字稿"
    paths = {
        "md": base.with_suffix(".md"),
        "srt": base.with_suffix(".srt"),
        "json": base.with_suffix(".json"),
        "jsonl": base.with_suffix(".jsonl"),
    }
    metadata = [
        f"- 原始媒体：`{media.name}`",
        f"- 音频：`{Path(state['audio']['path']).name}`",
        "- 转写模型：`faster-whisper-large-v3-turbo`",
        f"- 媒体时长：`{stamp_ms(state['source']['duration_ms'])}`",
        f"- 第一阶段子阶段：`{len(state['stage1']['chunks'])}` 个；时间戳均为原媒体全局时间",
        "- 说明：这是未经人工校订的机器识别结果；边界重叠已按核心责任区间合并。",
    ]
    final_payload = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": state["workflow_id"],
        "source_media": state["source"]["path"],
        "audio": state["audio"]["path"],
        "model": state["environment"]["model"],
        "duration": state["source"]["duration_ms"] / 1000,
        "processed_at": now_iso(),
        "chunk_count": len(state["stage1"]["chunks"]),
        "boundary_warnings": warnings,
        "segments": segments,
    }
    atomic_write_text(paths["md"], render_transcript_markdown("原始逐字稿（机器转写）", metadata, segments), bom=True)
    atomic_write_text(paths["srt"], render_srt(segments), bom=True)
    atomic_write_json(paths["json"], final_payload)
    atomic_write_text(paths["jsonl"], render_jsonl(segments))
    validate_bundle(paths, len(segments))
    records = [file_record(path, database_root) for path in paths.values()]
    commit_path = task_dir / FINAL_BUNDLE_MANIFEST
    chunk_provenance = []
    for chunk in state["stage1"]["chunks"]:
        chunk_provenance.append(
            {
                "index": chunk["index"],
                "ranges": {
                    key: chunk[key]
                    for key in ("core_start_ms", "core_end_ms", "decode_start_ms", "decode_end_ms")
                },
                "segment_count": chunk.get("segment_count", 0),
                "environment": chunk.get("environment", {}),
                "previous_chunk_context": chunk.get("previous_chunk_context", {}),
                "temporary_outputs_before_cleanup": chunk.get("outputs", []),
                "completed_at": chunk.get("completed_at"),
            }
        )
    commit = {
        "schema_version": SCHEMA_VERSION,
        "workflow_version": state.get("workflow_version", WORKFLOW_VERSION),
        "workflow_id": state["workflow_id"],
        "source_media": state["source"]["path"],
        "source_sha256": state["source"]["identity"]["sha256"],
        "audio": state["audio"],
        "execution_policy": {
            "previous_chunk_context": state.get("execution", {}).get("previous_chunk_context"),
            "compact_stage1_intermediates": stage1_compaction_required(state),
        },
        "chunks": chunk_provenance,
        "segment_count": len(segments),
        "boundary_warnings": warnings,
        "outputs": records,
        "committed_at": now_iso(),
    }
    atomic_write_json(commit_path, commit)
    records.append(file_record(commit_path, database_root))
    state["stage1"]["merge"] = {
        "status": "complete",
        "segment_count": len(segments),
        "boundary_warnings": warnings,
        "outputs": records,
    }
    state["stage1"]["status"] = "complete"
    state["stage1"]["current_chunk"] = None
    state["stage1"]["completed_at"] = now_iso()
    cleanup = state["stage1"].setdefault("intermediate_cleanup", {})
    cleanup.update(
        {
            "status": "pending" if stage1_compaction_required(state) else "deferred",
            "scope": PARTS_DIRNAME,
            "last_error": None,
        }
    )
    state["stage1"]["gate"] = None
    if not stage1_compaction_required(state) and state["execution"]["mode"] == "staged":
        state["stage1"]["gate"] = {"kind": "enter_stage2", "created_at": now_iso()}
    completion_path = handoff_dir / "第一阶段完成.md"
    atomic_write_text(completion_path, render_stage1_complete(state), bom=True)
    register_owned(state, completion_path, database_root)
    persist_state(state, media, database_root)
    if stage1_compaction_required(state):
        compact_stage1_intermediates(state, media, database_root)


def initialize_state(
    media: Path,
    database_root: Path,
    *,
    mode: str,
    chunk_count: int,
    overlap_seconds: int,
    force_audio: bool,
    requested_device: str | None,
    requested_compute: str | None,
    previous_context_enabled: bool = True,
    previous_context_segments: int = DEFAULT_CONTEXT_SEGMENTS,
    previous_context_chars: int = DEFAULT_CONTEXT_CHARS,
    compact_stage1_intermediates: bool = True,
) -> dict[str, Any]:
    """提取／验证完整音频并创建经用户确认的分段状态。"""

    task_dir, handoff_dir, parts_dir, state_path = state_paths(media)
    if state_path.is_file():
        state = load_state(media)
        validate_source(state, media, database_root)
        return state
    for directory in (handoff_dir, parts_dir):
        if directory.is_dir() and any(item.name != ".lock" for item in directory.rglob("*")):
            raise WorkflowError(f"发现没有 state.json 的非空工作目录，拒绝覆盖：{directory}")

    duration_ms = probe_duration_ms(media)
    if previous_context_segments <= 0 or previous_context_chars <= 0:
        raise WorkflowError("上一段语境的段数和字符上限必须是正整数。")
    if duration_ms > LONG_MEDIA_THRESHOLD_MS and chunk_count < 2:
        raise WorkflowError("完整转写时，超过一个半小时的媒体必须至少分成两个内部转写段。")
    chunks = build_chunk_plan(duration_ms, chunk_count, overlap_seconds * 1000)
    config = load_config(database_root)
    audio = extract_audio_atomic(
        media,
        duration_ms,
        str(config.get("audio_bitrate", "128k")),
        force=force_audio,
    )
    print("正在登记原媒体与音频 SHA256；大文件可能需要几分钟。", flush=True)
    source_identity = file_record(media, database_root)
    audio_identity = file_record(audio, database_root)
    model_path = config_path(config, "whisper_model", database_root)
    terminology = config_path(config, "terminology_file", database_root)
    for chunk in chunks:
        chunk.update({"attempts": 0, "outputs": [], "warnings": []})
    created = now_iso()
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "workflow_version": WORKFLOW_VERSION,
        "workflow_id": str(uuid.uuid4()),
        "revision": 0,
        "created_at": created,
        "updated_at": created,
        "source": {
            "kind": "audio" if media.suffix.lower() in AUDIO_EXTENSIONS else "video",
            "path": relative_to_database(media, database_root),
            "duration_ms": duration_ms,
            "identity": {
                "size_bytes": source_identity["size_bytes"],
                "mtime_ns": source_identity["mtime_ns"],
                "sha256": source_identity["sha256"],
            },
        },
        "audio": {
            "status": "complete",
            **audio_identity,
            "duration_ms": probe_duration_ms(audio),
        },
        "execution": {
            "mode": mode,
            "pause_after_each_chunk": mode == "staged",
            "plan_confirmed": True,
            "plan_confirmed_at": created,
            "chunk_count": chunk_count,
            "overlap_ms": overlap_seconds * 1000,
            "plan_hash": plan_hash(chunks),
            "requested_device": requested_device or "auto",
            "requested_compute_type": requested_compute,
            "previous_chunk_context": {
                "enabled": previous_context_enabled,
                "strategy": "verified_previous_part_tail",
                "max_segments": previous_context_segments,
                "max_chars": previous_context_chars,
                "token_strategy": "balanced_model_half_context",
            },
            "compact_stage1_intermediates": compact_stage1_intermediates,
        },
        "environment": {
            "model": relative_to_database(model_path, database_root),
            "terminology_file": relative_to_database(terminology, database_root) if terminology.exists() else None,
            "terminology_sha256": sha256_file(terminology) if terminology.is_file() else None,
        },
        "stage1": {
            "status": "ready",
            "current_chunk": None,
            "gate": None,
            "chunks": chunks,
            "merge": {"status": "pending", "outputs": []},
            "intermediate_cleanup": {
                "status": "pending" if compact_stage1_intermediates else "deferred",
                "scope": PARTS_DIRNAME,
            },
        },
        "stage2": {"status": "pending", "outputs": []},
        "errors": [],
        "temporary_owned_files": [],
    }
    handoff_dir.mkdir(parents=True, exist_ok=True)
    parts_dir.mkdir(parents=True, exist_ok=True)
    persist_state(state, media, database_root)
    return state


def reset_interrupted_chunk(state: dict[str, Any]) -> None:
    """把硬中断留下的运行中段恢复为可重试，保留此前完成段。"""

    if state["stage1"]["status"] != "chunk_running":
        return
    current = int(state["stage1"].get("current_chunk") or 0)
    chunk = next((item for item in state["stage1"]["chunks"] if item["index"] == current), None)
    if chunk:
        chunk["status"] = "pending"
        chunk.setdefault("warnings", []).append("检测到上次进程中断；本次仅重做当前段。")
    state["stage1"]["status"] = "ready"
    state["stage1"]["current_chunk"] = None


def run_next_chunk(
    state: dict[str, Any],
    media: Path,
    database_root: Path,
    *,
    requested_index: int | None,
    device_arg: str | None,
    compute_arg: str | None,
) -> dict[str, Any]:
    """恰好处理一个待办子阶段；最后一段成功后自动合并第一阶段。"""

    reset_interrupted_chunk(state)
    if state["stage1"]["status"] == "awaiting_continue":
        raise WorkflowError("上一子阶段已完成；必须在用户明确说“继续”后先执行 approve-next。")
    if state["stage1"]["status"] == "complete":
        print("第一阶段已经完成；没有需要重做的子阶段。", flush=True)
        return state
    if state["stage1"]["status"] not in {"ready", "failed_recoverable"}:
        raise WorkflowError(f"当前第一阶段状态不允许转写：{state['stage1']['status']}")
    chunk = next((item for item in state["stage1"]["chunks"] if item["status"] != "complete"), None)
    if chunk is None:
        merge_stage1(state, media, database_root)
        return state
    if requested_index is not None and requested_index != chunk["index"]:
        raise WorkflowError(f"下一段应为 {chunk['index']}，拒绝跳到 {requested_index}。")

    validate_source(state, media, database_root)
    audio = validate_record(state["audio"], database_root)
    _, handoff_dir, parts_dir, _ = state_paths(media)
    chunk["total_chunks"] = len(state["stage1"]["chunks"])
    chunk["attempts"] = int(chunk.get("attempts", 0)) + 1
    chunk["status"] = "running"
    chunk["started_at"] = now_iso()
    state["stage1"]["status"] = "chunk_running"
    state["stage1"]["current_chunk"] = chunk["index"]
    persist_state(state, media, database_root)

    # 只有“子阶段完成状态已经持久化，随后确实开始 merge”才属于可只重做
    # 合并的失败。不能仅凭内存中的 chunk.status=complete 判断，否则完成说明
    # 或状态提交失败也会被误当成末段合并失败，导致非末段工作流卡死。
    chunk_state_committed = False
    merge_started = False
    try:
        chunk_audio = parts_dir / "音频" / f"part-{int(chunk['index']):03d}.wav"
        previous_text, previous_context = load_previous_chunk_context(state, chunk, database_root)
        chunk["previous_chunk_context"] = previous_context
        extract_chunk_audio(audio, chunk, chunk_audio)
        config = load_config(database_root)
        payload, environment = transcribe_chunk(
            chunk_audio,
            media,
            chunk,
            config,
            database_root,
            device_arg=device_arg or state.get("execution", {}).get("requested_device"),
            compute_arg=compute_arg or state.get("execution", {}).get("requested_compute_type"),
            terminology_text=terminology_prompt(config, database_root),
            previous_context_text=previous_text,
        )
        prompt_metrics = environment.get("initial_prompt", {})
        if isinstance(prompt_metrics, dict):
            previous_context.update(prompt_metrics)
        outputs = write_part_bundle(
            parts_dir,
            media,
            chunk,
            payload,
            environment,
            previous_context,
            state,
            database_root,
        )
        outputs.append(file_record(chunk_audio, database_root))
        for record in outputs:
            validate_record(record, database_root)
            register_owned(state, database_path(record["path"], database_root), database_root)
        chunk["status"] = "complete"
        chunk["completed_at"] = now_iso()
        chunk["segment_count"] = payload["metrics"]["segment_count"]
        chunk["environment"] = environment
        chunk["outputs"] = outputs
        state["stage1"]["current_chunk"] = None
        remaining = [item for item in state["stage1"]["chunks"] if item["status"] != "complete"]
        if remaining:
            if state["execution"]["mode"] == "staged":
                state["stage1"]["status"] = "awaiting_continue"
                state["stage1"]["gate"] = {
                    "kind": "continue_next_chunk",
                    "target_chunk": remaining[0]["index"],
                    "created_at": now_iso(),
                }
            else:
                state["stage1"]["status"] = "ready"
                state["stage1"]["gate"] = None
        else:
            state["stage1"]["status"] = "ready_to_merge"
            state["stage1"]["gate"] = None
        receipt = handoff_dir / "子阶段" / f"第一阶段_子阶段_{int(chunk['index']):03d}_完成说明.md"
        atomic_write_text(receipt, render_chunk_receipt(state, chunk), bom=True)
        register_owned(state, receipt, database_root)
        persist_state(state, media, database_root)
        chunk_state_committed = True
        if not remaining:
            merge_started = True
            merge_stage1(state, media, database_root)
        return state
    except BaseException as error:
        # 如果段成果已经完整提交，只是末段后的 bundle 合并失败，就保留该段完成
        # 状态并把下一步设为 merge；否则下次会无谓地重新转写整段。
        formal_merge_complete = (
            merge_started
            and chunk_state_committed
            and state.get("stage1", {}).get("status") == "complete"
            and state.get("stage1", {}).get("merge", {}).get("status") == "complete"
        )
        merge_failure = (
            merge_started
            and chunk_state_committed
            and chunk.get("status") == "complete"
            and not formal_merge_complete
        )
        if formal_merge_complete:
            cleanup = state["stage1"].setdefault("intermediate_cleanup", {})
            cleanup.update(
                {
                    "status": "failed_recoverable",
                    "failed_at": now_iso(),
                    "last_error": str(error),
                    "scope": PARTS_DIRNAME,
                }
            )
            state["stage1"]["current_chunk"] = None
            state["stage1"]["gate"] = None
        elif merge_failure:
            state["stage1"]["status"] = "ready_to_merge"
            state["stage1"]["current_chunk"] = None
            merge_state = state["stage1"].setdefault("merge", {"outputs": []})
            merge_state["status"] = "failed_recoverable"
            merge_state["last_error"] = str(error)
        else:
            # KeyboardInterrupt 也要尽力留下可接手状态；硬杀进程则由下次
            # reset_interrupted_chunk 处理。
            chunk["status"] = "failed_recoverable"
            chunk.setdefault("warnings", []).append(str(error))
            state["stage1"]["status"] = "failed_recoverable"
            state["stage1"]["current_chunk"] = chunk["index"]
            state["stage1"]["gate"] = None
        state.setdefault("errors", []).append(
            {
                "at": now_iso(),
                "message": str(error),
                "chunk": None if (merge_failure or formal_merge_complete) else chunk["index"],
                "operation": (
                    "compact_stage1" if formal_merge_complete else "merge" if merge_failure else "transcribe_chunk"
                ),
            }
        )
        persist_state(state, media, database_root)
        raise


def verify_stage1_outputs(state: dict[str, Any], database_root: Path) -> None:
    """在进入或完成第二阶段前重新验证正式逐字稿提交清单。"""

    merge = state["stage1"].get("merge", {})
    if state["stage1"]["status"] != "complete" or merge.get("status") != "complete":
        raise WorkflowError("第一阶段尚未完整提交。")
    records = merge.get("outputs")
    if not isinstance(records, list) or len(records) != 5:
        raise WorkflowError("第一阶段正式提交清单必须完整登记四种逐字稿和一个 manifest。")
    task_dir = database_path(state["source"]["path"], database_root).parent
    base = task_dir / "01_原始逐字稿"
    expected_paths = {
        base.with_suffix(".md").resolve(),
        base.with_suffix(".srt").resolve(),
        base.with_suffix(".json").resolve(),
        base.with_suffix(".jsonl").resolve(),
        (task_dir / FINAL_BUNDLE_MANIFEST).resolve(),
    }
    actual_paths: set[Path] = set()
    for record in records:
        if not isinstance(record, dict):
            raise WorkflowError("第一阶段正式提交清单含无效记录。")
        actual_paths.add(validate_record(record, database_root).resolve())
    if actual_paths != expected_paths:
        raise WorkflowError("第一阶段正式提交清单的五个路径与预期不一致。")

    commit_path = task_dir / FINAL_BUNDLE_MANIFEST
    commit = json.loads(commit_path.read_text(encoding="utf-8-sig"))
    if commit.get("workflow_id") != state.get("workflow_id"):
        raise WorkflowError("第一阶段正式 manifest 属于另一个工作流。")
    if commit.get("source_sha256") != state.get("source", {}).get("identity", {}).get("sha256"):
        raise WorkflowError("第一阶段正式 manifest 的来源哈希不一致。")
    expected_count = merge.get("segment_count")
    if not isinstance(expected_count, int) or commit.get("segment_count") != expected_count:
        raise WorkflowError("第一阶段正式 manifest 的段数与状态不一致。")
    commit_outputs = commit.get("outputs")
    if not isinstance(commit_outputs, list) or len(commit_outputs) != 4:
        raise WorkflowError("第一阶段正式 manifest 未完整登记四种逐字稿。")
    commit_paths = {validate_record(record, database_root).resolve() for record in commit_outputs}
    if commit_paths != expected_paths - {commit_path.resolve()}:
        raise WorkflowError("第一阶段正式 manifest 的四种逐字稿路径不完整。")
    validate_bundle(
        {
            "md": base.with_suffix(".md"),
            "srt": base.with_suffix(".srt"),
            "json": base.with_suffix(".json"),
            "jsonl": base.with_suffix(".jsonl"),
        },
        expected_count,
    )


def find_unknown_stage1_intermediate_entries(
    state: dict[str, Any], media: Path, database_root: Path
) -> list[str]:
    """只检查 ``01_转写分段``；第一阶段收口绝不触碰 ``00_阶段交接``。"""

    _, _, parts_dir, _ = state_paths(media)
    known_files = {
        path.resolve()
        for path in validate_owned_temporary_paths(state, media, database_root)
        if path_within(path, parts_dir)
    }
    known_dirs = {parts_dir.resolve(), (parts_dir / "音频").resolve()}
    unknown: list[str] = []
    if not parts_dir.exists():
        return unknown
    for item in parts_dir.rglob("*"):
        resolved = item.resolve()
        if item.is_symlink():
            unknown.append(relative_to_database(item, database_root))
        elif item.is_file() and resolved not in known_files:
            unknown.append(relative_to_database(item, database_root))
        elif item.is_dir() and resolved not in known_dirs:
            unknown.append(relative_to_database(item, database_root) + "/")
        elif not item.is_file() and not item.is_dir():
            unknown.append(relative_to_database(item, database_root))
    return sorted(set(unknown))


def compact_stage1_intermediates(
    state: dict[str, Any], media: Path, database_root: Path
) -> bool:
    """验证正式总稿后，幂等清理第一阶段分段中间件。

    正式五项成果和 ``00_阶段交接`` 是恢复锚点，始终先保留。任何未知文件、
    越界白名单、哈希异常或权限错误都只改变 ``intermediate_cleanup``，绝不把
    ``stage1/merge`` 从 complete 回滚，也绝不重新转写。
    """

    if not stage1_compaction_required(state):
        state["stage1"].setdefault("intermediate_cleanup", {}).update(
            {"status": "deferred", "scope": PARTS_DIRNAME}
        )
        return True
    verify_stage1_outputs(state, database_root)
    task_dir, handoff_dir, parts_dir, _ = state_paths(media)
    if parts_dir.parent.resolve() != task_dir.resolve():
        raise WorkflowError("第一阶段分段目录不是任务目录的直接子目录，拒绝清理。")
    cleanup = state["stage1"].setdefault("intermediate_cleanup", {})
    if cleanup.get("status") == "complete":
        if parts_dir.exists() and any(parts_dir.iterdir()):
            raise WorkflowError("第一阶段已记为收口完成，但分段目录又出现内容；请人工核对。")
        return True

    cleanup.update(
        {
            "status": "running",
            "scope": PARTS_DIRNAME,
            "started_at": cleanup.get("started_at", now_iso()),
            "last_attempt_at": now_iso(),
            "last_error": None,
        }
    )
    state["stage1"]["gate"] = None
    persist_state(state, media, database_root)
    completion_path = handoff_dir / "第一阶段完成.md"
    try:
        # 删除第一个文件前完成整份白名单与未知条目校验。
        owned = validate_owned_temporary_paths(state, media, database_root)
        unknown = find_unknown_stage1_intermediate_entries(state, media, database_root)
        if unknown:
            raise Stage1CompactionError(
                "01_转写分段含未登记文件，未删除：" + "、".join(unknown)
            )
        part_paths = [
            path
            for path in owned
            if path_within(path, parts_dir) and path.resolve() != parts_dir.resolve()
        ]
        for path in part_paths:
            if path.exists() and (path.is_symlink() or not path.is_file()):
                raise Stage1CompactionError(f"登记的分段清理目标不是普通文件：{path}")
        existing = [path for path in part_paths if path.is_file()]
        # A registered name is not permission to delete user edits. Verify all
        # surviving part outputs before deleting the first file; missing files
        # may belong to an interrupted, already validated cleanup attempt.
        for chunk in state["stage1"]["chunks"]:
            for record in chunk.get("outputs", []):
                recorded_path = database_path(record["path"], database_root)
                if recorded_path.is_file() and path_within(recorded_path, parts_dir):
                    validate_record(record, database_root)
        deleted_size = sum(path.stat().st_size for path in existing)
        for path in sorted(existing, key=lambda item: len(item.parts), reverse=True):
            path.unlink(missing_ok=True)
        for directory in ((parts_dir / "音频"), parts_dir):
            if directory.exists():
                directory.rmdir()

        # 已删除路径从白名单移出；其范围、大小与哈希已经永久写入正式 manifest。
        part_resolved = {path.resolve() for path in part_paths}
        state["temporary_owned_files"] = [
            item
            for item in state.get("temporary_owned_files", [])
            if database_path(item, database_root).resolve() not in part_resolved
        ]
        cleanup.update(
            {
                "status": "complete",
                "completed_at": now_iso(),
                "deleted_file_count": len(existing),
                "deleted_size_bytes": deleted_size,
                "last_error": None,
            }
        )
        if state["execution"]["mode"] == "staged" and state["stage2"]["status"] == "pending":
            state["stage1"]["gate"] = {"kind": "enter_stage2", "created_at": now_iso()}
        atomic_write_text(completion_path, render_stage1_complete(state), bom=True)
        register_owned(state, completion_path, database_root)
        persist_state(state, media, database_root)
        return True
    except (OSError, WorkflowError, UnicodeError, json.JSONDecodeError) as error:
        cleanup.update(
            {
                "status": (
                    "blocked_unknown_files"
                    if isinstance(error, Stage1CompactionError) and "未登记文件" in str(error)
                    else "failed_recoverable"
                ),
                "failed_at": now_iso(),
                "last_error": str(error),
            }
        )
        state["stage1"]["gate"] = None
        state.setdefault("errors", []).append(
            {"at": now_iso(), "message": str(error), "operation": "compact_stage1"}
        )
        atomic_write_text(completion_path, render_stage1_complete(state), bom=True)
        register_owned(state, completion_path, database_root)
        persist_state(state, media, database_root)
        return False


def find_unknown_temporary_entries(
    state: dict[str, Any], media: Path, database_root: Path, *, ignore_lock: bool = True
) -> list[str]:
    """列出临时目录中不是本工作流登记的文件或目录。"""

    _, handoff_dir, parts_dir, _ = state_paths(media)
    # 先验证整份白名单，再把它用于未知文件检查；绝不让损坏状态把永久文件
    # 混入后续清理目标。
    known_files = {path.resolve() for path in validate_owned_temporary_paths(state, media, database_root)}
    if ignore_lock:
        known_files.add((handoff_dir / ".lock").resolve())
    known_dirs = {
        handoff_dir.resolve(),
        (handoff_dir / "子阶段").resolve(),
        parts_dir.resolve(),
        (parts_dir / "音频").resolve(),
    }
    unknown: list[str] = []
    for base in (handoff_dir, parts_dir):
        if not base.exists():
            continue
        for item in base.rglob("*"):
            resolved = item.resolve()
            if item.is_file() and resolved not in known_files:
                unknown.append(relative_to_database(item, database_root))
            elif item.is_dir() and resolved not in known_dirs:
                unknown.append(relative_to_database(item, database_root) + "/")
    return sorted(set(unknown))


def cleanup_temporary_state(state: dict[str, Any], media: Path, database_root: Path) -> None:
    """只删除明确登记的第一阶段临时文件，状态文件最后删除。"""

    task_dir, handoff_dir, parts_dir, state_path = state_paths(media)
    if handoff_dir.parent.resolve() != task_dir.resolve() or parts_dir.parent.resolve() != task_dir.resolve():
        raise WorkflowError("临时目录不是目标资料夹的直接子目录，拒绝清理。")
    # 必须在删除第一个字节前一次性验证全部目标；任何越界项都会使整个清理
    # 原样停止，永久文件不会被部分删除。
    owned = validate_owned_temporary_paths(state, media, database_root)
    unknown = find_unknown_temporary_entries(state, media, database_root)
    if unknown:
        raise WorkflowError("临时目录含未登记文件，未自动删除：" + "、".join(unknown))
    # state.json 是恢复清理所需的最后凭据。先删普通文件和可独立移除的子目录；
    # 此前任何失败都仍能用原状态重试。
    for path in sorted(
        (path for path in owned if path.resolve() != state_path.resolve()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        path.unlink(missing_ok=True)
    for directory in sorted(
        [(handoff_dir / "子阶段"), (parts_dir / "音频"), parts_dir],
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if directory.exists():
            try:
                directory.rmdir()
            except OSError as error:
                raise WorkflowError(f"临时目录未能清空：{directory}（{error}）") from error
    state_path.unlink(missing_ok=True)
    (handoff_dir / ".lock").unlink(missing_ok=True)
    try:
        handoff_dir.rmdir()
    except OSError as error:
        # 极少数权限／占用错误发生在最后一步时，恢复最小 state.json，使下一次
        # complete 仍有入口；task_lock 的 finally 会处理锁文件。
        try:
            atomic_write_json(state_path, state)
        except OSError:
            pass
        raise WorkflowError(f"阶段交接目录未能删除：{handoff_dir}（{error}）") from error


def resolve_artifact_path(value: Path | None, default: Path, database_root: Path) -> Path:
    """解析第二阶段成果参数，并限制在资料库内。"""

    if value is None:
        path = default.resolve()
    elif value.is_absolute():
        path = value.expanduser().resolve()
    else:
        path = (database_root / value).resolve()
    if not path_within(path, database_root):
        raise WorkflowError(f"成果路径越出资料库：{path}")
    return path


def run_fidelity_audit(source: Path, final: Path, report: Path) -> dict[str, Any]:
    """调用同目录忠实度审计器，并把其错误转成统一工作流错误。"""

    script = Path(__file__).resolve().with_name("audit_source_fidelity.py")
    spec = importlib.util.spec_from_file_location("mdlib_source_fidelity_auditor", script)
    if spec is None or spec.loader is None:
        raise WorkflowError("无法加载原材料覆盖审计器。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.audit_report(source, final, report)
    except module.FidelityError as error:
        raise WorkflowError(f"原材料覆盖校验失败：{error}") from error


def command_inspect(args: argparse.Namespace) -> None:
    """执行材料清单和媒体时长预检，全程只读。"""

    resolved, root = resolve_input(args.input, args.database_root)
    payload = inspect_project_payload(resolved, root, args.target_minutes, args.overlap_seconds)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    inventory = payload["material_inventory"]
    counts = inventory["counts"]
    print(
        "材料："
        f"视频 {counts['videos']}；音频 {counts['audios']}；逐字稿候选 {counts['transcripts']}"
    )
    if payload["source"] is not None:
        print(f"媒体：{payload['source']}")
        print(f"时长：{payload['duration']}（{payload['duration_ms']} ms）")
        print(
            "是否超过完整转写的一个半小时阈值："
            f"{'是' if payload['is_over_long_media_threshold'] else '否'}"
        )
    else:
        print(payload["notice"])
    if payload["requires_user_material_choice"]:
        print("检测到多种／多个候选材料，必须先由用户选择处理方式：")
        for option in inventory["user_choice"]["options"]:
            print(f"  - {option['id']}：{option['label']}（成本：{option['cost']}）")
    if payload["existing_workflow_issue"]:
        print(f"发现不可直接续跑的旧状态：{payload['existing_workflow_issue']['message']}")
    elif payload["existing_workflow"]:
        print(f"已有可续跑状态：{payload['existing_workflow']['next_action']}")
    elif payload["is_over_long_media_threshold"]:
        print(f"建议第一阶段分成 {payload['suggested_chunk_count']} 个子阶段：")
        for chunk in payload["suggested_ranges"]:
            print(
                f"  {chunk['index']:03d}: {stamp_ms(chunk['core_start_ms'])}–{stamp_ms(chunk['core_end_ms'])}"
            )


def command_import_transcript(args: argparse.Namespace) -> None:
    """在用户明确选择文件后导入外部机器逐字稿。"""

    if not args.confirmed_by_user:
        raise WorkflowError(
            "import-transcript 必须带 --confirmed-by-user；多资料并存时应先让用户选择。"
        )
    resolved, root = resolve_input(args.input, args.database_root)
    source = select_external_transcript(resolved, root)
    if args.task_dir is None:
        task_dir = resolved if resolved.is_dir() else source.parent
    else:
        task_dir, task_root = resolve_input(args.task_dir, root)
        if task_root != root:
            raise WorkflowError("外部逐字稿与任务目录必须位于同一资料库。")
        if task_dir.exists() and not task_dir.is_dir():
            raise WorkflowError(f"--task-dir 不是目录：{task_dir}")
    result = import_external_transcript(
        source,
        task_dir,
        root,
        source_software=args.source_software,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "外部逐字稿已规范化导入；原文件保持不变。"
            f"成果：{result['output']['path']}；manifest：{result['manifest']['path']}"
        )


def command_init(args: argparse.Namespace) -> None:
    """在对话层已取得用户确认后初始化计划。"""

    resolved, root = resolve_input(args.input, args.database_root)
    media = choose_media(resolved)
    duration = probe_duration_ms(media)
    if (duration > LONG_MEDIA_THRESHOLD_MS or args.mode == "staged") and not args.confirmed_by_user:
        raise WorkflowError("长媒体或分阶段模式需确认方案；已有连续处理授权可作为 --confirmed-by-user 的依据。")
    count = args.chunk_count or suggest_chunk_count(duration, args.target_minutes)
    _, handoff_dir, _, state_path = state_paths(media)
    with task_lock(handoff_dir, None if not state_path.exists() else load_state(media).get("workflow_id")):
        state = initialize_state(
            media,
            root,
            mode=args.mode,
            chunk_count=count,
            overlap_seconds=args.overlap_seconds,
            force_audio=args.force_audio,
            requested_device=args.device,
            requested_compute=args.compute_type,
            previous_context_enabled=not args.no_previous_context,
            previous_context_segments=args.previous_context_segments,
            previous_context_chars=args.previous_context_chars,
            compact_stage1_intermediates=not args.keep_stage1_intermediates,
        )
    print(f"第一阶段计划已登记：{len(state['stage1']['chunks'])} 段；下一步：{next_action(state)}")


def command_run_chunk(args: argparse.Namespace) -> None:
    """处理恰好一个子阶段。"""

    resolved, root = resolve_input(args.input, args.database_root)
    media = choose_media(resolved)
    state = load_state(media)
    _, handoff_dir, _, _ = state_paths(media)
    with task_lock(handoff_dir, state["workflow_id"]):
        state = load_state(media)
        run_next_chunk(
            state,
            media,
            root,
            requested_index=args.index,
            device_arg=args.device,
            compute_arg=args.compute_type,
        )
    print(f"本次子阶段处理结束；下一步：{next_action(state)}")


def command_run_continuous(args: argparse.Namespace) -> None:
    """Run an already authorized continuous stage in one process, reusing its model."""
    from asr_runtime import release_models
    resolved, root = resolve_input(args.input, args.database_root)
    media = choose_media(resolved)
    state = load_state(media)
    if state["execution"]["mode"] != "continuous":
        raise WorkflowError("run-continuous 不能越过 staged 模式的用户暂停点。")
    _, handoff_dir, _, _ = state_paths(media)
    try:
        with task_lock(handoff_dir, state["workflow_id"]):
            state = load_state(media)
            while state["stage1"]["status"] != "complete":
                run_next_chunk(state, media, root, requested_index=None,
                               device_arg=args.device, compute_arg=args.compute_type)
    finally:
        release_models()
    print(f"第一阶段连续处理完成；下一步：{next_action(state)}")


def command_approve_next(args: argparse.Namespace) -> None:
    """记录用户本次“继续”授权，只开放下一段。"""

    if not args.confirmed_by_user:
        raise WorkflowError("必须在用户明确说“继续”后添加 --confirmed-by-user。")
    resolved, root = resolve_input(args.input, args.database_root)
    media = choose_media(resolved)
    state = load_state(media)
    _, handoff_dir, _, _ = state_paths(media)
    with task_lock(handoff_dir, state["workflow_id"]):
        state = load_state(media)
        validate_source(state, media, root)
        if state["execution"]["mode"] != "staged" or state["stage1"]["status"] != "awaiting_continue":
            raise WorkflowError("当前状态不在等待下一子阶段确认。")
        target = state["stage1"]["gate"]["target_chunk"]
        state["stage1"]["gate"] = None
        state["stage1"]["status"] = "ready"
        state.setdefault("approvals", []).append({"kind": "continue_next_chunk", "target_chunk": target, "at": now_iso()})
        persist_state(state, media, root)
    print(f"已记录继续授权；仅开放子阶段 {target}。")


def command_merge(args: argparse.Namespace) -> None:
    """恢复场景下显式重试第一阶段合并。"""

    resolved, root = resolve_input(args.input, args.database_root)
    media = choose_media(resolved)
    state = load_state(media)
    _, handoff_dir, _, _ = state_paths(media)
    with task_lock(handoff_dir, state["workflow_id"]):
        state = load_state(media)
        validate_source(state, media, root)
        merge_stage1(state, media, root)
    print(f"第一阶段合并完成；下一步：{next_action(state)}")


def command_compact_stage1(args: argparse.Namespace) -> None:
    """显式重试第一阶段中间件收口，也可在用户授权后升级旧任务。"""

    if not args.confirmed_by_user:
        raise WorkflowError("收口会删除分段中间件，必须带 --confirmed-by-user。")
    resolved, root = resolve_input(args.input, args.database_root)
    media = choose_media(resolved)
    state = load_state(media)
    _, handoff_dir, _, _ = state_paths(media)
    with task_lock(handoff_dir, state["workflow_id"]):
        state = load_state(media)
        validate_source(state, media, root)
        verify_stage1_outputs(state, root)
        if not stage1_compaction_required(state):
            # 旧任务只在本次明确授权后升级；不会被脚本版本变更追溯删除。
            state["execution"]["compact_stage1_intermediates"] = True
            state["stage1"]["intermediate_cleanup"] = {
                "status": "pending",
                "scope": PARTS_DIRNAME,
                "enabled_from_legacy_at": now_iso(),
            }
            state["stage1"]["gate"] = None
            persist_state(state, media, root)
        compact_stage1_intermediates(state, media, root)
    print(f"第一阶段正式总稿保持不变；下一步：{next_action(state)}")


def command_stage2_start(args: argparse.Namespace) -> None:
    """记录第二阶段开始；分阶段模式要求新的用户确认。"""

    resolved, root = resolve_input(args.input, args.database_root)
    media = choose_media(resolved)
    state = load_state(media)
    _, handoff_dir, _, _ = state_paths(media)
    with task_lock(handoff_dir, state["workflow_id"]):
        state = load_state(media)
        validate_source(state, media, root)
        verify_stage1_outputs(state, root)
        if stage1_compaction_required(state) and stage1_compaction_status(state) != "complete":
            raise WorkflowError("第一阶段分段中间件尚未安全收口；请先重试 compact-stage1。")
        if state["execution"]["mode"] == "staged" and not args.confirmed_by_user:
            raise WorkflowError("分阶段模式必须在用户确认进入第二阶段后添加 --confirmed-by-user。")
        if state["stage2"]["status"] == "complete":
            raise WorkflowError("第二阶段已经完成。")
        state["stage2"]["status"] = "running"
        state["stage2"]["started_at"] = state["stage2"].get("started_at", now_iso())
        state["stage1"]["gate"] = None
        persist_state(state, media, root)
    print("第二阶段已登记为进行中；应连续完成校正、最终稿和哈希核对，并按已确认的入库要求归档最终 Markdown。")


def command_complete(args: argparse.Namespace) -> None:
    """验证第二阶段成果并安全清理第一阶段临时交接。

    本命令是建库流程的归档完成门；只做转写时无需调用。调用方必须
    先用安全复制命令创建成稿副本，再显式传入 ``--ingest-confirmed``；本命令验证
    源稿、重定位后的归档稿和实际依赖有效后才允许完成和清理。Word 视觉附件仍单独授权。
    """

    resolved, root = resolve_input(args.input, args.database_root)
    media = choose_media(resolved)
    state = load_state(media)
    task_dir, handoff_dir, _, _ = state_paths(media)
    corrected = resolve_artifact_path(args.corrected, task_dir / "02_校正逐字稿.md", root)
    final = resolve_artifact_path(args.final, media.with_suffix(".md"), root)
    coverage_arg = getattr(args, "coverage_report", None)
    coverage_report = resolve_artifact_path(
        coverage_arg,
        handoff_dir / COVERAGE_REPORT_FILENAME,
        root,
    )
    if coverage_report.parent.resolve() != handoff_dir.resolve() or coverage_report.is_symlink():
        raise WorkflowError("内容覆盖清单必须是当前任务 00_阶段交接中的普通文件。")
    ingest_confirmed = bool(getattr(args, "ingest_confirmed", False))
    cooked_arg = getattr(args, "cooked", None)
    visual_arg = getattr(args, "visual", None)
    cooked_visual_arg = getattr(args, "cooked_visual", None)
    if not ingest_confirmed:
        raise WorkflowError(
            "最终 Markdown 尚未确认自动备份；请先安全复制到Markdown，"
            "再添加 --ingest-confirmed 完成哈希验证。"
        )
    if cooked_visual_arg is not None and visual_arg is None:
        raise WorkflowError("--cooked-visual 只能与 --visual 一起使用。")
    config = load_config(root)
    cooked_dir = config_path(config, "cooked_dir", root)
    bundled_default = cooked_dir / final.stem / final.name
    flat_default = cooked_dir / final.name
    cooked = resolve_artifact_path(cooked_arg, flat_default if flat_default.exists() else bundled_default if bundled_default.exists() else flat_default, root)
    visual: Path | None = None
    cooked_visual: Path | None = None
    if visual_arg is not None:
        visual = resolve_artifact_path(visual_arg, task_dir / "03_视觉资料.docx", root)
        if visual.suffix.lower() != ".docx":
            raise WorkflowError("视觉资料必须是 DOCX 文件。")
        if cooked_visual_arg is not None:
            cooked_visual = resolve_artifact_path(
                cooked_visual_arg,
                cooked_dir / visual.name,
                root,
            )
            if cooked_visual.suffix.lower() != ".docx":
                raise WorkflowError("视觉资料成稿库副本必须是 DOCX 文件。")
    with task_lock(handoff_dir, state["workflow_id"]):
        state = load_state(media)
        validate_source(state, media, root)
        verify_stage1_outputs(state, root)
        if state["stage2"]["status"] not in {"running", "complete"}:
            raise WorkflowError("第二阶段尚未登记为进行中；请先执行 stage2-start。")
        for label, path in (("校正逐字稿", corrected), ("最终研究记录", final)):
            if not path.is_file() or path.stat().st_size == 0:
                raise WorkflowError(f"{label}缺失或为空：{path}")
        fidelity_record = run_fidelity_audit(corrected, final, coverage_report)
        register_owned(state, coverage_report, root)
        fidelity_record["report"] = relative_to_database(coverage_report, root)
        fidelity_record["report_sha256"] = sha256_file(coverage_report)
        final_hash = sha256_file(final)
        output_paths = [corrected, final]
        if not cooked.is_file() or cooked.stat().st_size == 0:
            raise WorkflowError(f"成稿库副本缺失或为空：{cooked}")
        cooked_hash = sha256_file(cooked)
        archive_record = None
        from project_io import ProjectError
        try:
            if cooked.parent.resolve() == cooked_dir.resolve():
                from flat_archive import verify_flat
                archive_record = verify_flat(final, cooked, database_root=root)
                temporary_roots = (task_dir / HANDOFF_DIRNAME, task_dir / PARTS_DIRNAME)
                for dependency in archive_record["dependencies"]:
                    linked = Path(dependency["path"]).resolve()
                    if any(linked.is_relative_to(folder.resolve()) for folder in temporary_roots):
                        raise ProjectError("成稿引用待清理目录中的文件；请先将附件保存到源项目永久位置：" + str(linked))
            else:
                if final_hash != cooked_hash:
                    raise ProjectError("源侧最终稿与旧成稿副本 SHA256 不一致。")
                if "<!-- mdlib:corrected-fulltext:begin -->" in final.read_text(encoding="utf-8-sig"):
                    from archive_bundle import verify_bundle
                    archive_record = verify_bundle(final, cooked.parent)
        except ProjectError as error:
            raise WorkflowError(f"归档校验失败：{error}；保留交接，不清理。") from error
        output_paths.append(cooked)
        ingestion_record: dict[str, Any] = {
            "status": "verified",
            "confirmed": True,
            "confirmed_at": now_iso(),
            "authorization_basis": "standing_all_final_markdown_policy",
            "final_cooked_sha256": cooked_hash,
            "source_final_sha256": final_hash,
            "archive": archive_record,
        }
        visual_record: dict[str, Any] | None = None
        if visual is not None:
            if not visual.is_file() or visual.stat().st_size == 0:
                raise WorkflowError(f"视觉资料缺失或为空：{visual}")
            visual_hash = sha256_file(visual)
            output_paths.append(visual)
            visual_record = {
                "selected": True,
                "source": relative_to_database(visual, root),
                "sha256": visual_hash,
                "verified_at": now_iso(),
            }
            if cooked_visual is not None:
                if not cooked_visual.is_file() or cooked_visual.stat().st_size == 0:
                    raise WorkflowError(f"视觉资料成稿库副本缺失或为空：{cooked_visual}")
                cooked_visual_hash = sha256_file(cooked_visual)
                if visual_hash != cooked_visual_hash:
                    raise WorkflowError("视觉资料与成稿库 DOCX 副本 SHA256 不一致；保留交接，不清理。")
                output_paths.append(cooked_visual)
                visual_record["cooked"] = relative_to_database(cooked_visual, root)
                visual_record["cooked_sha256"] = cooked_visual_hash
        outputs = [file_record(path, root) for path in output_paths]
        state["stage2"].update(
            {
                "status": "complete",
                "completed_at": state["stage2"].get("completed_at", now_iso()),
                "outputs": outputs,
                "ingestion": ingestion_record,
                "source_fidelity": fidelity_record,
                "cleanup_status": "pending",
            }
        )
        state["stage2"]["final_cooked_sha256"] = cooked_hash
        if visual_record is not None:
            state["stage2"]["visual"] = visual_record
        persist_state(state, media, root)
        unknown = find_unknown_temporary_entries(state, media, root)
        if unknown:
            state["stage2"]["cleanup_status"] = "blocked_unknown_files"
            state.setdefault("errors", []).append(
                {"at": now_iso(), "message": "临时目录含未登记文件，未清理：" + "、".join(unknown)}
            )
            persist_state(state, media, root)
            raise WorkflowError("第二阶段成果已验证，但临时目录含未登记文件，交接未删除：" + "、".join(unknown))
        state["stage2"]["cleanup_status"] = "running"
        persist_state(state, media, root)
        cleanup_temporary_state(state, media, root)
    print("第二阶段成果及自动备份副本哈希已验证；第一阶段交接与临时分段已安全删除。")


def command_status(args: argparse.Namespace) -> None:
    """输出可续跑状态，不改变任何文件。"""

    resolved, root = resolve_input(args.input, args.database_root)
    media = choose_media(resolved)
    state = load_state(media)
    summary = {
        "workflow_id": state["workflow_id"],
        "source": state["source"]["path"],
        "stage1_status": state["stage1"]["status"],
        "stage2_status": state["stage2"]["status"],
        "completed_chunks": [chunk["index"] for chunk in state["stage1"]["chunks"] if chunk["status"] == "complete"],
        "total_chunks": len(state["stage1"]["chunks"]),
        "previous_chunk_context_enabled": previous_context_policy(state)["enabled"],
        "stage1_intermediate_cleanup_status": stage1_compaction_status(state),
        "next_action": next_action(state),
        "state_file": relative_to_database(state_paths(media)[3], root),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"第一阶段：{summary['stage1_status']}；第二阶段：{summary['stage2_status']}")
        print(f"已完成子阶段：{summary['completed_chunks']} / {summary['total_chunks']}")
        print(f"第一阶段中间件收口：{summary['stage1_intermediate_cleanup_status']}")
        print(f"下一步：{summary['next_action']}")


def build_parser() -> argparse.ArgumentParser:
    """构建命令行接口。"""

    parser = argparse.ArgumentParser(
        description="Markdown 资料库：材料预检、逐字稿导入、媒体分段、续跑与安全清理。"
    )
    parser.add_argument("--database-root", type=Path, help="“Markdown资料库”根目录")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="只读清点同项目材料、探测时长并给出用户处理选项"
    )
    inspect_parser.add_argument("input", type=Path)
    inspect_parser.add_argument("--target-minutes", type=int, default=DEFAULT_TARGET_MINUTES)
    inspect_parser.add_argument("--overlap-seconds", type=int, default=DEFAULT_OVERLAP_SECONDS)
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.set_defaults(handler=command_inspect)

    import_parser = subparsers.add_parser(
        "import-transcript", help="用户选择后安全导入一个外部 TXT/MD/SRT/VTT 逐字稿"
    )
    import_parser.add_argument("input", type=Path, help="明确逐字稿文件，或只有一个候选的目录")
    import_parser.add_argument(
        "--task-dir",
        type=Path,
        help="规范化成果目录；默认使用外部逐字稿所在目录",
    )
    import_parser.add_argument("--source-software", help="下载或转写软件名称，写入 manifest")
    import_parser.add_argument("--confirmed-by-user", action="store_true", required=True)
    import_parser.add_argument("--json", action="store_true")
    import_parser.set_defaults(handler=command_import_transcript)

    init_parser = subparsers.add_parser("init", help="用户确认后初始化第一阶段计划")
    init_parser.add_argument("input", type=Path)
    init_parser.add_argument("--mode", choices=("continuous", "staged"), default="continuous")
    init_parser.add_argument("--chunk-count", type=int)
    init_parser.add_argument("--target-minutes", type=int, default=DEFAULT_TARGET_MINUTES)
    init_parser.add_argument("--overlap-seconds", type=int, default=DEFAULT_OVERLAP_SECONDS)
    init_parser.add_argument("--confirmed-by-user", action="store_true")
    init_parser.add_argument("--force-audio", action="store_true")
    init_parser.add_argument("--device", choices=("auto", "cuda", "cpu"))
    init_parser.add_argument("--compute-type")
    init_parser.add_argument("--no-previous-context", action="store_true", help="关闭后一段读取前一段尾部语境")
    init_parser.add_argument("--previous-context-segments", type=int, default=DEFAULT_CONTEXT_SEGMENTS)
    init_parser.add_argument("--previous-context-chars", type=int, default=DEFAULT_CONTEXT_CHARS)
    init_parser.add_argument(
        "--keep-stage1-intermediates",
        action="store_true",
        help="第一阶段完成后仍保留分段 WAV 和分段稿",
    )
    init_parser.set_defaults(handler=command_init)

    chunk_parser = subparsers.add_parser("run-chunk", help="恰好执行一个第一阶段子阶段")
    chunk_parser.add_argument("input", type=Path)
    chunk_parser.add_argument("--index", type=int)
    chunk_parser.add_argument("--device", choices=("auto", "cuda", "cpu"))
    chunk_parser.add_argument("--compute-type")
    chunk_parser.set_defaults(handler=command_run_chunk)

    continuous_parser = subparsers.add_parser("run-continuous", help="在一个进程中续跑已授权 continuous 第一阶段；保留逐段检查点并复用模型")
    continuous_parser.add_argument("input", type=Path)
    continuous_parser.add_argument("--device", choices=("auto", "cuda", "cpu"))
    continuous_parser.add_argument("--compute-type")
    continuous_parser.set_defaults(handler=command_run_continuous)

    approve_parser = subparsers.add_parser("approve-next", help="记录用户对下一子阶段的继续授权")
    approve_parser.add_argument("input", type=Path)
    approve_parser.add_argument("--confirmed-by-user", action="store_true", required=True)
    approve_parser.set_defaults(handler=command_approve_next)

    merge_parser = subparsers.add_parser("merge", help="在恢复场景下重试第一阶段合并")
    merge_parser.add_argument("input", type=Path)
    merge_parser.set_defaults(handler=command_merge)

    compact_parser = subparsers.add_parser(
        "compact-stage1", help="验证正式总稿后重试清理第一阶段分段中间件"
    )
    compact_parser.add_argument("input", type=Path)
    compact_parser.add_argument("--confirmed-by-user", action="store_true", required=True)
    compact_parser.set_defaults(handler=command_compact_stage1)

    stage2_parser = subparsers.add_parser("stage2-start", help="确认并登记进入第二阶段")
    stage2_parser.add_argument("input", type=Path)
    stage2_parser.add_argument("--confirmed-by-user", action="store_true")
    stage2_parser.set_defaults(handler=command_stage2_start)

    complete_parser = subparsers.add_parser("complete", help="验证第二阶段并清理临时交接")
    complete_parser.add_argument("input", type=Path)
    complete_parser.add_argument("--corrected", type=Path)
    complete_parser.add_argument("--final", type=Path)
    complete_parser.add_argument(
        "--coverage-report",
        type=Path,
        help="原材料覆盖清单；默认 00_阶段交接/内容覆盖清单.json，完成时强制校验并安全清理",
    )
    complete_parser.add_argument("--cooked", type=Path)
    complete_parser.add_argument(
        "--ingest-confirmed",
        action="store_true",
        help="最终 Markdown 已按已确认的入库要求归档到成稿库时添加；完成阶段强制要求",
    )
    complete_parser.add_argument(
        "--visual",
        type=Path,
        help="仅在用户选择视觉流程时传入 03_视觉资料.docx",
    )
    complete_parser.add_argument(
        "--cooked-visual",
        type=Path,
        help="视觉 DOCX 的成稿库副本；省略时默认为成稿库中的同名文件",
    )
    complete_parser.set_defaults(handler=command_complete)

    status_parser = subparsers.add_parser("status", help="只读查看当前续跑入口")
    status_parser.add_argument("input", type=Path)
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(handler=command_status)
    return parser


def main() -> None:
    """命令行入口；把工作流错误转成简洁的非零退出。"""

    # 固定环境可能继承旧版 Windows 中文代码页；统一 UTF-8，避免 JSON 路径乱码。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except WorkflowError as error:
        parser.exit(2, f"错误：{error}\n")


if __name__ == "__main__":
    main()
