"""视频视觉资料候选提取、人工复核清单与 Word 附件生成器。

这个模块是“Markdown 资料库”媒体流程中的视觉分支。它只负责本地文件，不负责下载
视频，也不会逐帧调用远程视觉模型。完整流程被拆成四个显式命令：

``inspect``
    只读探测视频时长、已有 manifest 和（可选）场景变化候选数量。该命令不会
    创建目录、截图或改写 manifest，因此可在用户决定是否支付视觉处理成本前
    安全运行。

``extract``
    先使用 ffmpeg 的场景变化过滤器找出候选时间点，再逐个导出候选截图；之后
    使用本地感知哈希去重，并可调用本地 Tesseract OCR。该命令必须携带
    ``--confirmed-by-user``。候选数量超过上限时，命令会在写入任何成果前停止，
    报告范围，并要求用户缩小时间范围或用检测到的精确数量再次确认。

``build-docx``
    只接受所有条目都已人工复核为 ``review_status=complete`` 的 manifest。
    Word 中的截图使用内联图片嵌入 OOXML 包，因此生成物是自包含文件。该命令
    同样必须携带 ``--confirmed-by-user``。

``embed-markdown``
    把已复核截图复制到最终 Markdown 同目录的 ``<Markdown stem>_assets``，使用
    “视频名 + 时间戳 + 画面主题”命名，并在受标记保护的章节中写入相对链接。
    重跑只替换该受管章节，不覆盖正文的其他人工内容。

manifest 是可续跑的机器事实源。截图导出后立即记录文件哈希；中断重跑时只
复用哈希一致的已完成候选。临时截图的清理仅形成白名单计划，本脚本绝不自动
递归删除目录，也不会删除 manifest 未登记的未知文件。

依赖说明：

* 场景检测与截图：优先使用 ``VISUAL_FFMPEG`` 指定的 ffmpeg，其次使用
  imageio-ffmpeg，最后使用 PATH 中的 ffmpeg；
* 图像分析：Pillow；
* Word：python-docx；
* OCR：可选的 pytesseract/Tesseract。缺少 OCR 时仍会生成待人工复核条目，
  并把限制写入 ``uncertainties``，不会伪造识别结果。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


# 发布版本从 Skill 根目录 VERSION 读取，避免脚本、文档和发布包各自维护一份
# 会漂移的版本号。视觉 manifest 结构本身仍由 SCHEMA_VERSION 独立控制。
WORKFLOW_VERSION = (
    Path(__file__).resolve().parents[1] / "VERSION"
).read_text(encoding="utf-8-sig").strip()
SCHEMA_VERSION = 1
DEFAULT_SCENE_THRESHOLD = 0.28
DEFAULT_MAX_CANDIDATES = 80
DEFAULT_DEDUP_HAMMING = 5
DEFAULT_MIN_INFORMATION_SCORE = 0.015
DEFAULT_MANIFEST_NAME = "03_视觉资料.manifest.json"
DEFAULT_DOCX_NAME = "03_视觉资料.docx"
TEMP_SCREENSHOT_DIR = ".visual-candidates"
FINAL_SCREENSHOT_DIR = "03_视觉资料_截图"
MARKDOWN_VISUALS_START = "<!-- mdlib-visuals:start -->"
MARKDOWN_VISUALS_END = "<!-- mdlib-visuals:end -->"
ALLOWED_REVIEW_STATUS = {"pending", "complete"}
# 这些字段不是机器提取结果，而是结合截图、逐字稿、参考资料后的人工／流程复核
# 结论。条目改成 complete 前必须逐项填写；没有相关内容时也要明确写“无”，不能
# 用空字符串掩盖“尚未复核”和“确认没有”之间的差别。
REVIEW_COMPLETION_FIELDS = {
    "corrected_text": "校正后的画面文字",
    "related_transcript": "关联逐字稿",
    "related_theme": "关联主题",
    "reference_findings": "参考资料核验信息",
    "description": "可见画面说明",
    "inference": "整理者推断",
}


class VisualWorkflowError(RuntimeError):
    """表示应向用户解释的工作流问题，而不是未处理的程序崩溃。"""


class ConfirmationRequired(VisualWorkflowError):
    """写操作缺少用户明确确认时抛出。"""


class CandidateOverflowError(VisualWorkflowError):
    """场景候选过多、需要用户重新确认处理范围时抛出。"""


def now_iso() -> str:
    """返回带本地时区的秒级 ISO 时间，便于跨会话追踪。"""

    return datetime.now().astimezone().isoformat(timespec="seconds")


def require_confirmation(confirmed: bool, operation: str) -> None:
    """在任何成果写入前验证用户确认关卡。

    ``inspect`` 不调用本函数，因为它是严格只读命令；``extract`` 和
    ``build-docx`` 必须在创建目录、临时文件或输出文件前调用。
    """

    if not confirmed:
        raise ConfirmationRequired(
            f"{operation} 会写入成果；必须先取得用户确认并添加 --confirmed-by-user。"
        )


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """在目标同目录原子提交字节，避免中断留下半份正式成果。"""

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


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """以 UTF-8、稳定缩进原子写入 manifest。"""

    atomic_write_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA256，避免把大媒体或大截图整体读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_relative_path(path: Path, parent: Path) -> str:
    """把 ``path`` 转为 ``parent`` 下的 POSIX 相对路径，并拒绝越界。"""

    resolved = path.resolve()
    try:
        relative = resolved.relative_to(parent.resolve())
    except ValueError as error:
        raise VisualWorkflowError(f"路径越出视觉资料目录：{resolved}") from error
    return relative.as_posix()


def resolve_manifest_path(relative: str, manifest_dir: Path) -> Path:
    """安全解析 manifest 内的相对路径，防止 ``..`` 指向目录外。"""

    candidate = (manifest_dir / Path(relative)).resolve()
    try:
        candidate.relative_to(manifest_dir.resolve())
    except ValueError as error:
        raise VisualWorkflowError(f"manifest 中的路径越界：{relative}") from error
    return candidate


def format_timestamp(seconds: float) -> str:
    """把秒数格式化为至少两位小时的 ``HH:MM:SS.mmm``。"""

    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{millis:03d}"


def find_ffmpeg() -> Path:
    """定位可用 ffmpeg，不在系统中安装或下载任何依赖。"""

    configured = os.environ.get("VISUAL_FFMPEG")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise VisualWorkflowError(f"VISUAL_FFMPEG 指向的文件不存在：{candidate}")

    try:
        import imageio_ffmpeg  # type: ignore

        candidate = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
        if candidate.is_file():
            return candidate
    except (ImportError, RuntimeError, OSError):
        pass

    found = shutil.which("ffmpeg")
    if found:
        return Path(found).resolve()
    raise VisualWorkflowError(
        "未找到本地 ffmpeg。请在固定媒体环境中运行，或设置 VISUAL_FFMPEG。"
    )


def _run_process(command: Sequence[str], *, purpose: str) -> subprocess.CompletedProcess[str]:
    """统一执行本地媒体命令，并把底层错误转成可读工作流错误。"""

    try:
        completed = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise VisualWorkflowError(f"无法启动本地媒体工具（{purpose}）：{error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-2000:]
        raise VisualWorkflowError(f"本地媒体工具执行失败（{purpose}）：{detail}")
    return completed


def probe_duration_seconds(media: Path, ffmpeg: Path | None = None) -> float:
    """只读探测视频时长。

    优先使用 PyAV 的结构化元数据；若当前环境没有 PyAV，则读取 ffmpeg 的
    ``Duration`` 信息。后者调用返回非零是 ffmpeg 只探测输入时的正常行为，
    因此这里单独解析 stderr，而不使用 ``_run_process`` 的成功码约束。
    """

    try:
        import av  # type: ignore

        with av.open(str(media)) as container:
            durations: list[float] = []
            for stream in container.streams.video:
                if stream.duration is not None and stream.time_base is not None:
                    durations.append(float(stream.duration * stream.time_base))
            if durations:
                duration = max(durations)
            elif container.duration is not None:
                duration = float(container.duration) / 1_000_000
            else:
                duration = 0.0
        if duration > 0:
            return duration
    except (ImportError, OSError, ValueError):
        pass
    except Exception as error:
        raise VisualWorkflowError(f"无法读取视频时长：{media.name}（{error}）") from error

    ffmpeg = ffmpeg or find_ffmpeg()
    completed = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(media)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr)
    if not match:
        raise VisualWorkflowError(f"无法从本地媒体元数据读取视频时长：{media.name}")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0:
        raise VisualWorkflowError(f"视频时长无效：{duration}")
    return duration


def validate_time_range(
    duration: float, start_seconds: float, end_seconds: float | None
) -> tuple[float, float]:
    """规范化用户选择的时间范围，并拒绝空范围或越界范围。"""

    start = float(start_seconds)
    end = duration if end_seconds is None else float(end_seconds)
    if start < 0 or start >= duration:
        raise VisualWorkflowError(f"开始时间必须位于 0 到 {duration:.3f} 秒之间。")
    if end <= start or end > duration + 0.001:
        raise VisualWorkflowError(f"结束时间必须大于开始时间且不超过 {duration:.3f} 秒。")
    return start, min(end, duration)


def detect_scene_timestamps(
    media: Path,
    ffmpeg: Path,
    *,
    threshold: float = DEFAULT_SCENE_THRESHOLD,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
) -> list[float]:
    """使用 ffmpeg 场景变化过滤器只读扫描候选时间点。

    ffmpeg 的 ``scene`` 分数比较相邻解码帧，本函数仅把超过阈值的帧时间写到
    stderr 的 ``showinfo``，输出端使用 null muxer，不创建截图。扫描范围的首帧
    总会被加入，以免恰好在一张长时间静止的幻灯片中间开始时漏掉该页。
    """

    if not 0 < threshold < 1:
        raise VisualWorkflowError("scene-threshold 必须大于 0 且小于 1。")

    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
    ]
    if start_seconds > 0:
        command.extend(["-ss", f"{start_seconds:.3f}"])
    command.extend(["-i", str(media)])
    if end_seconds is not None:
        command.extend(["-t", f"{end_seconds - start_seconds:.3f}"])

    # 逗号需要对 ffmpeg 表达式转义；参数直接传给子进程，不经过 shell。
    video_filter = f"select=eq(n\\,0)+gt(scene\\,{threshold:.6f}),showinfo"
    null_device = "NUL" if os.name == "nt" else "/dev/null"
    command.extend(
        ["-an", "-vf", video_filter, "-fps_mode", "vfr", "-f", "null", null_device]
    )
    completed = _run_process(command, purpose="场景变化只读扫描")

    raw = [float(value) for value in re.findall(r"pts_time:([0-9.eE+-]+)", completed.stderr)]
    if not raw:
        return [round(start_seconds, 3)]

    # 输入侧 -ss 在不同容器／ffmpeg 版本中可能保留原始 PTS，也可能把 PTS 归零。
    # 若第一个时间明显小于选择范围起点，则把所有值平移回全局视频时间。
    if start_seconds > 0 and raw[0] < start_seconds - 0.25:
        raw = [value + start_seconds for value in raw]

    upper = math.inf if end_seconds is None else end_seconds + 0.001
    normalized: list[float] = []
    for value in raw:
        value = max(start_seconds, value)
        if value > upper:
            continue
        rounded = round(value, 3)
        if not normalized or abs(normalized[-1] - rounded) > 0.020:
            normalized.append(rounded)
    return normalized or [round(start_seconds, 3)]


def candidate_scope_report(
    timestamps: Sequence[float], *, max_candidates: int, start: float, end: float
) -> dict[str, Any]:
    """构造候选过多时供对话层展示的稳定结构化报告。"""

    return {
        "detected_candidate_count": len(timestamps),
        "configured_max_candidates": max_candidates,
        "requested_range": {
            "start_seconds": start,
            "end_seconds": end,
            "start_timestamp": format_timestamp(start),
            "end_timestamp": format_timestamp(end),
        },
        "first_candidate": format_timestamp(timestamps[0]) if timestamps else None,
        "last_candidate": format_timestamp(timestamps[-1]) if timestamps else None,
        "required_action": (
            "请让用户确认更小的 --start-seconds/--end-seconds 范围；或在明确接受该"
            "工作量后，用 --confirmed-candidate-count 填入本次检测到的精确候选数重跑。"
        ),
    }


def enforce_candidate_limit(
    timestamps: Sequence[float],
    *,
    max_candidates: int,
    confirmed_candidate_count: int | None,
    start: float,
    end: float,
) -> None:
    """候选过多时在任何写入前停止，除非用户确认了精确检测数量。"""

    if max_candidates <= 0:
        raise VisualWorkflowError("max-candidates 必须大于 0。")
    detected = len(timestamps)
    if detected <= max_candidates:
        return
    if confirmed_candidate_count == detected:
        return
    report = candidate_scope_report(
        timestamps, max_candidates=max_candidates, start=start, end=end
    )
    raise CandidateOverflowError(
        "候选画面过多，尚未确认处理范围；已在写入前停止。\n"
        + json.dumps(report, ensure_ascii=False, indent=2)
    )


def source_identity(media: Path, duration: float) -> dict[str, Any]:
    """建立媒体来源指纹，防止旧 manifest 静默套到被替换的视频。"""

    stat = media.stat()
    return {
        "path": str(media.resolve()),
        "name": media.name,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(media),
        "duration_seconds": round(duration, 3),
    }


def reference_records(values: Iterable[str]) -> list[dict[str, Any]]:
    """把用户提供的参考资料变成可追溯记录，不读取未指定的目录。"""

    records: list[dict[str, Any]] = []
    for value in values:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise VisualWorkflowError(f"参考资料不存在或不是普通文件：{path}")
        records.append(
            {
                "display": path.name,
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def extract_frame(
    media: Path, ffmpeg: Path, timestamp_seconds: float, destination: Path
) -> None:
    """在给定全局时间点导出一张 PNG，并以原子替换提交。

    最长边限制为 1600 像素，既保留 PPT/OCR 可读性，也避免把 4K 帧原样塞进
    Word。输出路径必须由调用方限定在本任务目录内。
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.stem}.tmp.{os.getpid()}.{uuid.uuid4().hex}.png"
    )
    try:
        command = [
            str(ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp_seconds:.3f}",
            "-i",
            str(media),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(1600,iw)':-2",
            "-y",
            str(temporary),
        ]
        _run_process(command, purpose=f"导出 {format_timestamp(timestamp_seconds)} 截图")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise VisualWorkflowError(
                f"截图导出没有生成有效文件：{format_timestamp(timestamp_seconds)}"
            )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def perceptual_hash(path: Path, *, hash_size: int = 8) -> str:
    """计算方向差分感知哈希（dHash），用于识别近似重复幻灯片。"""

    try:
        from PIL import Image
    except ImportError as error:
        raise VisualWorkflowError("缺少 Pillow，无法执行截图感知去重。") from error
    with Image.open(path) as image:
        gray = image.convert("L").resize((hash_size + 1, hash_size))
        # Pillow 14 将移除 getdata；兼容旧版本时再回退，避免新版本弃用警告。
        if hasattr(gray, "get_flattened_data"):
            pixels = list(gray.get_flattened_data())
        else:  # pragma: no cover - 仅旧版 Pillow 使用。
            pixels = list(gray.getdata())
    bits = []
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for column in range(hash_size):
            bits.append(pixels[offset + column] > pixels[offset + column + 1])
    value = sum((1 << index) for index, enabled in enumerate(bits) if enabled)
    return f"{value:0{hash_size * hash_size // 4}x}"


