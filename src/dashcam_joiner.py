"""
Dashcam Video Joiner
コムテックドライブレコーダー AVI動画結合ツール (Windows 11)
"""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FILENAME_PATTERN = re.compile(r"^(\d{8})_(\d{6})_F_Nor\.AVI$", re.IGNORECASE)

# ファイル名時刻差チェック (30秒・31秒混在に対応)
INTERVAL_MIN = 29
INTERVAL_MAX = 32

# duration チェック
DURATION_MIN = 29.0
DURATION_MAX = 31.5
DURATION_LAST_WARN_MIN = 5.0
DURATION_DIFF_WARN = 2.0
AV_DURATION_DIFF_MAX = 0.5

# 29秒未満の時刻差判定で使う許容誤差 (duration とファイル名時刻差のズレを吸収)
SHORT_INTERVAL_TOLERANCE_SECONDS = 1.0

# 正規化パラメータ (音ズレ対策モード)
DEFAULT_FPS_STR = "55/2"          # fps取得不可時のデフォルト (27.5fps)
NORMALIZE_CRF = 18
NORMALIZE_AUDIO_RATE = 48000
NORMALIZE_AUDIO_BITRATE = "192k"

# モード定数
MODE_SAFE = "safe"   # 音ズレ対策モード (デフォルト)
MODE_FAST = "fast"   # 高速・無劣化モード (非推奨)

# エンコード方式 (MODE_SAFE 内のサブオプション)
ENCODE_MODE_CPU_STABLE = "cpu_stable"  # CPU安定・高画質 (libx264 / CRF18)
ENCODE_MODE_VIDEO_COPY = "video_copy"  # 高速・映像コピー音声補正 (映像copy / 音声AAC補正)

ENCODE_MODE_LABEL: dict[str, str] = {
    ENCODE_MODE_CPU_STABLE: "CPU安定・高画質（libx264 / CRF18）",
    ENCODE_MODE_VIDEO_COPY: "高速・映像コピー音声補正（映像copy / 音声AAC補正）",
}

# 録画空白区間の扱い
GAP_MODE_JOIN   = "join"    # 警告して1本に結合する（推奨）
GAP_MODE_SPLIT  = "split"   # 空白区間で分割して複数ファイルにする
GAP_MODE_STRICT = "strict"  # 空白区間があれば中止する

GAP_MODE_LABEL: dict[str, str] = {
    GAP_MODE_JOIN:   "警告して1本に結合",
    GAP_MODE_SPLIT:  "空白区間で分割",
    GAP_MODE_STRICT: "空白区間があれば中止",
}


# ---------------------------------------------------------------------------
# App base directory
# ---------------------------------------------------------------------------

