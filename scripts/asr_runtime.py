"""GPU preflight, lazy model reuse, and bounded local ASR benchmarking."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from project_io import ProjectError, write_json

_DLL_HANDLES = []
_MODELS = {}


def prepare_libraries(config: dict | None = None, root: Path | None = None) -> list[str]:
    if os.name != 'nt':
        return []
    candidates = [Path(sys.prefix) / 'Lib' / 'site-packages' / 'nvidia' / package / 'bin'
                  for package in ('cublas', 'cudnn', 'cuda_runtime', 'cuda_nvrtc')]
    for value in (config or {}).get('cuda_library_dirs', []):
        path = Path(value)
        candidates.append(path if path.is_absolute() else (root or Path.cwd()) / path)
    selected = []
    for directory in candidates:
        if directory.is_dir():
            resolved = str(directory.resolve())
            _DLL_HANDLES.append(os.add_dll_directory(resolved))
            selected.append(resolved)
    # Some native dependencies use LoadLibrary rather than Python's DLL search.
    entries = os.environ.get('PATH', '').split(os.pathsep)
    os.environ['PATH'] = os.pathsep.join(selected + [p for p in entries if p not in selected])
    return selected


def probe(config: dict | None = None, root: Path | None = None) -> dict:
    libraries = prepare_libraries(config, root)
    versions = {}
    for package in ('faster-whisper', 'ctranslate2', 'av', 'imageio-ffmpeg'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    result = {'python': sys.executable, 'packages': versions, 'cuda_library_dirs': libraries,
              'cuda_device_count': 0, 'cuda_compute_types': [], 'ready_for_model_probe': False}
    try:
        import ctranslate2
        result['cuda_device_count'] = ctranslate2.get_cuda_device_count()
        if result['cuda_device_count']:
            result['cuda_compute_types'] = sorted(ctranslate2.get_supported_compute_types('cuda'))
            result['ready_for_model_probe'] = all(versions.values())
    except (ImportError, RuntimeError) as error:
        result['error'] = str(error)
    try:
        smi = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,memory.used,driver_version',
                              '--format=csv,noheader'], capture_output=True, text=True, timeout=10)
        result['gpu_snapshot'] = smi.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        result['gpu_snapshot'] = None
    result['limitation'] = '设备探测不证明模型可执行；benchmark 才会运行短音频。'
    return result


def get_model(path: Path, device: str, compute_type: str, cpu_threads: int):
    from faster_whisper import WhisperModel
    key = (str(path.resolve()), device, compute_type, cpu_threads)
    if key not in _MODELS:
        _MODELS[key] = WhisperModel(str(path), device=device, compute_type=compute_type,
                                   cpu_threads=cpu_threads, num_workers=1)
    return _MODELS[key]


def release_models():
    _MODELS.clear()
    import gc
    gc.collect()


def benchmark(root: Path, media: Path, output: Path, *, seconds: int = 30, start: float = 0,
              batch_sizes: tuple[int, ...] = (1, 4)) -> dict:
    if seconds < 5 or seconds > 60 or start < 0 or any(b < 1 or b > 16 for b in batch_sizes):
        raise ProjectError('基准仅允许 5 至 60 秒、非负起点以及 1 至 16 的批量大小。')
    import media_stage_manager as manager
    config = manager.load_config(root)
    diagnosis = probe(config, root)
    if not diagnosis['ready_for_model_probe']:
        raise ProjectError('GPU 或依赖探测未通过，请先补齐环境。')
    model_path = manager.config_path(config, 'whisper_model', root)
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix='mdlib-asr-') as temporary:
            sample = Path(temporary) / 'sample.wav'
            manager.extract_chunk_audio(media, {'decode_start_ms': round(start * 1000),
                                               'decode_end_ms': round((start + seconds) * 1000)}, sample)
            before = time.perf_counter()
            model = get_model(model_path, 'cuda', 'float16', min(8, os.cpu_count() or 1))
            load_seconds = time.perf_counter() - before
            for size in batch_sizes:
                runner = model
                if size > 1:
                    from faster_whisper import BatchedInferencePipeline
                    runner = BatchedInferencePipeline(model=model)
                kwargs = {'language': 'zh', 'beam_size': int(config.get('beam_size', 3)), 'vad_filter': True}
                if size > 1:
                    kwargs['batch_size'] = size
                warm_start = time.perf_counter()
                warm_segments, _ = runner.transcribe(str(sample), **kwargs)
                list(warm_segments)
                warmup_seconds = time.perf_counter() - warm_start
                before = time.perf_counter()
                segments, info = runner.transcribe(str(sample), **kwargs)
                segments = list(segments)  # faster-whisper is lazy: exhaust to measure actual work.
                elapsed = time.perf_counter() - before
                results.append({'batch_size': size, 'seconds': round(elapsed, 3),
                                'warmup_seconds': round(warmup_seconds, 3),
                                'audio_seconds': info.duration, 'real_time_factor': round(elapsed / max(info.duration, 0.001), 4),
                                'segment_count': len(segments), 'text': ''.join(s.text for s in segments)})
    finally:
        release_models()
    baseline_length = len(results[0]['text']) if results else 0
    for run in results:
        ratio = len(run['text']) / max(baseline_length, 1)
        run['length_ratio_to_first'] = round(ratio, 3)
        run['quality_gate'] = 'failed_length_divergence' if ratio < 0.8 or ratio > 1.25 else 'requires_semantic_review'
    report = {'schema_version': 'asr-benchmark-1', 'preflight': diagnosis, 'device': 'cuda',
              'compute_type': 'float16', 'model_load_seconds': round(load_seconds, 3),
              'sample_source': str(media), 'sample_start_seconds': start, 'runs': results,
              'caveat': '短样本速度不是长视频承诺；批量默认仍为 1，切换前检查人名、数字、漏句与边界。'}
    write_json(output, report)
    return {'status': 'benchmarked', 'report': str(output), 'device': 'cuda',
            'runs': [{k: v for k, v in r.items() if k != 'text'} for r in results]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['probe', 'benchmark'])
    parser.add_argument('--database-root', type=Path, required=True)
    parser.add_argument('--media', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--start', type=float, default=0)
    args = parser.parse_args()
    try:
        if args.command == 'probe':
            import media_stage_manager as manager
            result = probe(manager.load_config(args.database_root), args.database_root)
        else:
            if not args.media or not args.output:
                raise ProjectError('benchmark 需要 --media 和 --output。')
            result = benchmark(args.database_root, args.media, args.output, seconds=args.seconds, start=args.start)
        print(json.dumps(result, ensure_ascii=False))
    except (ProjectError, OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f'错误：{error}\n')


if __name__ == '__main__':
    main()