def hamming_distance(first_hash: str, second_hash: str) -> int:
    """计算两个十六进制感知哈希的汉明距离。"""

    if len(first_hash) != len(second_hash):
        raise ValueError("感知哈希长度不一致。")
    return (int(first_hash, 16) ^ int(second_hash, 16)).bit_count()


def image_information_metrics(path: Path) -> dict[str, float | str]:
    """使用本地图像统计估计画面信息密度并给出宽泛类型提示。

    该启发式只用来剔除近乎纯色的转场帧，不声称能可靠识别 PPT、图表或地图。
    所有保留画面的 ``visual_kind`` 仍需人工复核。
    """

    try:
        from PIL import Image, ImageFilter, ImageStat
    except ImportError as error:
        raise VisualWorkflowError("缺少 Pillow，无法评估截图信息密度。") from error

    with Image.open(path) as image:
        gray = image.convert("L").resize((320, 180))
        stat = ImageStat.Stat(gray)
        contrast = min(1.0, float(stat.stddev[0]) / 96.0)
        entropy = min(1.0, float(gray.entropy()) / 8.0)
        edge = gray.filter(ImageFilter.FIND_EDGES)
        edge_density = min(1.0, float(ImageStat.Stat(edge).mean[0]) / 64.0)
        if hasattr(gray, "get_flattened_data"):
            gray_values = gray.get_flattened_data()
        else:  # pragma: no cover - 仅旧版 Pillow 使用。
            gray_values = gray.getdata()
        bright_ratio = sum(1 for value in gray_values if value >= 210) / (320 * 180)

    score = round(0.35 * contrast + 0.35 * entropy + 0.30 * edge_density, 6)
    if bright_ratio >= 0.45 and edge_density >= 0.05:
        hint = "可能为PPT、表格或文件截图，待人工复核"
    elif entropy >= 0.55:
        hint = "可能为图表、地图或信息图片，待人工复核"
    else:
        hint = "场景变化候选，待人工复核"
    return {
        "information_score": score,
        "contrast": round(contrast, 6),
        "entropy": round(entropy, 6),
        "edge_density": round(edge_density, 6),
        "bright_ratio": round(bright_ratio, 6),
        "visual_kind_hint": hint,
    }