def get_app_base_dir() -> Path:
    """
    PyInstaller (frozen) では exe の親ディレクトリ、
    通常 Python では src/ の 2 階層上 (プロジェクトルート) を返す。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# FFmpeg / ffprobe discovery
# ---------------------------------------------------------------------------

def _find_executable(name: str) -> Optional[str]:
    """tools/<name> → 同階層/<name> → PATH の順に探す。"""
    base = get_app_base_dir()
    for path in [base / "tools" / name, base / name]:
        if path.is_file():
            return str(path)
    return shutil.which(name)


def find_ffmpeg() -> tuple[Optional[str], Optional[str]]:
    ffmpeg = _find_executable("ffmpeg.exe") or _find_executable("ffmpeg")
    ffprobe = _find_executable("ffprobe.exe") or _find_executable("ffprobe")
    return ffmpeg, ffprobe


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class Logger:
    def __init__(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = log_dir / f"join_{ts}.txt"
        self._file = open(self.path, "w", encoding="utf-8")
        self.write("=== Dashcam Video Joiner Log ===")
        self.write(f"実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.write("")

    def write(self, msg: str) -> None:
        print(msg)
        self._file.write(msg + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


# ---------------------------------------------------------------------------
# File validation
# ---------------------------------------------------------------------------

def parse_filename(filename: str) -> Optional[datetime]:
    """ファイル名から日時を返す。形式不正は None。"""
    m = FILENAME_PATTERN.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def validate_filenames(files: list[Path]) -> tuple[bool, list[str]]:
    """全ファイルのファイル名形式チェック。"""
    errors: list[str] = []
    for f in files:
        if f.suffix.lower() != ".avi":
            errors.append(f"AVI以外のファイル: {f.name}")
            continue
        if parse_filename(f.name) is None:
            errors.append(f"ファイル名形式不正: {f.name}")
    return len(errors) == 0, errors


def sort_files(files: list[Path]) -> list[Path]:
    """日時昇順ソート。parse_filename が None のファイルは後ろへ。"""
    def key(p: Path):
        dt = parse_filename(p.name)
        return dt if dt is not None else datetime.max
    return sorted(files, key=key)


def check_interval(sorted_files: list[Path]) -> tuple[bool, list[str]]:
    """
    隣接ファイル間の時刻差チェック。
    29〜32秒ならOK（30秒・31秒混在に対応）。
    """
    errors: list[str] = []
    datetimes = [parse_filename(f.name) for f in sorted_files]
    for i in range(1, len(datetimes)):
        prev = datetimes[i - 1]
        curr = datetimes[i]
        if prev is None or curr is None:
            continue
        diff = (curr - prev).total_seconds()
        if not (INTERVAL_MIN <= diff <= INTERVAL_MAX):
            errors.append(
                f"間隔異常: {sorted_files[i-1].name} → {sorted_files[i].name}"
                f" (差: {diff:.1f}秒, 許容: {INTERVAL_MIN}-{INTERVAL_MAX}秒)"
            )
    return len(errors) == 0, errors


def check_interval_detailed(sorted_files: list[Path]) -> dict:
    """
    隣接ファイル間の時刻差を事前分類する。

    normal  (29〜32秒) : 通常連続区間
    gap     (32秒超)   : 録画空白区間（駐車・休憩など）
    overlap (diff<=0)  : 重複・時計異常の疑い → 常にエラー
    pending (0<diff<29): 前ファイルduration確認が必要 (ffprobe後に分類)

    戻り値:
        {
            "normal_count":         int,
            "gap_issues":           list[dict],  # kind="gap"
            "overlap_issues":       list[dict],  # kind="overlap_or_order_error" (diff<=0)
            "pending_short_issues": list[dict],  # kind="needs_duration_check" (0<diff<29)
        }
    各 issue dict: {kind, file_index, prev_file, curr_file, diff_seconds}
    """
    normal_count = 0
    gap_issues: list[dict] = []
    overlap_issues: list[dict] = []
    pending_short_issues: list[dict] = []
    datetimes = [parse_filename(f.name) for f in sorted_files]
    for i in range(1, len(datetimes)):
        prev_dt = datetimes[i - 1]
        curr_dt = datetimes[i]
        if prev_dt is None or curr_dt is None:
            continue
        diff = (curr_dt - prev_dt).total_seconds()
        issue = {
            "file_index": i,            # curr_file の sorted_files 内インデックス
            "prev_file": sorted_files[i - 1],
            "curr_file": sorted_files[i],
            "diff_seconds": diff,
        }
        if INTERVAL_MIN <= diff <= INTERVAL_MAX:
            normal_count += 1
        elif diff > INTERVAL_MAX:
            issue["kind"] = "gap"
            gap_issues.append(issue)
        elif diff <= 0:
            issue["kind"] = "overlap_or_order_error"
            overlap_issues.append(issue)
        else:  # 0 < diff < INTERVAL_MIN
            issue["kind"] = "needs_duration_check"
            pending_short_issues.append(issue)
    return {
        "normal_count": normal_count,
        "gap_issues": gap_issues,
        "overlap_issues": overlap_issues,
        "pending_short_issues": pending_short_issues,
    }


# ---------------------------------------------------------------------------
# Gap / segment helpers
# ---------------------------------------------------------------------------

def classify_short_intervals(
    infos: list[dict],
    pending_short_issues: list[dict],
    logger: Logger,
) -> tuple[list[dict], list[dict]]:
    """
    0 < diff < 29秒 の間隔を前ファイルの実 duration で分類する (ffprobe後に呼ぶ)。

    判定基準 (previous_duration = prev_file の video stream duration 優先):
        prev_duration <= diff + SHORT_INTERVAL_TOLERANCE_SECONDS
            → short_clip_gap: 前ファイルが短く終わった後の録画再開 (gap 扱い)
        prev_duration > diff + SHORT_INTERVAL_TOLERANCE_SECONDS
            → overlap_or_order_error: 前ファイルと重複する疑い (常にエラー)
        prev_duration 取得不可
            → overlap_or_order_error (安全側)

    Returns:
        (short_clip_gaps, overlap_errors)
    """
    short_clip_gaps: list[dict] = []
    overlap_errors: list[dict] = []

    for issue in pending_short_issues:
        idx = issue["file_index"]      # curr_file のインデックス
        prev_idx = idx - 1             # prev_file のインデックス
        diff = issue["diff_seconds"]

        prev_info = infos[prev_idx] if 0 <= prev_idx < len(infos) else None

        # previous_duration: video stream duration 優先、なければ format.duration
        prev_duration: Optional[float] = None
        if prev_info:
            v = prev_info.get("video")
            if v:
                d = v.get("duration")
                if d and d > 0:
                    prev_duration = d
            if prev_duration is None:
                d = prev_info.get("duration")
                if d and d > 0:
                    prev_duration = d

        new_issue = dict(issue)
        new_issue["prev_duration"] = prev_duration

        if prev_duration is None:
            new_issue["kind"] = "overlap_or_order_error"
            new_issue["no_duration"] = True
            logger.write(
                f"[WARN] 29秒未満の時刻差で前ファイルduration取得不可 → 安全側で危険な異常として扱います: "
                f"{issue['prev_file'].name} → {issue['curr_file'].name}"
                f" (差: {diff:.1f}秒)"
            )
            overlap_errors.append(new_issue)
        elif prev_duration <= diff + SHORT_INTERVAL_TOLERANCE_SECONDS:
            # 前ファイルが短く終わっている → 短い最終クリップ後の録画再開
            new_issue["kind"] = "short_clip_gap"
            short_clip_gaps.append(new_issue)
        else:
            # 前ファイルの再生中に次ファイルが始まっている → 重複の疑い
            new_issue["kind"] = "overlap_or_order_error"
            new_issue["no_duration"] = False
            overlap_errors.append(new_issue)

    return short_clip_gaps, overlap_errors


def split_files_by_gaps(
    sorted_files: list[Path],
    gap_issues: list[dict],
) -> list[list[Path]]:
    """
    gap_issues に基づいてファイルリストをセグメントに分割する。
    gap の curr_file から新しいセグメントを開始する。
    """
    if not gap_issues:
        return [list(sorted_files)]
    gap_starts = {issue["curr_file"] for issue in gap_issues}
    segments: list[list[Path]] = []
    current: list[Path] = []
    for f in sorted_files:
        if f in gap_starts and current:
            segments.append(current)
            current = []
        current.append(f)
    if current:
        segments.append(current)
    return segments


def _split_infos_by_segments(
    infos: list[dict],
    sorted_files: list[Path],
    gap_issues: list[dict],
) -> list[list[dict]]:
    """gap_issues に基づいて infos を sorted_files と同じ分割でセグメント化する。"""
    gap_starts = {issue["curr_file"] for issue in gap_issues}
    seg_infos: list[list[dict]] = []
    current: list[dict] = []
    for f, info in zip(sorted_files, infos):
        if f in gap_starts and current:
            seg_infos.append(current)
            current = []
        current.append(info)
    if current:
        seg_infos.append(current)
    return seg_infos


def make_segment_output_path(
    base_path: Path,
    seg_idx: int,
    seg_files: list[Path],
) -> Path:
    """分割セグメント用の出力ファイルパスを生成する。

    例: final.mp4, seg_idx=1, files=[20260606_105002..., 20260606_105911...]
    → final_part001_20260606_105002-20260606_105911.mp4
    """
    start_dt = parse_filename(seg_files[0].name)
    end_dt = parse_filename(seg_files[-1].name)
    start_str = start_dt.strftime("%Y%m%d_%H%M%S") if start_dt else "unknown"
    end_str = end_dt.strftime("%Y%m%d_%H%M%S") if end_dt else "unknown"
    name = f"{base_path.stem}_part{seg_idx:03d}_{start_str}-{end_str}{base_path.suffix}"
    return base_path.parent / name


# ---------------------------------------------------------------------------
# ffprobe inspection
# ---------------------------------------------------------------------------

def probe_file(ffprobe_path: str, filepath: Path) -> Optional[dict]:
    """ffprobe でファイル情報をJSONで取得。失敗時は None。"""
    cmd = [
        ffprobe_path, "-v", "error",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(filepath),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def _parse_stream_duration(s: dict) -> Optional[float]:
    """ストリームの duration を float で返す。取得不可は None。"""
    raw = s.get("duration")
    if raw is None:
        return None
    try:
        val = float(raw)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


def extract_stream_info(probe: dict) -> dict:
    """プローブ結果から映像・音声情報を抽出する。"""
    info: dict = {"video": None, "audio": None, "duration": None}
    streams = probe.get("streams", [])
    for s in streams:
        codec_type = s.get("codec_type", "")
        if codec_type == "video" and info["video"] is None:
            fps_raw = s.get("r_frame_rate", "0/1")
            try:
                num, den = fps_raw.split("/")
                fps = float(num) / float(den) if float(den) != 0 else 0.0
            except (ValueError, ZeroDivisionError):
                fps = 0.0
            info["video"] = {
                "codec": s.get("codec_name", ""),
                "width": s.get("width", 0),
                "height": s.get("height", 0),
                "fps": round(fps, 3),
                "fps_raw": fps_raw,          # 正規化時に使う raw 文字列 (例: "55/2")
                "duration": _parse_stream_duration(s),
            }
        elif codec_type == "audio" and info["audio"] is None:
            info["audio"] = {
                "codec": s.get("codec_name", ""),
                "sample_rate": s.get("sample_rate", ""),
                "channels": s.get("channels", 0),
                "duration": _parse_stream_duration(s),
            }
    fmt = probe.get("format", {})
    try:
        info["duration"] = float(fmt.get("duration", 0))
    except (ValueError, TypeError):
        info["duration"] = 0.0
    return info


def check_streams(
    files: list[Path],
    ffprobe_path: str,
    logger: Logger,
    progress_cb,
) -> tuple[bool, list[str], list[dict], list[str], list[dict]]:
    """
    全ファイルのffprobe検査と仕様一致チェック。
    戻り値: (ok, errors, infos, warnings, short_clip_candidates)
    short_clip_candidates: 非最終ファイルで duration が 5〜29秒のファイル一覧。
                           gap か overlap かは呼び出し元で次ファイルとの時刻差で判定する。
    """
    errors: list[str] = []
    warnings: list[str] = []
    infos: list[dict] = []
    short_clip_candidates: list[dict] = []
    ref_video: Optional[dict] = None
    ref_audio: Optional[dict] = None
    total = len(files)

    logger.write("--- ffprobe 検査開始 ---")
    for i, f in enumerate(files):
        progress_cb(i + 1, total)
        probe = probe_file(ffprobe_path, f)
        if probe is None:
            errors.append(f"ffprobe失敗: {f.name}")
            infos.append({})
            continue

        info = extract_stream_info(probe)
        infos.append(info)

        if info["video"] is None:
            errors.append(f"動画トラックなし: {f.name}")
            continue
        if info["audio"] is None:
            errors.append(f"音声トラックなし: {f.name}")
            continue

        dur = info["duration"]
        is_last = (i == total - 1)
        if is_last:
            if dur < DURATION_LAST_WARN_MIN:
                errors.append(f"最終ファイルのdurationが短すぎます: {f.name} ({dur:.2f}秒)")
            elif dur < DURATION_MIN:
                warnings.append(
                    f"[WARN] 最終ファイルが短いですが許可します: {f.name} ({dur:.2f}秒)"
                )
            elif dur > DURATION_MAX:
                errors.append(f"最終ファイルのdurationが長すぎます: {f.name} ({dur:.2f}秒)")
        else:
            if dur < DURATION_LAST_WARN_MIN:
                errors.append(
                    f"duration異常(短すぎ): {f.name} ({dur:.2f}秒, {DURATION_LAST_WARN_MIN}秒未満)"
                )
            elif dur < DURATION_MIN:
                # 5.0〜29.0秒: 短い最終クリップ候補として記録し、後で時刻差で判定
                logger.write(
                    f"[INFO] 短いdurationのファイルを検出 (短い最終クリップ候補): "
                    f"{f.name} ({dur:.2f}秒)"
                )
                short_clip_candidates.append({
                    "file_index": i,
                    "file": f,
                    "duration": dur,
                })
            elif dur > DURATION_MAX:
                errors.append(
                    f"duration異常(長すぎ): {f.name} ({dur:.2f}秒, 期待: {DURATION_MAX}秒以下)"
                )

        v_dur = info["video"].get("duration")
        a_dur = info["audio"].get("duration")
        if v_dur is not None and a_dur is not None:
            av_diff = abs(v_dur - a_dur)
            if av_diff > AV_DURATION_DIFF_MAX:
                errors.append(
                    f"映像/音声duration差が大きい: {f.name} "
                    f"(video={v_dur:.2f}s, audio={a_dur:.2f}s, diff={av_diff:.2f}s)"
                )
        else:
            logger.write(
                f"[INFO] stream duration取得不可: {f.name} (video={v_dur}, audio={a_dur})"
            )

        if ref_video is None:
            ref_video = info["video"]
            ref_audio = info["audio"]
        else:
            v = info["video"]
            a = info["audio"]
            if v["codec"] != ref_video["codec"]:
                errors.append(
                    f"video codec不一致: {f.name} ({v['codec']} vs {ref_video['codec']})"
                )
            if v["width"] != ref_video["width"] or v["height"] != ref_video["height"]:
                errors.append(
                    f"解像度不一致: {f.name} ({v['width']}x{v['height']} vs "
                    f"{ref_video['width']}x{ref_video['height']})"
                )
            if abs(v["fps"] - ref_video["fps"]) > 0.1:
                errors.append(f"fps不一致: {f.name} ({v['fps']} vs {ref_video['fps']})")
            if a["codec"] != ref_audio["codec"]:
                errors.append(
                    f"audio codec不一致: {f.name} ({a['codec']} vs {ref_audio['codec']})"
                )
            if a["sample_rate"] != ref_audio["sample_rate"]:
                errors.append(
                    f"sample rate不一致: {f.name} ({a['sample_rate']} vs {ref_audio['sample_rate']})"
                )
            if a["channels"] != ref_audio["channels"]:
                errors.append(
                    f"channels不一致: {f.name} ({a['channels']} vs {ref_audio['channels']})"
                )

        v_dur_str = f"{v_dur:.2f}s" if v_dur is not None else "N/A"
        a_dur_str = f"{a_dur:.2f}s" if a_dur is not None else "N/A"
        logger.write(
            f"  [{i+1:03d}] {f.name} | "
            f"video={info['video']['codec']} {info['video']['width']}x{info['video']['height']} "
            f"{info['video']['fps']}fps({info['video']['fps_raw']}) vdur={v_dur_str} | "
            f"audio={info['audio']['codec']} {info['audio']['sample_rate']}Hz "
            f"ch={info['audio']['channels']} adur={a_dur_str} | fmt_dur={dur:.2f}s"
        )

    for w in warnings:
        logger.write(w)
    logger.write("--- ffprobe 検査終了 ---")
    return len(errors) == 0, errors, infos, warnings, short_clip_candidates


# ---------------------------------------------------------------------------
# concat list generation
# ---------------------------------------------------------------------------

def escape_ffconcat_path(path: Path) -> str:
    """
    FFmpeg concat demuxer 用パスエスケープ。
    スラッシュ正規化 + シングルクォートを '\'' にエスケープ。
    """
    posix_path = path.resolve().as_posix()
    return posix_path.replace("'", r"'\''")


def make_concat_list(files: list[Path], list_path: Path) -> Path:
    """FFmpeg concat demuxer 用リストファイルを生成する。"""
    with open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            escaped = escape_ffconcat_path(p)
            f.write(f"file '{escaped}'\n")
    return list_path


# ---------------------------------------------------------------------------
# fps helper for safe mode
# ---------------------------------------------------------------------------

def get_normalize_fps(infos: list[dict]) -> str:
    """
    先頭ファイルの r_frame_rate を返す。
    取得不可・無効な場合は DEFAULT_FPS_STR ("55/2") を返す。
    """
    for info in infos:
        if not info or not info.get("video"):
            continue
        fps_raw = info["video"].get("fps_raw", "")
        if not fps_raw or fps_raw in ("0/1", "0/0", ""):
            continue
        try:
            num, den = fps_raw.split("/")
            if float(den) > 0 and float(num) > 0:
                return fps_raw
        except (ValueError, ZeroDivisionError):
            pass
    return DEFAULT_FPS_STR


# ---------------------------------------------------------------------------
# Single AVI → MP4 normalization  (safe mode, Step 2)
# ---------------------------------------------------------------------------

def normalize_avi_to_mp4(
    ffmpeg_path: str,
    input_path: Path,
    output_path: Path,
    fps_str: str,
    logger: Logger,
) -> tuple[bool, str]:
    """
    1 つの AVI を MP4 に正規化する。
    タイムスタンプをリセットし、音声を同期させる。
    """
    cmd = [
        ffmpeg_path, "-y",
        "-fflags", "+genpts",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-vf", f"setpts=PTS-STARTPTS,fps={fps_str}",
        "-af", "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(NORMALIZE_CRF),
        "-c:a", "aac",
        "-ar", str(NORMALIZE_AUDIO_RATE),
        "-b:a", NORMALIZE_AUDIO_BITRATE,
        str(output_path),
    ]
    logger.write(f"正規化コマンド [{input_path.name}]: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
        logger.write(f"--- stderr [{input_path.name}] ---")
        logger.write(result.stderr or "(なし)")
        if "non monotonic" in result.stderr.lower() or "non-monotonic" in result.stderr.lower():
            logger.write(f"[INFO] Non-monotonic DTS/PTS 検出: {input_path.name}")
        if result.returncode != 0:
            return False, f"ffmpeg終了コード: {result.returncode}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"正規化タイムアウト (600秒超): {input_path.name}"
    except OSError as e:
        return False, f"ffmpeg起動エラー: {e}"


# ---------------------------------------------------------------------------
# Single AVI → MP4 normalization  (video copy mode, Step 2)
# ---------------------------------------------------------------------------

def normalize_avi_to_mp4_video_copy(
    ffmpeg_path: str,
    input_path: Path,
    output_path: Path,
    logger: Logger,
) -> tuple[bool, str]:
    """
    1 つの AVI を MP4 に正規化する (高速・映像コピー音声補正モード)。
    映像はコピー (-c:v copy)、音声のみ AAC 変換と同期補正を行う。
    -vf / fps 補正は適用しない。
    """
    cmd = [
        ffmpeg_path, "-y",
        "-fflags", "+genpts",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-c:v", "copy",
        "-af", "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
        "-c:a", "aac",
        "-ar", str(NORMALIZE_AUDIO_RATE),
        "-b:a", NORMALIZE_AUDIO_BITRATE,
        "-movflags", "+faststart",
        str(output_path),
    ]
    logger.write(f"正規化コマンド(映像copy) [{input_path.name}]: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
        logger.write(f"--- stderr [{input_path.name}] ---")
        logger.write(result.stderr or "(なし)")
        if "non monotonic" in result.stderr.lower() or "non-monotonic" in result.stderr.lower():
            logger.write(f"[INFO] Non-monotonic DTS/PTS 検出: {input_path.name}")
        if result.returncode != 0:
            return False, f"ffmpeg終了コード: {result.returncode}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"正規化タイムアウト (600秒超): {input_path.name}"
    except OSError as e:
        return False, f"ffmpeg起動エラー: {e}"


# ---------------------------------------------------------------------------
# Final MP4 concat  (safe mode, Step 3)
# ---------------------------------------------------------------------------

def run_mp4_concat(
    ffmpeg_path: str,
    concat_list: Path,
    output_path: Path,
    logger: Logger,
) -> tuple[bool, str]:
    """
    正規化済み MP4 を結合する。
    映像はコピー、音声は AAC へ再エンコードしてタイムスタンプをならす。
    """
    cmd = [
        ffmpeg_path, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-c:v", "copy",
        "-af", "aresample=async=1:first_pts=0",
        "-c:a", "aac",
        "-ar", str(NORMALIZE_AUDIO_RATE),
        "-b:a", NORMALIZE_AUDIO_BITRATE,
        "-movflags", "+faststart",
        str(output_path),
    ]
    logger.write(f"MP4結合コマンド: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=3600,
        )
        logger.write("--- ffmpeg stdout ---")
        logger.write(result.stdout or "(なし)")
        logger.write("--- ffmpeg stderr ---")
        logger.write(result.stderr or "(なし)")
        if "non monotonic" in result.stderr.lower() or "non-monotonic" in result.stderr.lower():
            logger.write(
                "[INFO] 最終結合で Non-monotonic DTS/PTS 警告を検出 (音声再エンコードで軽減済み)"
            )
        if result.returncode != 0:
            return False, f"ffmpeg終了コード: {result.returncode}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "MP4結合タイムアウト (3600秒超)"
    except OSError as e:
        return False, f"ffmpeg起動エラー: {e}"


# ---------------------------------------------------------------------------
# AVI concat  (fast mode / legacy)
# ---------------------------------------------------------------------------

def run_ffmpeg_join(
    ffmpeg_path: str,
    concat_list: Path,
    output_path: Path,
    logger: Logger,
) -> tuple[bool, str]:
    """FFmpeg concat demuxer で AVI を結合。映像1本・音声1本のみコピー。"""
    cmd = [
        ffmpeg_path, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-dn", "-sn",
        "-c", "copy",
        str(output_path),
    ]
    logger.write(f"ffmpeg コマンド: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=3600,
        )
        logger.write("--- ffmpeg stdout ---")
        logger.write(result.stdout or "(なし)")
        logger.write("--- ffmpeg stderr ---")
        logger.write(result.stderr or "(なし)")
        if result.returncode != 0:
            return False, f"ffmpeg 終了コード: {result.returncode}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "ffmpeg タイムアウト (3600秒超)"
    except OSError as e:
        return False, f"ffmpeg 起動エラー: {e}"


# ---------------------------------------------------------------------------
# Post-join duration check
# ---------------------------------------------------------------------------

def check_output_duration(
    ffprobe_path: str,
    output_path: Path,
    expected_total: float,
    basis_label: str,
    logger: Logger,
) -> tuple[bool, str]:
    """出力ファイルの duration と入力合計を比較する。"""
    probe = probe_file(ffprobe_path, output_path)
    if probe is None:
        msg = "出力ファイルのffprobeに失敗しました"
        logger.write(f"[WARN] {msg}")
        return False, msg
    info = extract_stream_info(probe)
    actual = info["duration"]
    diff = abs(actual - expected_total)
    logger.write(f"duration比較基準: {basis_label}")
    logger.write(f"入力duration合計: {expected_total:.2f}秒")
    logger.write(f"出力duration:     {actual:.2f}秒")
    logger.write(f"差分:             {diff:.2f}秒")
    if diff <= DURATION_DIFF_WARN:
        logger.write("duration チェック: OK")
        return True, f"OK (差: {diff:.2f}秒)"
    else:
        msg = f"duration差が {diff:.2f}秒 (許容: {DURATION_DIFF_WARN}秒以内) — 要確認"
        logger.write(f"[WARN] {msg}")
        return False, msg


# ---------------------------------------------------------------------------
# Duration helpers for audio sync mode
# ---------------------------------------------------------------------------

def probe_normalized_mp4_duration(
    ffprobe_path: str,
    mp4_path: Path,
    logger: Logger,
) -> Optional[float]:
    """正規化後 MP4 の duration を ffprobe で取得する。取得不可は None。"""
    probe = probe_file(ffprobe_path, mp4_path)
    if probe is None:
        logger.write(f"[WARN] 正規化後MP4のffprobe失敗: {mp4_path.name}")
        return None
    info = extract_stream_info(probe)
    dur = info.get("duration")
    if dur is None or dur <= 0:
        logger.write(f"[WARN] 正規化後MP4のduration無効: {mp4_path.name} (dur={dur})")
        return None
    return dur


def determine_expected_duration_for_audio_sync_mode(
    infos: list[dict],
    normalized_durations: list[Optional[float]],
    logger: Logger,
) -> tuple[float, str]:
    """
    音ズレ対策モードの duration 期待値を決定する。
    優先1: 正規化後 MP4 duration 合計
    優先2: 元 AVI video stream duration 合計
    優先3: 元 AVI format.duration 合計 (fallback)
    """
    fmt_total = sum(info.get("duration", 0.0) for info in infos if info)

    v_durs = [
        info["video"].get("duration")
        for info in infos
        if info and info.get("video")
    ]
    v_total: Optional[float] = None
    if len(v_durs) == len(infos) and all(d is not None and d > 0 for d in v_durs):
        v_total = sum(v_durs)

    norm_total: Optional[float] = None
    if (
        len(normalized_durations) == len(infos)
        and all(d is not None and d > 0 for d in normalized_durations)
    ):
        norm_total = sum(normalized_durations)

    # 診断ログ
    logger.write(f"  元AVI format.duration合計:        {fmt_total:.2f}秒")
    v_str = f"{v_total:.2f}秒" if v_total is not None else "N/A"
    n_str = f"{norm_total:.2f}秒" if norm_total is not None else "N/A"
    logger.write(f"  元AVI video stream duration合計:  {v_str}")
    logger.write(f"  正規化後MP4 duration合計:         {n_str}")

    if norm_total is not None:
        logger.write("  採用: 正規化後MP4 duration合計")
        return norm_total, "正規化後MP4 duration合計"

    if v_total is not None:
        logger.write("  [WARN] 正規化後MP4 duration一部取得不可 — 元AVI video stream duration合計を採用")
        return v_total, "元AVI video stream duration合計"

    logger.write("  [WARN] 正規化後MP4 / 元AVI video stream duration取得不可 — 元AVI format.duration合計を採用 (偽警告の可能性あり)")
    return fmt_total, "元AVI format.duration合計"


# ---------------------------------------------------------------------------
# Common pre-check helper (両モード共通)
# ---------------------------------------------------------------------------

def _run_pre_checks(
    files: list[Path],
    output_path: Path,
    ffprobe_path: str,
    mode: str,
    gap_mode: str,
    logger: Logger,
    log,
    ui_status_cb,
    ui_done_cb,
    probe_progress_cb,
) -> Optional[dict]:
    """
    共通事前チェック。
    成功: dict(sorted_files, infos, warns, total_input_dur, fps_str, gap_issues)
    失敗: None (ui_done_cb を呼んで return)
    """
    mode_label = "音ズレ対策モード (MP4出力)" if mode == MODE_SAFE else "高速・無劣化モード (AVI出力)"
    logger.write(f"モード: {mode_label}")

    # 0. 出力先上書き防止
    input_resolved = {p.resolve() for p in files}
    if output_path.resolve() in input_resolved:
        msg = "出力先が入力ファイルと同じです。元動画を上書きするため中止します。"
        log(f"[ERROR] {msg}")
        logger.write("結果: 失敗")
        ui_done_cb(False, msg)
        return None

    # 1. ファイル名チェック
    ui_status_cb("ファイル名チェック中...")
    ok, errs = validate_filenames(files)
    if not ok:
        for e in errs:
            log(f"[ERROR] {e}")
        log("ファイル名チェック: 失敗 — 結合を中止します")
        logger.write("結果: 失敗")
        ui_done_cb(False, "ファイル名チェックに失敗しました")
        return None
    log("ファイル名形式: OK")

    # 2. ソート
    sorted_files = sort_files(files)
    logger.write("--- ソート後ファイル一覧 ---")
    for i, f in enumerate(sorted_files):
        logger.write(f"  [{i+1:03d}] {f.name}")

    # 3. 時刻差チェック (ffprobe 前)
    ui_status_cb("時刻差チェック中...")
    interval_result      = check_interval_detailed(sorted_files)
    normal_cnt           = interval_result["normal_count"]
    gap_issues           = interval_result["gap_issues"]
    overlap_issues       = interval_result["overlap_issues"]
    pending_short_issues = interval_result["pending_short_issues"]

    pending_label = (
        f" / 短い間隔(duration確認待ち) {len(pending_short_issues)}箇所"
        if pending_short_issues else ""
    )
    log(
        f"時刻差チェック: 通常連続 {normal_cnt}箇所 / "
        f"録画空白 {len(gap_issues)}箇所 / "
        f"危険な異常 {len(overlap_issues)}箇所"
        f"{pending_label}"
    )

    # diff <= 0 は常にエラー停止
    if overlap_issues:
        for issue in overlap_issues:
            log(
                f"[ERROR] 危険な時刻差: {issue['prev_file'].name} → {issue['curr_file'].name}"
                f" (差: {issue['diff_seconds']:.1f}秒)"
            )
        log("時刻差チェック: 失敗 — 危険な時刻差があるため結合を中止します")
        logger.write("結果: 失敗")
        ui_done_cb(
            False,
            "危険な時刻差（0秒以下）を検出しました。\nファイルの順序や重複を確認してください。",
        )
        return None

    # 録画空白区間 (diff > 32秒) の処理
    if gap_issues:
        if gap_mode == GAP_MODE_STRICT:
            for issue in gap_issues:
                log(
                    f"[ERROR] 録画空白区間を検出したため中止します: "
                    f"{issue['prev_file'].name} → {issue['curr_file'].name}"
                    f" (差: {issue['diff_seconds']:.1f}秒)"
                )
            log("時刻差チェック: 失敗 — 録画空白区間があるため中止します (厳密チェック)")
            logger.write("結果: 失敗")
            ui_done_cb(False, "録画空白区間を検出しました（厳密チェックモード）")
            return None
        else:
            for issue in gap_issues:
                log(
                    f"[WARN] 録画空白区間: {issue['prev_file'].name}"
                    f" → {issue['curr_file'].name}"
                    f" (差: {issue['diff_seconds']:.1f}秒)"
                )
            if gap_mode == GAP_MODE_JOIN:
                log("[INFO] 録画空白区間を検出しましたが、設定により1本の動画として結合を続行します")
                log("[INFO] 空白時間は動画に挿入しません")
            else:  # GAP_MODE_SPLIT
                log(f"[INFO] 録画空白区間 {len(gap_issues)}箇所 を検出しました (分割点候補)")

    if pending_short_issues:
        log(
            f"[INFO] 29秒未満の間隔が {len(pending_short_issues)}箇所 — "
            f"ffprobe後に前ファイルdurationで詳細分類します"
        )

    if not gap_issues and not overlap_issues and not pending_short_issues:
        log("時刻差チェック: OK")

    # 4. サマリー表示
    estimated_sec = len(sorted_files) * 30
    h, rem = divmod(estimated_sec, 3600)
    m, s = divmod(rem, 60)
    log(f"対象ファイル数: {len(sorted_files)}")
    log(f"開始ファイル: {sorted_files[0].name}")
    log(f"終了ファイル: {sorted_files[-1].name}")
    log(f"推定時間: {h}時間{m:02d}分{s:02d}秒")

    # 5. ffprobe 検査
    ui_status_cb("ffprobe 検査中...")
    ok, errs, infos, warns, short_clip_candidates = check_streams(
        sorted_files, ffprobe_path, logger, probe_progress_cb
    )
    if warns:
        for w in warns:
            log(w)
    if not ok:
        for e in errs:
            log(f"[ERROR] {e}")
        log("ffprobe 検査: 失敗 — 結合を中止します")
        logger.write("結果: 失敗")
        ui_done_cb(False, "ffprobe 検査に失敗しました")
        return None
    log("動画トラック: OK")
    log("音声トラック: OK")
    log("動画/音声形式一致: OK")
    if warns:
        log(f"警告 {len(warns)} 件あり (詳細はログ参照)")

    total_input_dur = sum(info.get("duration", 0.0) for info in infos if info)

    # fps (safe mode 用。fast mode では未使用)
    fps_str = get_normalize_fps(infos)
    logger.write(f"採用fps: {fps_str}")

    # Step 5.5: 29秒未満の間隔を前ファイル duration で詳細分類 (ffprobe後)
    short_clip_cnt = 0
    if pending_short_issues:
        ui_status_cb("短い間隔の詳細チェック中...")
        short_clip_gaps, overlap_errors = classify_short_intervals(
            infos, pending_short_issues, logger
        )

        # overlap_errors は常にエラー停止
        if overlap_errors:
            for issue in overlap_errors:
                prev_dur = issue.get("prev_duration")
                if issue.get("no_duration"):
                    log(
                        f"[ERROR] 29秒未満の時刻差を検出しましたが、前ファイルdurationを取得できないため"
                        f"安全側で中止します: {issue['prev_file'].name} → {issue['curr_file'].name}"
                        f" (差: {issue['diff_seconds']:.1f}秒)"
                    )
                else:
                    log(
                        f"[ERROR] 危険な時刻差: {issue['prev_file'].name} → {issue['curr_file'].name}"
                        f" (差: {issue['diff_seconds']:.1f}秒,"
                        f" 前ファイルduration: {prev_dur:.1f}秒)"
                    )
            log("時刻差チェック: 失敗 — 危険な時刻差があるため結合を中止します")
            logger.write("結果: 失敗")
            ui_done_cb(
                False,
                "危険な時刻差（重複または順序異常の疑い）を検出しました。\nログファイルを確認してください。",
            )
            return None

        # short_clip_gaps の処理 (gap と同じ扱い)
        short_clip_cnt = len(short_clip_gaps)
        for issue in short_clip_gaps:
            prev_dur = issue.get("prev_duration", 0.0)
            log(
                f"[WARN] 短い最終クリップ後の録画再開: {issue['prev_file'].name}"
                f" → {issue['curr_file'].name}"
                f" (差: {issue['diff_seconds']:.1f}秒, 前ファイルduration: {prev_dur:.1f}秒)"
            )

        if short_clip_gaps:
            if gap_mode == GAP_MODE_STRICT:
                for issue in short_clip_gaps:
                    log(
                        f"[ERROR] 録画空白区間を検出したため中止します: "
                        f"{issue['prev_file'].name} → {issue['curr_file'].name}"
                        f" (差: {issue['diff_seconds']:.1f}秒, 短い最終クリップ後の録画再開)"
                    )
                log("時刻差チェック: 失敗 — 録画空白区間があるため中止します (厳密チェック)")
                logger.write("結果: 失敗")
                ui_done_cb(False, "短い最終クリップ後の録画再開を検出しました（厳密チェックモード）")
                return None
            else:
                if gap_mode == GAP_MODE_JOIN:
                    log("[INFO] 設定により1本の動画として結合を続行します")
                    log("[INFO] 空白時間は動画に挿入しません")
                else:  # GAP_MODE_SPLIT
                    log(f"[INFO] 短い最終クリップ後の録画再開 {len(short_clip_gaps)}箇所 (分割点候補)")

        # short_clip_gaps を gap_issues に合流 (split_files_by_gaps で分割点として使う)
        gap_issues = gap_issues + short_clip_gaps

        # 長い録画空白区間の推定録画停止時間をログ (ファイルログのみ)
        for issue in interval_result["gap_issues"]:
            pi = issue["file_index"] - 1
            if 0 <= pi < len(infos) and infos[pi]:
                p_info = infos[pi]
                p_dur: Optional[float] = None
                v = p_info.get("video")
                if v:
                    d = v.get("duration")
                    if d and d > 0:
                        p_dur = d
                if p_dur is None:
                    d = p_info.get("duration")
                    if d and d > 0:
                        p_dur = d
                if p_dur:
                    stop_est = issue["diff_seconds"] - p_dur
                    if stop_est > 0:
                        logger.write(
                            f"[INFO] 推定録画停止時間: {issue['prev_file'].name}"
                            f" → {issue['curr_file'].name}"
                            f" (前ファイルduration={p_dur:.1f}秒,"
                            f" 時刻差={issue['diff_seconds']:.1f}秒,"
                            f" 推定停止={stop_est:.1f}秒)"
                        )

        # 確定版サマリーログ
        log(
            f"時刻差チェック (確定): 通常連続 {normal_cnt}箇所 / "
            f"録画空白 {len(gap_issues) - short_clip_cnt}箇所 / "
            f"短い最終クリップ後の録画再開 {short_clip_cnt}箇所 / "
            f"危険な異常 0箇所"
        )

    # Step 5.6: 短いdurationの非最終クリップを次ファイルとの時刻差で gap/overlap 判定
    if short_clip_candidates:
        ui_status_cb("短いdurationファイルの詳細チェック中...")
        short_dur_errors: list[str] = []
        short_dur_allowed_cnt = 0

        for candidate in short_clip_candidates:
            ci  = candidate["file_index"]
            f   = candidate["file"]
            dur = candidate["duration"]

            # short_clip_candidates は非最終ファイルのみなので sorted_files[ci+1] は必ず存在する
            next_f = sorted_files[ci + 1]
            curr_dt = parse_filename(f.name)
            next_dt = parse_filename(next_f.name)

            if curr_dt is None or next_dt is None:
                log(
                    f"[ERROR] 短いdurationファイルの時刻取得不可: {f.name} ({dur:.2f}秒)"
                )
                short_dur_errors.append(f"短いdurationファイルの時刻取得不可: {f.name}")
                continue

            diff_to_next = (next_dt - curr_dt).total_seconds()

            if diff_to_next >= dur - SHORT_INTERVAL_TOLERANCE_SECONDS:
                # 次ファイルは現クリップ再生終了後に始まる → 短い最終クリップとして許可
                already_gap = any(
                    g.get("prev_file") == f and g.get("curr_file") == next_f
                    for g in gap_issues
                )
                if already_gap:
                    # 既に gap_issues にある (長い録画空白 or short_clip_gap) → 重複登録しない
                    log(
                        f"[WARN] 録画空白直前の短い最終クリップを許可: {f.name}"
                        f" (duration={dur:.2f}秒, 次ファイルまで={diff_to_next:.2f}秒)"
                    )
                else:
                    # 新規 gap として追加
                    log(
                        f"[WARN] 短い最終クリップを録画空白扱いで許可: {f.name}"
                        f" (duration={dur:.2f}秒, 次ファイルまで={diff_to_next:.2f}秒)"
                    )
                    new_gap: dict = {
                        "kind": "short_duration_gap",
                        "file_index": ci + 1,
                        "prev_file": f,
                        "curr_file": next_f,
                        "diff_seconds": diff_to_next,
                        "prev_duration": dur,
                    }
                    if gap_mode == GAP_MODE_STRICT:
                        log(
                            f"[ERROR] 短い最終クリップ後の録画空白を検出したため中止します: "
                            f"{f.name} (duration={dur:.2f}秒, 次ファイルまで={diff_to_next:.2f}秒)"
                        )
                        short_dur_errors.append(
                            f"短い最終クリップ後の録画空白 (厳密チェック): {f.name}"
                        )
                    else:
                        gap_issues = gap_issues + [new_gap]
                        if gap_mode == GAP_MODE_JOIN:
                            log("[INFO] 設定により1本の動画として結合を続行します")
                            log("[INFO] 空白時間は動画に挿入しません")
                        else:  # GAP_MODE_SPLIT
                            log("[INFO] 短い最終クリップ後の空白 (分割点候補)")
                short_dur_allowed_cnt += 1
            else:
                # 次ファイルと重複の可能性 → エラー
                log(
                    f"[ERROR] 短いファイルが次ファイルと重複している可能性:"
                    f" {f.name} → {next_f.name}"
                    f" (duration={dur:.2f}秒, 次ファイルまで={diff_to_next:.2f}秒)"
                )
                short_dur_errors.append(
                    f"duration異常(次ファイルと重複の疑い): {f.name}"
                    f" ({dur:.2f}秒, 次まで{diff_to_next:.2f}秒)"
                )

        if short_dur_errors:
            if gap_mode == GAP_MODE_STRICT:
                log(
                    "ffprobe検査: 失敗 — 短い最終クリップ後の録画空白があるため中止します"
                    " (厳密チェック)"
                )
            else:
                log("duration チェック: 失敗 — 短いdurationファイルに問題があります")
            logger.write("結果: 失敗")
            ui_done_cb(
                False,
                "短いdurationファイルの検証に失敗しました。\nログファイルを確認してください。",
            )
            return None

        if short_dur_allowed_cnt > 0:
            log(f"短い最終クリップ許可: {short_dur_allowed_cnt}件")

    return {
        "sorted_files": sorted_files,
        "infos": infos,
        "warns": warns,
        "total_input_dur": total_input_dur,
        "fps_str": fps_str,
        "gap_issues": gap_issues,
    }


# ---------------------------------------------------------------------------
# Pipeline: 音ズレ対策モード
# ---------------------------------------------------------------------------

def _pipeline_safe(
    files: list[Path],
    output_path: Path,
    ffmpeg_path: str,
    ffprobe_path: str,
    log_dir: Path,
    keep_intermediate: bool,
    encode_mode: str,
    gap_mode: str,
    ui_log_cb,
    ui_status_cb,
    ui_done_cb,
    probe_progress_cb,
) -> None:
    """音ズレ対策モード: AVI→MP4正規化 → MP4結合"""
    logger = Logger(log_dir)

    def log(msg: str) -> None:
        logger.write(msg)
        ui_log_cb(msg)

    work_dir: Optional[Path] = None
    success = False

    try:
        result = _run_pre_checks(
            files, output_path, ffprobe_path,
            MODE_SAFE, gap_mode, logger, log, ui_status_cb, ui_done_cb, probe_progress_cb,
        )
        if result is None:
            return

        sorted_files: list[Path] = result["sorted_files"]
        infos: list[dict] = result["infos"]
        warns: list[str] = result["warns"]
        fps_str: str = result["fps_str"]
        gap_issues: list[dict] = result["gap_issues"]

        log(f"採用fps: {fps_str}")
        logger.write(f"録画空白区間の扱い: {GAP_MODE_LABEL[gap_mode]}")
        logger.write(f"エンコード方式: {ENCODE_MODE_LABEL.get(encode_mode, encode_mode)}")

        # エンコード方式ログ
        if encode_mode == ENCODE_MODE_VIDEO_COPY:
            log("[INFO] 高速・映像コピー音声補正モードを使用します")
            log("[INFO] 映像は再エンコードせず copy します")
            log("[INFO] 音声は AAC 48000Hz 192kbps へ変換し、aresample で同期補正します")
        else:
            log("[INFO] CPU安定・高画質モードを使用します (libx264 / CRF18 / veryfast)")

        # セグメント分割
        if gap_issues and gap_mode == GAP_MODE_SPLIT:
            segments = split_files_by_gaps(sorted_files, gap_issues)
            seg_infos_list = _split_infos_by_segments(infos, sorted_files, gap_issues)
            log(f"[INFO] 録画空白区間で {len(segments)} セグメントに分割します")
            for si, seg in enumerate(segments):
                log(f"[INFO] segment {si+1:03d}: {seg[0].name} → {seg[-1].name}, files={len(seg)}")
        else:
            segments = [sorted_files]
            seg_infos_list = [infos]

        # 作業ディレクトリ作成
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        work_dir = get_app_base_dir() / "work" / f"normalize_{ts}"
        work_dir.mkdir(parents=True, exist_ok=True)
        log(f"作業ディレクトリ: {work_dir}")

        total_files_all = sum(len(s) for s in segments)
        output_files: list[Path] = []
        all_dur_oks: list[bool] = []
        all_dur_msgs: list[str] = []
        global_file_idx = 0

        for seg_idx, (seg_files, seg_infos) in enumerate(zip(segments, seg_infos_list)):
            seg_prefix = f"seg{seg_idx+1:03d}_" if len(segments) > 1 else ""
            seg_output = (
                make_segment_output_path(output_path, seg_idx + 1, seg_files)
                if len(segments) > 1 else output_path
            )

            if len(segments) > 1:
                log(
                    f"--- セグメント {seg_idx+1}/{len(segments)}: "
                    f"{seg_files[0].name} → {seg_files[-1].name} "
                    f"({len(seg_files)}ファイル) ---"
                )

            # 各AVI → MP4 正規化
            normalized_mp4s: list[Path] = []
            normalized_durations: list[Optional[float]] = []

            for i, avi_file in enumerate(seg_files):
                global_file_idx += 1
                mp4_path = work_dir / f"{seg_prefix}normalized_{i+1:04d}.mp4"
                status_msg = f"正規化中 [{global_file_idx}/{total_files_all}] {avi_file.name}"
                log(status_msg)
                ui_status_cb(status_msg)

                if encode_mode == ENCODE_MODE_VIDEO_COPY:
                    ok, err_msg = normalize_avi_to_mp4_video_copy(
                        ffmpeg_path, avi_file, mp4_path, logger
                    )
                else:
                    ok, err_msg = normalize_avi_to_mp4(
                        ffmpeg_path, avi_file, mp4_path, fps_str, logger
                    )
                if not ok:
                    log(f"[ERROR] 正規化失敗: {avi_file.name} — {err_msg}")
                    logger.write("結果: 失敗")
                    ui_done_cb(False, f"正規化に失敗しました: {avi_file.name}\n{err_msg}")
                    return
                normalized_mp4s.append(mp4_path)
                mp4_dur = probe_normalized_mp4_duration(ffprobe_path, mp4_path, logger)
                dur_str = f"{mp4_dur:.2f}秒" if mp4_dur is not None else "N/A"
                logger.write(f"正規化後duration [{i+1:03d}] {mp4_path.name}: {dur_str}")
                normalized_durations.append(mp4_dur)

            seg_label = f"セグメント {seg_idx+1:03d} " if len(segments) > 1 else "全"
            log(f"{seg_label}ファイル正規化完了: {len(seg_files)}ファイル")

            # concat list
            concat_list_path = work_dir / f"{seg_prefix}list.txt"
            make_concat_list(normalized_mp4s, concat_list_path)

            # MP4 結合
            seg_label2 = f"セグメント {seg_idx+1}/{len(segments)} " if len(segments) > 1 else ""
            ui_status_cb(f"{seg_label2}正規化済みMP4を結合中...")
            log(f"{seg_label2}正規化済みMP4を結合中...")
            seg_output.parent.mkdir(parents=True, exist_ok=True)

            ok, err_msg = run_mp4_concat(ffmpeg_path, concat_list_path, seg_output, logger)
            if not ok:
                log(f"[ERROR] {err_msg}")
                logger.write("結果: 失敗")
                ui_done_cb(False, f"結合に失敗しました: {err_msg}")
                return
            log(f"結合完了: {seg_output.name}")
            output_files.append(seg_output)

            # duration 期待値を決定
            logger.write("--- duration 比較基準の選定 ---")
            expected_dur, basis_label = determine_expected_duration_for_audio_sync_mode(
                seg_infos, normalized_durations, logger
            )

            # 結合後 duration チェック
            ui_status_cb("結合後 duration チェック中...")
            dur_ok, dur_msg = check_output_duration(
                ffprobe_path, seg_output, expected_dur, basis_label, logger
            )
            log(f"結合後durationチェック: {dur_msg}")
            all_dur_oks.append(dur_ok)
            all_dur_msgs.append(dur_msg)

        success = True
        logger.write("結果: 成功")

        # 完了メッセージ
        if len(output_files) == 1:
            msg = f"結合完了: {output_files[0].name}"
        else:
            file_list = "\n".join(f"  {f.name}" for f in output_files)
            msg = f"分割結合完了: {len(output_files)} ファイル\n{file_list}"
        dur_failed = [m for ok, m in zip(all_dur_oks, all_dur_msgs) if not ok]
        if dur_failed:
            msg += "\n[警告] " + " / ".join(dur_failed)
        if warns:
            msg += f"\n[注意] 事前チェックで {len(warns)} 件の警告がありました (ログ参照)"
        ui_done_cb(True, msg)

    except Exception as e:
        log(f"[EXCEPTION] {e}")
        logger.write("結果: 例外発生")
        ui_done_cb(False, f"予期しないエラー: {e}")
    finally:
        # 中間ファイルの削除判定
        if work_dir is not None and work_dir.exists():
            if success and not keep_intermediate:
                try:
                    shutil.rmtree(work_dir)
                    logger.write(f"中間ファイルを削除しました: {work_dir}")
                except OSError as e:
                    logger.write(f"[WARN] 中間ファイル削除失敗: {e}")
            else:
                reason = "中間ファイルを残す設定" if keep_intermediate else "エラー発生"
                logger.write(f"中間ファイルを保持します ({reason}): {work_dir}")
        logger.close()


# ---------------------------------------------------------------------------
# Pipeline: 高速・無劣化モード (legacy)
# ---------------------------------------------------------------------------

def _pipeline_fast(
    files: list[Path],
    output_path: Path,
    ffmpeg_path: str,
    ffprobe_path: str,
    log_dir: Path,
    gap_mode: str,
    ui_log_cb,
    ui_status_cb,
    ui_done_cb,
    probe_progress_cb,
) -> None:
    """高速・無劣化モード: AVI直接結合 (-c copy)"""
    logger = Logger(log_dir)

    def log(msg: str) -> None:
        logger.write(msg)
        ui_log_cb(msg)

    try:
        result = _run_pre_checks(
            files, output_path, ffprobe_path,
            MODE_FAST, gap_mode, logger, log, ui_status_cb, ui_done_cb, probe_progress_cb,
        )
        if result is None:
            return

        sorted_files: list[Path] = result["sorted_files"]
        infos: list[dict] = result["infos"]
        warns: list[str] = result["warns"]
        gap_issues: list[dict] = result["gap_issues"]

        logger.write(f"録画空白区間の扱い: {GAP_MODE_LABEL[gap_mode]}")

        # セグメント分割
        if gap_issues and gap_mode == GAP_MODE_SPLIT:
            segments = split_files_by_gaps(sorted_files, gap_issues)
            seg_infos_list = _split_infos_by_segments(infos, sorted_files, gap_issues)
            log(f"[INFO] 録画空白区間で {len(segments)} セグメントに分割します")
            for si, seg in enumerate(segments):
                log(f"[INFO] segment {si+1:03d}: {seg[0].name} → {seg[-1].name}, files={len(seg)}")
        else:
            segments = [sorted_files]
            seg_infos_list = [infos]

        output_files: list[Path] = []
        all_dur_oks: list[bool] = []
        all_dur_msgs: list[str] = []

        ui_status_cb("結合中...")
        for seg_idx, (seg_files, seg_infos) in enumerate(zip(segments, seg_infos_list)):
            seg_output = (
                make_segment_output_path(output_path, seg_idx + 1, seg_files)
                if len(segments) > 1 else output_path
            )

            if len(segments) > 1:
                log(
                    f"--- セグメント {seg_idx+1}/{len(segments)}: "
                    f"{seg_files[0].name} → {seg_files[-1].name} "
                    f"({len(seg_files)}ファイル) ---"
                )
                ui_status_cb(f"セグメント {seg_idx+1}/{len(segments)} 結合中...")

            seg_output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as tmp:
                concat_list = make_concat_list(seg_files, Path(tmp) / "concat_list.txt")
                log(f"出力先: {seg_output}")
                ok, err_msg = run_ffmpeg_join(ffmpeg_path, concat_list, seg_output, logger)
                if not ok:
                    log(f"[ERROR] {err_msg}")
                    logger.write("結果: 失敗")
                    ui_done_cb(False, f"結合に失敗しました: {err_msg}")
                    return
            log(f"結合完了: {seg_output.name}")
            output_files.append(seg_output)

            seg_input_dur = sum(info.get("duration", 0.0) for info in seg_infos if info)
            ui_status_cb("結合後 duration チェック中...")
            dur_ok, dur_msg = check_output_duration(
                ffprobe_path, seg_output, seg_input_dur,
                "元AVI format.duration合計", logger
            )
            log(f"結合後durationチェック: {dur_msg}")
            all_dur_oks.append(dur_ok)
            all_dur_msgs.append(dur_msg)

        logger.write("結果: 成功")

        # 完了メッセージ
        if len(output_files) == 1:
            msg = f"結合完了: {output_files[0].name}"
        else:
            file_list = "\n".join(f"  {f.name}" for f in output_files)
            msg = f"分割結合完了: {len(output_files)} ファイル\n{file_list}"
        dur_failed = [m for ok, m in zip(all_dur_oks, all_dur_msgs) if not ok]
        if dur_failed:
            msg += "\n[警告] " + " / ".join(dur_failed)
        if warns:
            msg += f"\n[注意] 事前チェックで {len(warns)} 件の警告がありました (ログ参照)"
        ui_done_cb(True, msg)

    except Exception as e:
        log(f"[EXCEPTION] {e}")
        logger.write("結果: 例外発生")
        ui_done_cb(False, f"予期しないエラー: {e}")
    finally:
        logger.close()


# ---------------------------------------------------------------------------
# Pipeline dispatcher
# ---------------------------------------------------------------------------

def run_pipeline(
    files: list[Path],
    output_path: Path,
    ffmpeg_path: str,
    ffprobe_path: str,
    log_dir: Path,
    mode: str,
    keep_intermediate: bool,
    encode_mode: str,
    gap_mode: str,
    ui_log_cb,
    ui_status_cb,
    ui_done_cb,
    probe_progress_cb,
) -> None:
    """モードに応じてパイプラインを選択して実行する。"""
    if mode == MODE_SAFE:
        _pipeline_safe(
            files, output_path, ffmpeg_path, ffprobe_path, log_dir,
            keep_intermediate, encode_mode, gap_mode,
            ui_log_cb, ui_status_cb, ui_done_cb, probe_progress_cb,
        )
    else:
        _pipeline_fast(
            files, output_path, ffmpeg_path, ffprobe_path, log_dir,
            gap_mode, ui_log_cb, ui_status_cb, ui_done_cb, probe_progress_cb,
        )


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Dashcam Video Joiner")
        self.resizable(True, True)
        self.minsize(740, 580)

        self._selected_files: list[Path] = []
        self._output_path: Optional[Path] = None
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._mode_var = tk.StringVar(value=MODE_SAFE)
        self._encode_mode_var = tk.StringVar(value=ENCODE_MODE_VIDEO_COPY)
        self._keep_intermediate_var = tk.BooleanVar(value=False)
        self._gap_mode_var = tk.StringVar(value=GAP_MODE_JOIN)

        self._ffmpeg, self._ffprobe = find_ffmpeg()

        self._build_ui()
        self._check_ffmpeg()
        self._poll_log_queue()

    # ----- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 3}

        # --- 結合モード選択 ---
        mode_frame = ttk.LabelFrame(self, text="結合モード", padding=8)
        mode_frame.pack(fill="x", padx=10, pady=(6, 2))

        ttk.Radiobutton(
            mode_frame,
            text="● 音ズレ対策モード（推奨）— AVI を MP4 へ正規化して結合",
            variable=self._mode_var,
            value=MODE_SAFE,
            command=self._on_mode_change,
        ).grid(row=0, column=0, sticky="w", padx=4, pady=2)

        ttk.Radiobutton(
            mode_frame,
            text="○ 高速・無劣化モード（非推奨）— -c copy でAVI結合 / 音ズレする場合あり",
            variable=self._mode_var,
            value=MODE_FAST,
            command=self._on_mode_change,
        ).grid(row=1, column=0, sticky="w", padx=4, pady=2)

        self._keep_cb = ttk.Checkbutton(
            mode_frame,
            text="中間ファイルを残す（音ズレ対策モードのみ有効）",
            variable=self._keep_intermediate_var,
        )
        self._keep_cb.grid(row=2, column=0, sticky="w", padx=24, pady=2)

        # --- エンコード方式 (音ズレ対策モード選択時のみ有効) ---
        encode_frame = ttk.LabelFrame(self, text="エンコード方式（音ズレ対策モード選択時）", padding=8)
        encode_frame.pack(fill="x", padx=10, pady=(2, 2))

        self._encode_rb_cpu = ttk.Radiobutton(
            encode_frame,
            text="● CPU安定・高画質（libx264 / CRF18）— 実績あり・推奨",
            variable=self._encode_mode_var,
            value=ENCODE_MODE_CPU_STABLE,
        )
        self._encode_rb_cpu.grid(row=0, column=0, sticky="w", padx=4, pady=2)

        self._encode_rb_vcopy = ttk.Radiobutton(
            encode_frame,
            text="○ 高速・映像コピー音声補正（映像copy / 音声AAC補正）— 42ファイルで確認済み・長時間は要検証",
            variable=self._encode_mode_var,
            value=ENCODE_MODE_VIDEO_COPY,
        )
        self._encode_rb_vcopy.grid(row=1, column=0, sticky="w", padx=4, pady=2)

        # --- 録画空白区間の扱い ---
        gap_frame = ttk.LabelFrame(self, text="録画空白区間の扱い", padding=8)
        gap_frame.pack(fill="x", padx=10, pady=(2, 2))

        ttk.Radiobutton(
            gap_frame,
            text="● 警告して1本に結合する（推奨）",
            variable=self._gap_mode_var,
            value=GAP_MODE_JOIN,
        ).grid(row=0, column=0, sticky="w", padx=4, pady=2)

        ttk.Radiobutton(
            gap_frame,
            text="○ 空白区間で分割して複数ファイルにする",
            variable=self._gap_mode_var,
            value=GAP_MODE_SPLIT,
        ).grid(row=1, column=0, sticky="w", padx=4, pady=2)

        ttk.Radiobutton(
            gap_frame,
            text="○ 空白区間があれば中止する（厳密チェック）",
            variable=self._gap_mode_var,
            value=GAP_MODE_STRICT,
        ).grid(row=2, column=0, sticky="w", padx=4, pady=2)

        # --- 設定 ---
        top = ttk.LabelFrame(self, text="設定", padding=8)
        top.pack(fill="x", padx=10, pady=(2, 2))

        ttk.Button(top, text="動画ファイルを選択", command=self._select_files).grid(
            row=0, column=0, sticky="w", **pad
        )
        self._files_label = ttk.Label(top, text="未選択", foreground="gray")
        self._files_label.grid(row=0, column=1, sticky="w", **pad)

        ttk.Button(top, text="出力先を選択", command=self._select_output).grid(
            row=1, column=0, sticky="w", **pad
        )
        self._output_label = ttk.Label(top, text="未選択", foreground="gray")
        self._output_label.grid(row=1, column=1, sticky="w", **pad)

        top.columnconfigure(1, weight=1)

        # --- ステータス ---
        self._status_var = tk.StringVar(value="待機中")
        ttk.Label(
            self, textvariable=self._status_var, anchor="w",
            relief="sunken", padding=(6, 2),
        ).pack(fill="x", padx=10, pady=(0, 2))

        self._progress = ttk.Progressbar(self, mode="indeterminate")
        self._progress.pack(fill="x", padx=10, pady=(0, 4))

        # --- ログ ---
        log_frame = ttk.LabelFrame(self, text="ログ", padding=4)
        log_frame.pack(fill="both", expand=True, padx=10, pady=4)

        self._log_text = scrolledtext.ScrolledText(
            log_frame, state="disabled", wrap="word",
            font=("Consolas", 9), height=15,
        )
        self._log_text.pack(fill="both", expand=True)

        # --- 結合開始 ---
        self._start_btn = ttk.Button(
            self, text="結合開始", command=self._start_join, state="disabled"
        )
        self._start_btn.pack(pady=8)

        self._on_mode_change()  # 初期状態

    # ----- Mode change ------------------------------------------------------

    def _on_mode_change(self) -> None:
        """モード変更時のUI更新。"""
        is_safe = (self._mode_var.get() == MODE_SAFE)
        self._keep_cb.config(state="normal" if is_safe else "disabled")
        enc_state = "normal" if is_safe else "disabled"
        self._encode_rb_cpu.config(state=enc_state)
        self._encode_rb_vcopy.config(state=enc_state)
        # 出力先の拡張子がモードと合わない場合はリセット
        if self._output_path is not None:
            expected_ext = ".mp4" if is_safe else ".avi"
            if self._output_path.suffix.lower() != expected_ext:
                self._output_path = None
                self._output_label.config(
                    text="モード変更のため出力先を再選択してください", foreground="orange"
                )
                self._update_start_button()

    # ----- FFmpeg check -----------------------------------------------------

    def _check_ffmpeg(self) -> None:
        if self._ffmpeg and self._ffprobe:
            self._append_log(f"ffmpeg:  {self._ffmpeg}")
            self._append_log(f"ffprobe: {self._ffprobe}")
        else:
            missing = []
            if not self._ffmpeg:
                missing.append("ffmpeg.exe")
            if not self._ffprobe:
                missing.append("ffprobe.exe")
            msg = (
                f"【エラー】{', '.join(missing)} が見つかりません。\n"
                "tools/ フォルダ内または PATH 上に配置してください。\n"
                "tools/README.md を参照してください。"
            )
            self._append_log(msg)
            self._status_var.set("ffmpeg/ffprobe が見つかりません")

    # ----- File selection ---------------------------------------------------

    def _select_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="AVI動画ファイルを選択",
            filetypes=[("AVI files", "*.AVI *.avi"), ("All files", "*.*")],
        )
        if not paths:
            return
        self._selected_files = [Path(p) for p in paths]
        n = len(self._selected_files)
        self._files_label.config(text=f"{n} ファイル選択済み", foreground="black")
        self._append_log(f"{n} ファイルを選択しました")
        self._update_start_button()

    def _select_output(self) -> None:
        is_safe = (self._mode_var.get() == MODE_SAFE)
        if is_safe:
            ext, ftypes, initial = ".mp4", [("MP4 file", "*.mp4")], "joined.mp4"
        else:
            ext, ftypes, initial = ".avi", [("AVI file", "*.avi")], "joined.avi"

        path = filedialog.asksaveasfilename(
            title="出力ファイル名を指定",
            defaultextension=ext,
            filetypes=ftypes,
            initialfile=initial,
        )
        if not path:
            return
        self._output_path = Path(path)
        self._output_label.config(text=str(self._output_path), foreground="black")
        self._update_start_button()

    def _update_start_button(self) -> None:
        ready = (
            bool(self._selected_files)
            and self._output_path is not None
            and bool(self._ffmpeg)
            and bool(self._ffprobe)
        )
        self._start_btn.config(state="normal" if ready else "disabled")

    # ----- Start join -------------------------------------------------------

    def _start_join(self) -> None:
        if not self._selected_files or self._output_path is None:
            return

        self._start_btn.config(state="disabled")
        self._progress.start(10)
        self._status_var.set("処理中...")
        self._clear_log()

        log_dir = get_app_base_dir() / "logs"
        mode = self._mode_var.get()
        keep_intermediate = self._keep_intermediate_var.get()
        encode_mode = self._encode_mode_var.get()
        gap_mode = self._gap_mode_var.get()

        def probe_progress(current: int, total: int) -> None:
            self._log_queue.put(f"ffprobe [{current}/{total}]...")
            self.after(
                0,
                lambda c=current, t=total: self._status_var.set(f"ffprobe 検査中 {c}/{t}"),
            )

        thread = threading.Thread(
            target=run_pipeline,
            args=(
                self._selected_files,
                self._output_path,
                self._ffmpeg,
                self._ffprobe,
                log_dir,
                mode,
                keep_intermediate,
                encode_mode,
                gap_mode,
                lambda msg: self._log_queue.put(msg),
                lambda msg: self.after(0, lambda m=msg: self._status_var.set(m)),
                self._on_done,
                probe_progress,
            ),
            daemon=True,
        )
        thread.start()

    def _on_done(self, success: bool, message: str) -> None:
        self.after(0, lambda: self._finish_ui(success, message))

    def _finish_ui(self, success: bool, message: str) -> None:
        self._progress.stop()
        self._start_btn.config(state="normal")
        if success:
            self._status_var.set("完了")
            messagebox.showinfo("完了", message)
        else:
            self._status_var.set("エラー")
            messagebox.showerror("エラー", message)

    # ----- Log helpers ------------------------------------------------------

    def _append_log(self, msg: str) -> None:
        self._log_text.config(state="normal")
        self._log_text.insert("end", msg + "\n")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _clear_log(self) -> None:
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