def deduplicate_image_paths(
    paths: Sequence[Path], *, max_hamming_distance: int = DEFAULT_DEDUP_HAMMING
) -> tuple[list[Path], dict[Path, Path]]:
    """按输入顺序保留首张近似图，返回唯一图片及“重复图→代表图”映射。

    该纯逻辑接口也供单元测试和维护工具直接复用。感知哈希适合相同幻灯片的
    微小压缩／字幕变化；阈值越大越容易误合并，默认值有意保持保守。
    """

    if max_hamming_distance < 0:
        raise ValueError("max_hamming_distance 不得为负。")
    unique: list[Path] = []
    duplicates: dict[Path, Path] = {}
    hashes: dict[Path, str] = {}
    for raw_path in paths:
        path = Path(raw_path)
        current_hash = perceptual_hash(path)
        representative: Path | None = None
        for kept in unique:
            if hamming_distance(current_hash, hashes[kept]) <= max_hamming_distance:
                representative = kept
                break
        hashes[path] = current_hash
        if representative is None:
            unique.append(path)
        else:
            duplicates[path] = representative
    return unique, duplicates


def run_local_ocr(path: Path, *, language: str) -> tuple[str, list[str], str]:
    """尝试本地 OCR；不可用时如实返回空文本和限制说明。"""

    try:
        import pytesseract  # type: ignore
        from PIL import Image

        with Image.open(path) as image:
            text = pytesseract.image_to_string(image, lang=language).strip()
        uncertainties = [] if text else ["本地 OCR 未识别出文字；需要结合截图人工检查。"]
        return text, uncertainties, f"pytesseract:{language}"
    except ImportError:
        return "", ["当前环境未安装本地 OCR 依赖；OCR 原文待人工补录。"], "unavailable"
    except Exception as error:
        return "", [f"本地 OCR 失败：{error}；OCR 原文待人工补录。"], "failed"


def _manifest_signature(
    *,
    threshold: float,
    start: float,
    end: float,
    dedup_hamming: int,
    min_information_score: float,
    keyframe_retention: str,
    timestamps: Sequence[float],
) -> dict[str, Any]:
    """生成用于续跑一致性校验的提取参数签名。"""

    return {
        "scene_threshold": round(threshold, 6),
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "dedup_hamming_distance": dedup_hamming,
        "min_information_score": round(min_information_score, 6),
        "keyframe_retention": keyframe_retention,
        "candidate_timestamps": [round(value, 3) for value in timestamps],
    }


def _new_manifest(
    *,
    source: dict[str, Any],
    signature: dict[str, Any],
    max_candidates: int,
    confirmed_candidate_count: int | None,
    references: list[dict[str, Any]],
    keyframe_retention: str,
    manifest_dir: Path,
) -> dict[str, Any]:
    """建立尚未写出任何截图的可续跑 manifest 骨架。"""

    candidates: list[dict[str, Any]] = []
    temp_dir = manifest_dir / TEMP_SCREENSHOT_DIR
    for index, timestamp in enumerate(signature["candidate_timestamps"], start=1):
        path = temp_dir / f"candidate-{index:04d}-{round(timestamp * 1000):012d}.png"
        candidates.append(
            {
                "candidate_index": index,
                "timestamp_seconds": timestamp,
                "timestamp": format_timestamp(timestamp),
                "temporary_path": safe_relative_path(path, manifest_dir),
                "status": "pending_extract",
                "sha256": None,
                "perceptual_hash": None,
                "information_metrics": None,
                "duplicate_of_candidate": None,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_version": WORKFLOW_VERSION,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "source": source,
        "extraction": {
            "status": "extracting",
            "signature": signature,
            "detected_candidate_count": len(candidates),
            "max_candidates": max_candidates,
            "confirmed_candidate_count": confirmed_candidate_count,
            "selection_method": "ffmpeg_scene_change_then_local_information_filter_and_dhash",
            "remote_model_calls": 0,
        },
        "reference_materials": references,
        "candidate_frames": candidates,
        "items": [],
        "cleanup_plan": {
            "default_policy": "在 DOCX 渲染验证成功后清理已登记的临时候选截图",
            "automatic_deletion": False,
            "status": "planned",
            "keyframe_retention": keyframe_retention,
            "owned_temporary_files": [item["temporary_path"] for item in candidates],
            "permanent_files": [],
            "post_verification_files": [],
            "unknown_file_policy": "发现未登记文件时停止并报告，绝不递归强删",
        },
        "document": None,
    }


def _validate_resume_manifest(
    manifest: dict[str, Any], source: dict[str, Any], signature: dict[str, Any]
) -> None:
    """确认现有 manifest 可安全续跑，不覆盖不同来源或不同参数任务。"""

    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise VisualWorkflowError("现有视觉 manifest 模式版本不受支持。")
    existing_source = manifest.get("source")
    if not isinstance(existing_source, dict):
        raise VisualWorkflowError("现有视觉 manifest 缺少 source。")
    for key in ("size_bytes", "mtime_ns", "sha256"):
        if existing_source.get(key) != source.get(key):
            raise VisualWorkflowError(
                f"视频来源已变化（{key} 不一致）；拒绝覆盖旧视觉 manifest。"
            )
    extraction = manifest.get("extraction")
    if not isinstance(extraction, dict) or extraction.get("signature") != signature:
        raise VisualWorkflowError(
            "现有视觉 manifest 的时间范围或场景／去重参数不同；请使用新的输出目录。"
        )


def _copy_atomic(source: Path, destination: Path) -> None:
    """把已验证候选截图原子复制为永久关键帧。"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    try:
        with source.open("rb") as source_handle, temporary.open("wb") as output_handle:
            shutil.copyfileobj(source_handle, output_handle, 1024 * 1024)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _finalize_items(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    ocr_language: str,
    skip_ocr: bool,
) -> None:
    """对已导出的候选做信息过滤、感知去重、OCR 并建立人工复核条目。"""

    manifest_dir = manifest_path.parent
    signature = manifest["extraction"]["signature"]
    dedup_limit = int(signature["dedup_hamming_distance"])
    min_score = float(signature["min_information_score"])
    kept: list[dict[str, Any]] = []

    for candidate in manifest["candidate_frames"]:
        screenshot = resolve_manifest_path(candidate["temporary_path"], manifest_dir)
        if candidate.get("status") != "extracted" or not screenshot.is_file():
            raise VisualWorkflowError(
                f"候选截图尚未完整提交：{candidate.get('temporary_path')}"
            )
        actual_hash = sha256_file(screenshot)
        if actual_hash != candidate.get("sha256"):
            raise VisualWorkflowError(
                f"候选截图哈希不一致，拒绝静默覆盖：{candidate['temporary_path']}"
            )
        phash = candidate.get("perceptual_hash") or perceptual_hash(screenshot)
        metrics = candidate.get("information_metrics") or image_information_metrics(screenshot)
        candidate["perceptual_hash"] = phash
        candidate["information_metrics"] = metrics

        if float(metrics["information_score"]) < min_score:
            candidate["status"] = "low_information"
            continue

        representative: dict[str, Any] | None = None
        for prior in kept:
            if hamming_distance(phash, prior["perceptual_hash"]) <= dedup_limit:
                representative = prior
                break
        if representative is not None:
            candidate["status"] = "duplicate"
            candidate["duplicate_of_candidate"] = representative["candidate_index"]
            continue
        candidate["status"] = "selected"
        kept.append(candidate)

    reference_names = [record["display"] for record in manifest["reference_materials"]]
    final_dir = manifest_dir / FINAL_SCREENSHOT_DIR
    items: list[dict[str, Any]] = []
    for item_index, candidate in enumerate(kept, start=1):
        temporary = resolve_manifest_path(candidate["temporary_path"], manifest_dir)
        final_path = final_dir / f"frame-{item_index:04d}-{candidate['timestamp'].replace(':', '')}.png"
        if final_path.is_file():
            if sha256_file(final_path) != candidate["sha256"]:
                raise VisualWorkflowError(f"永久截图已存在但哈希不同：{final_path}")
        else:
            _copy_atomic(temporary, final_path)

        if skip_ocr:
            ocr_text = ""
            ocr_uncertainties = ["本次按用户选择跳过 OCR；OCR 原文待人工补录。"]
            ocr_engine = "skipped_by_user"
        else:
            ocr_text, ocr_uncertainties, ocr_engine = run_local_ocr(
                final_path, language=ocr_language
            )

        items.append(
            {
                "item_id": f"visual-{item_index:04d}",
                "candidate_index": candidate["candidate_index"],
                "timestamp_seconds": candidate["timestamp_seconds"],
                "timestamp": candidate["timestamp"],
                "screenshot_path": safe_relative_path(final_path, manifest_dir),
                "screenshot_sha256": sha256_file(final_path),
                "perceptual_hash": candidate["perceptual_hash"],
                "selection_reason": "场景变化候选，经本地信息密度过滤和感知哈希去重保留",
                "visual_kind": candidate["information_metrics"]["visual_kind_hint"],
                "ocr_raw": ocr_text,
                "ocr_engine": ocr_engine,
                "corrected_text": "",
                "references": list(reference_names),
                "related_transcript": "",
                "related_theme": "",
                "reference_findings": "",
                "description": "",
                "inference": "",
                "uncertainties": list(ocr_uncertainties),
                "review_status": "pending",
            }
        )

    manifest["items"] = items
    manifest["extraction"]["status"] = "awaiting_review"
    manifest["extraction"]["selected_count"] = len(items)
    manifest["extraction"]["duplicate_count"] = sum(
        item.get("status") == "duplicate" for item in manifest["candidate_frames"]
    )
    manifest["extraction"]["low_information_count"] = sum(
        item.get("status") == "low_information" for item in manifest["candidate_frames"]
    )
    final_screenshots = [item["screenshot_path"] for item in items]
    if manifest["cleanup_plan"].get("keyframe_retention") == "remove_after_verified":
        # 本脚本只登记用户选择，不在 Word 尚未渲染核验时删除截图；后续清理必须
        # 严格使用这个白名单，并在发现未知文件时停止。
        manifest["cleanup_plan"]["permanent_files"] = []
        manifest["cleanup_plan"]["post_verification_files"] = final_screenshots
    else:
        manifest["cleanup_plan"]["permanent_files"] = final_screenshots
        manifest["cleanup_plan"]["post_verification_files"] = []
    manifest["updated_at"] = now_iso()
    atomic_write_json(manifest_path, manifest)


def extract_visual_materials(
    *,
    media: Path,
    output_dir: Path,
    manifest_path: Path,
    ffmpeg: Path,
    duration: float,
    timestamps: Sequence[float],
    scene_threshold: float,
    start: float,
    end: float,
    max_candidates: int,
    confirmed_candidate_count: int | None,
    dedup_hamming: int,
    min_information_score: float,
    keyframe_retention: str,
    references: list[dict[str, Any]],
    ocr_language: str,
    skip_ocr: bool,
) -> dict[str, Any]:
    """执行可续跑截图提取；调用者必须已经通过全部用户确认关卡。"""

    if dedup_hamming < 0:
        raise VisualWorkflowError("dedup-hamming 必须大于等于 0。")
    if not 0 <= min_information_score <= 1:
        raise VisualWorkflowError("min-information-score 必须位于 0 到 1 之间。")

    signature = _manifest_signature(
        threshold=scene_threshold,
        start=start,
        end=end,
        dedup_hamming=dedup_hamming,
        min_information_score=min_information_score,
        keyframe_retention=keyframe_retention,
        timestamps=timestamps,
    )
    identity = source_identity(media, duration)
    output_dir.mkdir(parents=True, exist_ok=True)

    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise VisualWorkflowError(f"无法读取现有视觉 manifest：{error}") from error
        if not isinstance(manifest, dict):
            raise VisualWorkflowError("现有视觉 manifest 顶层不是 JSON 对象。")
        _validate_resume_manifest(manifest, identity, signature)
        if manifest.get("extraction", {}).get("status") in {
            "awaiting_review",
            "review_complete",
            "docx_built",
        }:
            validate_manifest(manifest, manifest_path, verify_images=True)
            return manifest
    else:
        manifest = _new_manifest(
            source=identity,
            signature=signature,
            max_candidates=max_candidates,
            confirmed_candidate_count=confirmed_candidate_count,
            references=references,
            keyframe_retention=keyframe_retention,
            manifest_dir=manifest_path.parent,
        )
        atomic_write_json(manifest_path, manifest)

    for candidate in manifest["candidate_frames"]:
        screenshot = resolve_manifest_path(candidate["temporary_path"], manifest_path.parent)
        if candidate.get("status") == "extracted":
            if screenshot.is_file() and sha256_file(screenshot) == candidate.get("sha256"):
                continue
            raise VisualWorkflowError(
                f"已提交候选截图缺失或哈希不符，拒绝自动覆盖：{candidate['temporary_path']}"
            )
        extract_frame(media, ffmpeg, float(candidate["timestamp_seconds"]), screenshot)
        candidate["sha256"] = sha256_file(screenshot)
        candidate["perceptual_hash"] = perceptual_hash(screenshot)
        candidate["information_metrics"] = image_information_metrics(screenshot)
        candidate["status"] = "extracted"
        manifest["updated_at"] = now_iso()
        atomic_write_json(manifest_path, manifest)

    _finalize_items(
        manifest,
        manifest_path,
        ocr_language=ocr_language,
        skip_ocr=skip_ocr,
    )
    return manifest


def validate_manifest(
    manifest: dict[str, Any], manifest_path: Path, *, verify_images: bool
) -> None:
    """验证 manifest 结构；构建 Word 前还会校验每张截图哈希。"""

    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise VisualWorkflowError("视觉 manifest 模式版本不受支持。")
    if not isinstance(manifest.get("source"), dict):
        raise VisualWorkflowError("视觉 manifest 缺少 source。")
    items = manifest.get("items")
    if not isinstance(items, list):
        raise VisualWorkflowError("视觉 manifest 的 items 不是列表。")
    seen_ids: set[str] = set()
    required = {
        "item_id",
        "timestamp",
        "screenshot_path",
        "screenshot_sha256",
        "ocr_raw",
        "corrected_text",
        "references",
        "related_transcript",
        "related_theme",
        "reference_findings",
        "description",
        "inference",
        "uncertainties",
        "review_status",
    }
    for item in items:
        if not isinstance(item, dict) or not required.issubset(item):
            raise VisualWorkflowError("视觉 manifest 中存在字段不完整的条目。")
        if item["item_id"] in seen_ids:
            raise VisualWorkflowError(f"视觉条目编号重复：{item['item_id']}")
        seen_ids.add(item["item_id"])
        if item["review_status"] not in ALLOWED_REVIEW_STATUS:
            raise VisualWorkflowError(
                f"{item['item_id']} 的 review_status 无效：{item['review_status']}"
            )
        if item["review_status"] == "complete":
            missing_review = [
                label
                for field, label in REVIEW_COMPLETION_FIELDS.items()
                if not isinstance(item.get(field), str) or not item[field].strip()
            ]
            if missing_review:
                raise VisualWorkflowError(
                    f"{item['item_id']} 已标记 complete，但以下复核字段仍为空："
                    + "、".join(missing_review)
                    + "。若确认没有相关内容，请明确填写“无”。"
                )
        if not isinstance(item["references"], list) or not isinstance(
            item["uncertainties"], list
        ):
            raise VisualWorkflowError(f"{item['item_id']} 的参考资料或不确定项不是列表。")
        if verify_images:
            screenshot = resolve_manifest_path(item["screenshot_path"], manifest_path.parent)
            if not screenshot.is_file():
                raise VisualWorkflowError(f"视觉截图不存在：{item['screenshot_path']}")
            if sha256_file(screenshot) != item["screenshot_sha256"]:
                raise VisualWorkflowError(f"视觉截图哈希不一致：{item['screenshot_path']}")


def load_manifest(path: Path, *, verify_images: bool = False) -> dict[str, Any]:
    """读取并验证视觉 manifest，不对其做隐式修复。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise VisualWorkflowError(f"找不到视觉 manifest：{path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise VisualWorkflowError(f"无法读取视觉 manifest：{error}") from error
    if not isinstance(payload, dict):
        raise VisualWorkflowError("视觉 manifest 顶层不是 JSON 对象。")
    validate_manifest(payload, path, verify_images=verify_images)
    return payload


def _set_run_font(
    run: Any,
    *,
    name: str = "Microsoft YaHei",
    size: float | None = None,
    bold: bool | None = None,
    color: str | None = None,
) -> None:
    """同时设置拉丁与东亚字体，降低 Word/LibreOffice 字体回退差异。"""

    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if color is not None:
        run.font.color.rgb = RGBColor.from_string(color)


def _configure_document_styles(document: Any) -> None:
    """应用 ``compact_reference_guide`` 的精确核心样式令牌。

    视觉附件是密集参考资料，因此选择该预设。中文字体使用 Microsoft YaHei
    作为命名覆盖；页面、边距、字号、段距和行距仍遵循预设数值。
    """

    from docx.enum.section import WD_SECTION_START
    from docx.enum.style import WD_STYLE_TYPE
    from docx.shared import Inches, Pt, RGBColor

    section = document.sections[0]
    section.start_type = WD_SECTION_START.NEW_PAGE
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1)
    section.right_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)

    styles = document.styles
    normal = styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.font.size = Pt(11)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.25

    heading_tokens = {
        "Heading 1": (16, "2E74B5", 18, 10),
        "Heading 2": (13, "2E74B5", 14, 7),
        "Heading 3": (12, "1F4D78", 10, 5),
    }
    for style_name, (size, color, before, after) in heading_tokens.items():
        style = styles[style_name]
        style.font.name = "Microsoft YaHei"
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True

    if "Visual Label" not in styles:
        label_style = styles.add_style("Visual Label", WD_STYLE_TYPE.PARAGRAPH)
    else:
        label_style = styles["Visual Label"]
    label_style.base_style = normal
    label_style.paragraph_format.space_before = Pt(2)
    label_style.paragraph_format.space_after = Pt(5)
    label_style.paragraph_format.line_spacing = 1.25


def _add_labeled_paragraph(document: Any, label: str, value: str) -> None:
    """以可复制文本呈现一个字段，避免用宽表格承载长段 OCR 内容。"""

    paragraph = document.add_paragraph(style="Visual Label")
    label_run = paragraph.add_run(f"{label}：")
    _set_run_font(label_run, size=10.5, bold=True, color="1F4D78")
    value_run = paragraph.add_run(value if value else "（未填写）")
    _set_run_font(value_run, size=10.5)


def _picture_dimensions(path: Path, *, max_width: float = 6.5, max_height: float = 6.35) -> tuple[float, float]:
    """按原始宽高比把截图约束在可用页面范围内。"""

    try:
        from PIL import Image
    except ImportError as error:
        raise VisualWorkflowError("缺少 Pillow，无法计算 Word 截图尺寸。") from error
    with Image.open(path) as image:
        width, height = image.size
    if width <= 0 or height <= 0:
        raise VisualWorkflowError(f"截图像素尺寸无效：{path}")
    scale = min(max_width / width, max_height / height)
    return width * scale, height * scale


def build_visual_docx(
    manifest: dict[str, Any], manifest_path: Path, output: Path, *, title: str
) -> None:
    """从已完成人工复核的 manifest 生成自包含 Word 视觉附件。"""

    validate_manifest(manifest, manifest_path, verify_images=True)
    incomplete = [
        item["item_id"] for item in manifest["items"] if item["review_status"] != "complete"
    ]
    if incomplete:
        raise VisualWorkflowError(
            "build-docx 只接受 review_status=complete；以下条目尚未完成："
            + "、".join(incomplete)
        )
    if not manifest["items"]:
        raise VisualWorkflowError("视觉 manifest 没有可写入 Word 的已复核条目。")

    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Inches, Pt
    except ImportError as error:
        raise VisualWorkflowError("缺少 python-docx，无法生成 Word 视觉资料。") from error

    document = Document()
    _configure_document_styles(document)
    document.core_properties.title = title
    document.core_properties.subject = "视频关键画面、OCR 与参考资料复核记录"
    document.core_properties.author = ""
    document.core_properties.last_modified_by = ""

    # 参考资料附件采用简洁的 editorial-cover 式开场，不使用表格伪造布局。
    title_paragraph = document.add_paragraph()
    title_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    title_paragraph.paragraph_format.space_before = Pt(0)
    title_paragraph.paragraph_format.space_after = Pt(6)
    title_run = title_paragraph.add_run(title)
    _set_run_font(title_run, size=24, bold=True, color="1F4D78")

    subtitle = document.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(18)
    subtitle_run = subtitle.add_run("关键画面截图、OCR 原文、校正文字与证据说明")
    _set_run_font(subtitle_run, size=11, color="555555")

    source = manifest["source"]
    _add_labeled_paragraph(document, "来源视频", str(source.get("name", "（未知）")))
    _add_labeled_paragraph(
        document,
        "提取范围",
        f"{format_timestamp(manifest['extraction']['signature']['start_seconds'])} - "
        f"{format_timestamp(manifest['extraction']['signature']['end_seconds'])}",
    )
    _add_labeled_paragraph(document, "画面数量", str(len(manifest["items"])))

    for index, item in enumerate(manifest["items"], start=1):
        if index > 1:
            document.add_page_break()
        heading = document.add_paragraph(
            f"画面 {index:03d} · {item['timestamp']}", style="Heading 1"
        )
        heading.paragraph_format.keep_with_next = True

        screenshot = resolve_manifest_path(item["screenshot_path"], manifest_path.parent)
        width, height = _picture_dimensions(screenshot)
        picture_paragraph = document.add_paragraph()
        picture_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        picture_paragraph.paragraph_format.space_after = Pt(8)
        picture_run = picture_paragraph.add_run()
        # run.add_picture 创建的是 inline drawing；图片二进制被写入 word/media。
        picture_run.add_picture(str(screenshot), width=Inches(width), height=Inches(height))

        references = "；".join(str(value) for value in item["references"])
        uncertainties = "；".join(str(value) for value in item["uncertainties"])
        _add_labeled_paragraph(document, "时间戳", str(item["timestamp"]))
        _add_labeled_paragraph(document, "OCR 原始识别文字", str(item["ocr_raw"]))
        _add_labeled_paragraph(document, "校正后的画面文字", str(item["corrected_text"]))
        _add_labeled_paragraph(document, "参考资料", references)
        _add_labeled_paragraph(document, "关联逐字稿", str(item["related_transcript"]))
        _add_labeled_paragraph(document, "关联主题", str(item["related_theme"]))
        _add_labeled_paragraph(
            document, "参考资料核验信息", str(item["reference_findings"])
        )
        _add_labeled_paragraph(document, "可见画面说明", str(item["description"]))
        _add_labeled_paragraph(document, "整理者推断", str(item["inference"]))
        _add_labeled_paragraph(document, "不确定项", uncertainties or "无")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f".{output.stem}.tmp.{os.getpid()}.{uuid.uuid4().hex}{output.suffix}"
    )
    try:
        document.save(temporary)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise VisualWorkflowError("python-docx 未生成有效 Word 文件。")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_filename_component(value: str, *, fallback: str, max_length: int = 64) -> str:
    """生成保留中文且兼容 Windows 的短文件名片段。"""

    cleaned = re.sub(r'[<>:"/\\|?*\[\]()#%!\x00-\x1f]', "_", str(value))
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")
    if not cleaned or cleaned == "无":
        cleaned = fallback
    return cleaned[:max_length].rstrip(" ._") or fallback


def _markdown_asset_filename(
    source_name: str, item: dict[str, Any], *, suffix: str
) -> str:
    """按“视频名 + 时间戳 + 画面编号 + 主题”生成稳定图片名。"""

    video = _safe_filename_component(Path(source_name).stem, fallback="视频")
    timestamp = _safe_filename_component(
        str(item["timestamp"]).replace(":", "-").replace(".", "-"),
        fallback="时间未知",
    )
    theme = _safe_filename_component(
        str(item.get("related_theme") or item.get("visual_kind") or "关键画面"),
        fallback="关键画面",
    )
    item_id = _safe_filename_component(str(item["item_id"]), fallback="visual")
    normalized_suffix = suffix.lower() if suffix else ".png"
    return f"{video}_{timestamp}_{item_id}_{theme}{normalized_suffix}"


def _preflight_managed_markdown(markdown: str) -> tuple[int | None, int | None]:
    """确认受管视觉章节标记成对且至多出现一次。"""

    start_count = markdown.count(MARKDOWN_VISUALS_START)
    end_count = markdown.count(MARKDOWN_VISUALS_END)
    if start_count != end_count or start_count > 1:
        raise VisualWorkflowError(
            "最终 Markdown 的资料库视觉章节标记损坏或重复；请先人工核对，脚本未改写正文。"
        )
    if start_count == 0:
        return None, None
    start = markdown.index(MARKDOWN_VISUALS_START)
    end = markdown.index(MARKDOWN_VISUALS_END, start) + len(MARKDOWN_VISUALS_END)
    return start, end


def embed_markdown_visuals(
    manifest: dict[str, Any],
    manifest_path: Path,
    markdown_path: Path,
    *,
    asset_dir: Path | None = None,
    section_title: str = "关键画面",
) -> dict[str, Any]:
    """复制已复核截图并原子写入最终 Markdown 的受管视觉章节。"""

    validate_manifest(manifest, manifest_path, verify_images=True)
    incomplete = [
        item["item_id"] for item in manifest["items"] if item["review_status"] != "complete"
    ]
    if incomplete:
        raise VisualWorkflowError(
            "embed-markdown 只接受 review_status=complete；以下条目尚未完成："
            + "、".join(incomplete)
        )
    if not manifest["items"]:
        raise VisualWorkflowError("视觉 manifest 没有可嵌入 Markdown 的已复核条目。")

    markdown_path = markdown_path.expanduser().resolve()
    if (
        not markdown_path.is_file()
        or markdown_path.suffix.lower() != ".md"
        or markdown_path.stat().st_size == 0
    ):
        raise VisualWorkflowError(f"最终 Markdown 不存在、为空或扩展名错误：{markdown_path}")
    markdown_parent = markdown_path.parent.resolve()
    resolved_asset_dir = (
        asset_dir.expanduser().resolve()
        if asset_dir is not None
        else markdown_parent / f"{markdown_path.stem}_assets"
    )
    try:
        asset_relative_dir = resolved_asset_dir.relative_to(markdown_parent)
    except ValueError as error:
        raise VisualWorkflowError("Markdown 图片目录必须位于最终 Markdown 所在目录内。") from error
    if not asset_relative_dir.parts:
        raise VisualWorkflowError("Markdown 图片必须放入独立 assets 子目录，不能散落在正文目录。")
    if resolved_asset_dir.exists() and not resolved_asset_dir.is_dir():
        raise VisualWorkflowError(f"Markdown 图片位置存在但不是目录：{resolved_asset_dir}")

    try:
        existing_markdown = markdown_path.read_text(encoding="utf-8-sig")
    except UnicodeError as error:
        raise VisualWorkflowError(f"最终 Markdown 不是可读取的 UTF-8 文本：{markdown_path}") from error
    managed_start, managed_end = _preflight_managed_markdown(existing_markdown)

    source_name = str(manifest["source"].get("name") or "视频")
    planned: list[dict[str, Any]] = []
    destinations: set[Path] = set()
    for item in manifest["items"]:
        source_image = resolve_manifest_path(item["screenshot_path"], manifest_path.parent)
        destination = resolved_asset_dir / _markdown_asset_filename(
            source_name, item, suffix=source_image.suffix
        )
        if destination in destinations:
            raise VisualWorkflowError(f"Markdown 图片命名冲突：{destination.name}")
        destinations.add(destination)
        source_hash = item["screenshot_sha256"]
        if destination.exists() and (
            not destination.is_file() or sha256_file(destination) != source_hash
        ):
            raise VisualWorkflowError(
                f"Markdown 图片已存在但内容不同，拒绝覆盖：{destination}"
            )
        relative_link = destination.relative_to(markdown_parent).as_posix()
        planned.append(
            {
                "item": item,
                "source": source_image,
                "destination": destination,
                "relative_link": relative_link,
                "sha256": source_hash,
            }
        )

    lines = [MARKDOWN_VISUALS_START, f"## {section_title}", ""]
    for record in planned:
        item = record["item"]
        theme = str(item.get("related_theme") or item.get("visual_kind") or "关键画面")
        alt = re.sub(
            r"[\[\]\r\n]",
            "_",
            f"{Path(source_name).stem} {item['timestamp']} {theme}",
        )
        lines.extend(
            [
                f"### {theme}",
                "",
                f"![{alt}]({record['relative_link']})",
                "",
                f"*视频：{source_name}；时间戳：{item['timestamp']}；画面类型：{item['visual_kind']}。*",
                "",
                f"- 画面文字：{item['corrected_text']}",
                f"- 画面说明：{item['description']}",
                f"- 参考资料核验：{item['reference_findings']}",
                f"- 整理者说明／推断：{item['inference']}",
                f"- 不确定项：{'；'.join(str(value) for value in item['uncertainties']) or '无'}",
                "",
            ]
        )
    lines.append(MARKDOWN_VISUALS_END)
    managed_section = "\n".join(lines)

    if managed_start is None or managed_end is None:
        updated_markdown = existing_markdown.rstrip() + "\n\n" + managed_section + "\n"
    else:
        updated_markdown = (
            existing_markdown[:managed_start].rstrip()
            + "\n\n"
            + managed_section
            + "\n"
            + existing_markdown[managed_end:].lstrip("\r\n")
        )

    resolved_asset_dir.mkdir(parents=True, exist_ok=True)
    for record in planned:
        if not record["destination"].exists():
            _copy_atomic(record["source"], record["destination"])
        if sha256_file(record["destination"]) != record["sha256"]:
            raise VisualWorkflowError(f"Markdown 图片复制后哈希不一致：{record['destination']}")
    atomic_write_bytes(markdown_path, updated_markdown.encode("utf-8"))

    manifest["markdown_visuals"] = {
        "markdown_path": str(markdown_path),
        "asset_dir": str(resolved_asset_dir),
        "section_title": section_title,
        "embedded_at": now_iso(),
        "managed_markers": [MARKDOWN_VISUALS_START, MARKDOWN_VISUALS_END],
        "assets": [
            {
                "item_id": record["item"]["item_id"],
                "path": record["relative_link"],
                "sha256": record["sha256"],
            }
            for record in planned
        ],
    }
    manifest["updated_at"] = now_iso()
    atomic_write_json(manifest_path, manifest)
    return {
        "operation": "embed-markdown",
        "markdown": str(markdown_path),
        "asset_dir": str(resolved_asset_dir),
        "asset_count": len(planned),
        "relative_links": [record["relative_link"] for record in planned],
        "managed_section_replaced": managed_start is not None,
    }


def inspect_manifest_summary(path: Path) -> dict[str, Any]:
    """只读汇总已有 manifest；损坏时报告问题但绝不尝试修复。"""

    if not path.is_file():
        return {"exists": False, "path": str(path)}
    try:
        manifest = load_manifest(path, verify_images=False)
        review_counts: dict[str, int] = {}
        for item in manifest.get("items", []):
            status = str(item.get("review_status", "invalid"))
            review_counts[status] = review_counts.get(status, 0) + 1
        return {
            "exists": True,
            "path": str(path),
            "valid": True,
            "extraction_status": manifest.get("extraction", {}).get("status"),
            "item_count": len(manifest.get("items", [])),
            "review_counts": review_counts,
            "document": manifest.get("document"),
            "markdown_visuals": manifest.get("markdown_visuals"),
        }
    except VisualWorkflowError as error:
        return {"exists": True, "path": str(path), "valid": False, "error": str(error)}


def command_inspect(args: argparse.Namespace) -> dict[str, Any]:
    """执行严格只读的媒体／manifest 检查。"""

    media = Path(args.input).expanduser().resolve()
    if not media.is_file():
        raise VisualWorkflowError(f"找不到视频文件：{media}")
    ffmpeg = find_ffmpeg()
    duration = probe_duration_seconds(media, ffmpeg)
    start, end = validate_time_range(duration, args.start_seconds, args.end_seconds)
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else media.parent / DEFAULT_MANIFEST_NAME
    )
    summary: dict[str, Any] = {
        "operation": "inspect",
        "read_only": True,
        "source": {
            "path": str(media),
            "name": media.name,
            "size_bytes": media.stat().st_size,
            "duration_seconds": round(duration, 3),
            "duration": format_timestamp(duration),
        },
        "requested_range": {
            "start_seconds": start,
            "end_seconds": end,
            "start": format_timestamp(start),
            "end": format_timestamp(end),
        },
        "manifest": inspect_manifest_summary(manifest_path),
        "visual_extraction_requires_confirmation": True,
    }
    if args.scan_scenes:
        timestamps = detect_scene_timestamps(
            media,
            ffmpeg,
            threshold=args.scene_threshold,
            start_seconds=start,
            end_seconds=end,
        )
        summary["scene_scan"] = candidate_scope_report(
            timestamps,
            max_candidates=args.max_candidates,
            start=start,
            end=end,
        )
        summary["scene_scan"]["over_limit"] = len(timestamps) > args.max_candidates
    else:
        summary["scene_scan"] = {
            "performed": False,
            "note": "添加 --scan-scenes 可只读统计场景变化候选；该扫描可能耗时。",
        }
    return summary


def command_extract(args: argparse.Namespace) -> dict[str, Any]:
    """执行确认后的候选提取，并返回可序列化结果摘要。"""

    require_confirmation(bool(args.confirmed_by_user), "extract")
    media = Path(args.input).expanduser().resolve()
    if not media.is_file():
        raise VisualWorkflowError(f"找不到视频文件：{media}")
    ffmpeg = find_ffmpeg()
    duration = probe_duration_seconds(media, ffmpeg)
    start, end = validate_time_range(duration, args.start_seconds, args.end_seconds)
    timestamps = detect_scene_timestamps(
        media,
        ffmpeg,
        threshold=args.scene_threshold,
        start_seconds=start,
        end_seconds=end,
    )
    enforce_candidate_limit(
        timestamps,
        max_candidates=args.max_candidates,
        confirmed_candidate_count=args.confirmed_candidate_count,
        start=start,
        end=end,
    )

    # 只有全部确认关卡通过后才允许创建输出目录或 manifest。
    output_dir = Path(args.output_dir).expanduser().resolve()
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else output_dir / DEFAULT_MANIFEST_NAME
    )
    try:
        manifest_path.relative_to(output_dir)
    except ValueError as error:
        raise VisualWorkflowError("extract 的 manifest 必须位于 output-dir 内。") from error

    manifest = extract_visual_materials(
        media=media,
        output_dir=output_dir,
        manifest_path=manifest_path,
        ffmpeg=ffmpeg,
        duration=duration,
        timestamps=timestamps,
        scene_threshold=args.scene_threshold,
        start=start,
        end=end,
        max_candidates=args.max_candidates,
        confirmed_candidate_count=args.confirmed_candidate_count,
        dedup_hamming=args.dedup_hamming,
        min_information_score=args.min_information_score,
        keyframe_retention=args.keyframe_retention,
        references=reference_records(args.reference),
        ocr_language=args.ocr_language,
        skip_ocr=args.skip_ocr,
    )
    return {
        "operation": "extract",
        "manifest": str(manifest_path),
        "status": manifest["extraction"]["status"],
        "detected_candidate_count": manifest["extraction"]["detected_candidate_count"],
        "selected_count": manifest["extraction"].get("selected_count", len(manifest["items"])),
        "review_required": any(
            item["review_status"] != "complete" for item in manifest["items"]
        ),
        "cleanup_is_automatic": manifest["cleanup_plan"]["automatic_deletion"],
    }


def command_build_docx(args: argparse.Namespace) -> dict[str, Any]:
    """执行确认后的 Word 构建并回写文档哈希，不执行截图清理。"""

    require_confirmation(bool(args.confirmed_by_user), "build-docx")
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_manifest(manifest_path, verify_images=True)
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else manifest_path.parent / DEFAULT_DOCX_NAME
    )
    title = args.title or "视频视觉资料"
    build_visual_docx(manifest, manifest_path, output, title=title)

    # Word 生成成功后只更新计划状态；未知文件仍不在删除白名单内，也不会被删除。
    manifest["document"] = {
        "path": str(output),
        "sha256": sha256_file(output),
        "size_bytes": output.stat().st_size,
        "built_at": now_iso(),
        "self_contained_inline_images": True,
    }
    manifest["extraction"]["status"] = "docx_built"
    manifest["cleanup_plan"]["status"] = "awaiting_docx_render_verification"
    manifest["updated_at"] = now_iso()
    atomic_write_json(manifest_path, manifest)
    return {
        "operation": "build-docx",
        "output": str(output),
        "sha256": manifest["document"]["sha256"],
        "item_count": len(manifest["items"]),
        "cleanup_performed": False,
        "cleanup_plan_status": manifest["cleanup_plan"]["status"],
    }


def command_embed_markdown(args: argparse.Namespace) -> dict[str, Any]:
    """执行确认后的 Markdown 图片复制与受管章节写入。"""

    require_confirmation(bool(args.confirmed_by_user), "embed-markdown")
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_manifest(manifest_path, verify_images=True)
    return embed_markdown_visuals(
        manifest,
        manifest_path,
        Path(args.markdown),
        asset_dir=Path(args.asset_dir) if args.asset_dir else None,
        section_title=args.section_title,
    )


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器；问答由 Skill 对话层负责。"""

    parser = argparse.ArgumentParser(
        description="视频关键画面候选、复核 manifest 与自包含 Word 附件工具"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="只读检查视频与已有 manifest")
    inspect_parser.add_argument("input", help="本地视频文件")
    inspect_parser.add_argument("--manifest", help="已有视觉 manifest；默认查视频同目录")
    inspect_parser.add_argument("--scan-scenes", action="store_true", help="只读统计场景变化候选")
    inspect_parser.add_argument("--scene-threshold", type=float, default=DEFAULT_SCENE_THRESHOLD)
    inspect_parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    inspect_parser.add_argument("--start-seconds", type=float, default=0.0)
    inspect_parser.add_argument("--end-seconds", type=float)
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.set_defaults(handler=command_inspect)

    extract_parser = subparsers.add_parser("extract", help="确认后提取、去重并建立复核 manifest")
    extract_parser.add_argument("input", help="本地视频文件")
    extract_parser.add_argument("--output-dir", required=True, help="视觉资料输出目录")
    extract_parser.add_argument("--manifest", help="必须位于 output-dir 内的 manifest 路径")
    extract_parser.add_argument("--confirmed-by-user", action="store_true")
    extract_parser.add_argument("--scene-threshold", type=float, default=DEFAULT_SCENE_THRESHOLD)
    extract_parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    extract_parser.add_argument(
        "--confirmed-candidate-count",
        type=int,
        help="候选超限后，用户明确确认的本次精确候选数",
    )
    extract_parser.add_argument("--start-seconds", type=float, default=0.0)
    extract_parser.add_argument("--end-seconds", type=float)
    extract_parser.add_argument("--dedup-hamming", type=int, default=DEFAULT_DEDUP_HAMMING)
    extract_parser.add_argument(
        "--min-information-score", type=float, default=DEFAULT_MIN_INFORMATION_SCORE
    )
    extract_parser.add_argument(
        "--keyframe-retention",
        choices=("keep", "remove_after_verified"),
        default="keep",
        help=(
            "独立关键帧默认长期保留；也可登记为 Word 全页渲染和哈希验证后"
            "按白名单清理。本命令不会自动删除。"
        ),
    )
    extract_parser.add_argument(
        "--reference", action="append", default=[], help="用于 OCR/说明复核的参考文件，可重复"
    )
    extract_parser.add_argument("--ocr-language", default="chi_sim+eng")
    extract_parser.add_argument("--skip-ocr", action="store_true")
    extract_parser.add_argument("--json", action="store_true")
    extract_parser.set_defaults(handler=command_extract)

    docx_parser = subparsers.add_parser("build-docx", help="从已完成复核的 manifest 生成 Word")
    docx_parser.add_argument("manifest", help="视觉资料 manifest")
    docx_parser.add_argument("--output", help=f"输出 DOCX；默认 {DEFAULT_DOCX_NAME}")
    docx_parser.add_argument("--title", help="Word 标题")
    docx_parser.add_argument("--confirmed-by-user", action="store_true")
    docx_parser.add_argument("--json", action="store_true")
    docx_parser.set_defaults(handler=command_build_docx)

    markdown_parser = subparsers.add_parser(
        "embed-markdown",
        help="把已复核截图复制到 Markdown 的 assets 子目录并写入相对链接",
    )
    markdown_parser.add_argument("manifest", help="视觉资料 manifest")
    markdown_parser.add_argument("markdown", help="已经生成的最终 Markdown")
    markdown_parser.add_argument("--asset-dir", help="默认使用 <Markdown stem>_assets")
    markdown_parser.add_argument("--section-title", default="关键画面")
    markdown_parser.add_argument("--confirmed-by-user", action="store_true")
    markdown_parser.add_argument("--json", action="store_true")
    markdown_parser.set_defaults(handler=command_embed_markdown)
    return parser


def _human_summary(result: dict[str, Any]) -> str:
    """为非 JSON CLI 模式生成简洁、可读的摘要。"""

    operation = result.get("operation")
    if operation == "inspect":
        source = result["source"]
        manifest = result["manifest"]
        return (
            f"只读检查完成：{source['name']}，时长 {source['duration']}。\n"
            f"视觉 manifest：{'已存在' if manifest['exists'] else '不存在'}。\n"
            "执行 extract 前仍需用户明确确认。"
        )
    if operation == "extract":
        return (
            f"视觉候选已建立：检测 {result['detected_candidate_count']} 个，"
            f"保留 {result['selected_count']} 个。\n"
            f"manifest：{result['manifest']}\n"
            "请结合截图前后逐字稿和参考资料填写校正文字、说明、不确定项，并把"
            "每项 review_status 改为 complete 后再生成 Word。"
        )
    if operation == "build-docx":
        return (
            f"Word 视觉附件已生成：{result['output']}（{result['item_count']} 项）。\n"
            "未自动清理任何临时截图；请先完成 DOCX 渲染验证，再按 manifest 白名单清理。"
        )
    if operation == "embed-markdown":
        return (
            f"已把 {result['asset_count']} 张关键画面写入：{result['markdown']}。\n"
            f"图片目录：{result['asset_dir']}；Markdown 使用相对链接。"
        )
    return json.dumps(result, ensure_ascii=False, indent=2)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。已知工作流错误以退出码 2 返回，不输出 Python 堆栈。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except VisualWorkflowError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_human_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
