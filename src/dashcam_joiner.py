"""
Dashcam Video Joiner
コムテックドライブレコーダー AVI動画結合ツール (Windows 11)
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FILENAME_PATTERN = re.compile(
    r"^(\d{8})_(\d{6})_F_Nor\.AVI$", re.IGNORECASE
)
DURATION_MIN = 29.0
DURATION_MAX = 31.5
DURATION_DIFF_WARN = 2.0  # seconds


# ---------------------------------------------------------------------------
# FFmpeg / ffprobe discovery
# ---------------------------------------------------------------------------

def _find_executable(name: str) -> Optional[str]:
    """
    tools/<name> → 同階層/<name> → PATH の順に探す。
    PyInstaller(_MEIPASS) と 通常Python(__file__) の両方に対応。
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent.parent

    candidates = [
        base / "tools" / name,
        base / name,
    ]
    for path in candidates:
        if path.is_file():
            return str(path)

    # PATH search
    import shutil
    found = shutil.which(name)
    return found


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
        self.write(f"=== Dashcam Video Joiner Log ===")
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
    """全ファイルのファイル名形式チェック。エラーメッセージリストを返す。"""
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
    """30秒間隔チェック。連続ファイル間の差を確認する。"""
    errors: list[str] = []
    datetimes = [parse_filename(f.name) for f in sorted_files]

    for i in range(1, len(datetimes)):
        prev = datetimes[i - 1]
        curr = datetimes[i]
        if prev is None or curr is None:
            continue
        diff = (curr - prev).total_seconds()
        if abs(diff - 30) > 2:
            errors.append(
                f"間隔異常: {sorted_files[i-1].name} → {sorted_files[i].name}"
                f" (差: {diff:.1f}秒)"
            )
    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# ffprobe inspection
# ---------------------------------------------------------------------------

def probe_file(ffprobe_path: str, filepath: Path) -> Optional[dict]:
    """ffprobe でファイル情報をJSONで取得。失敗時は None。"""
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(filepath),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def extract_stream_info(probe: dict) -> dict:
    """プローブ結果から映像・音声情報を抽出する。"""
    info: dict = {"video": None, "audio": None, "duration": None}

    streams = probe.get("streams", [])
    for s in streams:
        codec_type = s.get("codec_type", "")
        if codec_type == "video" and info["video"] is None:
            fps_str = s.get("r_frame_rate", "0/1")
            try:
                num, den = fps_str.split("/")
                fps = float(num) / float(den) if float(den) != 0 else 0.0
            except (ValueError, ZeroDivisionError):
                fps = 0.0
            info["video"] = {
                "codec": s.get("codec_name", ""),
                "width": s.get("width", 0),
                "height": s.get("height", 0),
                "fps": round(fps, 3),
            }
        elif codec_type == "audio" and info["audio"] is None:
            info["audio"] = {
                "codec": s.get("codec_name", ""),
                "sample_rate": s.get("sample_rate", ""),
                "channels": s.get("channels", 0),
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
) -> tuple[bool, list[str], list[dict]]:
    """
    全ファイルのffprobe検査と仕様一致チェック。
    progress_cb(current, total) で進捗通知。
    """
    errors: list[str] = []
    infos: list[dict] = []
    ref_video: Optional[dict] = None
    ref_audio: Optional[dict] = None

    logger.write("--- ffprobe 検査開始 ---")

    for i, f in enumerate(files):
        progress_cb(i + 1, len(files))
        probe = probe_file(ffprobe_path, f)
        if probe is None:
            errors.append(f"ffprobe失敗: {f.name}")
            infos.append({})
            continue

        info = extract_stream_info(probe)
        infos.append(info)

        # ストリーム存在チェック
        if info["video"] is None:
            errors.append(f"動画トラックなし: {f.name}")
            continue
        if info["audio"] is None:
            errors.append(f"音声トラックなし: {f.name}")
            continue

        # duration チェック (最終ファイル以外)
        dur = info["duration"]
        is_last = (i == len(files) - 1)
        if not is_last and not (DURATION_MIN <= dur <= DURATION_MAX):
            errors.append(
                f"duration異常: {f.name} ({dur:.2f}秒, 期待: {DURATION_MIN}-{DURATION_MAX}秒)"
            )

        # 仕様一致チェック（基準は先頭ファイル）
        if ref_video is None:
            ref_video = info["video"]
            ref_audio = info["audio"]
        else:
            v = info["video"]
            a = info["audio"]
            if v["codec"] != ref_video["codec"]:
                errors.append(f"video codec不一致: {f.name} ({v['codec']} vs {ref_video['codec']})")
            if v["width"] != ref_video["width"] or v["height"] != ref_video["height"]:
                errors.append(
                    f"解像度不一致: {f.name} ({v['width']}x{v['height']} vs "
                    f"{ref_video['width']}x{ref_video['height']})"
                )
            if abs(v["fps"] - ref_video["fps"]) > 0.1:
                errors.append(f"fps不一致: {f.name} ({v['fps']} vs {ref_video['fps']})")
            if a["codec"] != ref_audio["codec"]:
                errors.append(f"audio codec不一致: {f.name} ({a['codec']} vs {ref_audio['codec']})")
            if a["sample_rate"] != ref_audio["sample_rate"]:
                errors.append(
                    f"sample rate不一致: {f.name} ({a['sample_rate']} vs {ref_audio['sample_rate']})"
                )
            if a["channels"] != ref_audio["channels"]:
                errors.append(
                    f"channels不一致: {f.name} ({a['channels']} vs {ref_audio['channels']})"
                )

        logger.write(
            f"  [{i+1:03d}] {f.name} | "
            f"video={info['video']['codec']} {info['video']['width']}x{info['video']['height']} "
            f"{info['video']['fps']}fps | "
            f"audio={info['audio']['codec']} {info['audio']['sample_rate']}Hz "
            f"ch={info['audio']['channels']} | "
            f"dur={dur:.2f}s"
        )

    logger.write("--- ffprobe 検査終了 ---")
    return len(errors) == 0, errors, infos


# ---------------------------------------------------------------------------
# concat list generation
# ---------------------------------------------------------------------------

def make_concat_list(files: list[Path], tmp_dir: Path) -> Path:
    """
    FFmpeg concat demuxer 用リストファイルを生成する。
    パスはスラッシュ正規化、シングルクォートをエスケープ。
    """
    list_path = tmp_dir / "concat_list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            # Windows パスをスラッシュ正規化
            posix_path = p.resolve().as_posix()
            # シングルクォートのエスケープ (concat demuxer: '' で表現)
            escaped = posix_path.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    return list_path


# ---------------------------------------------------------------------------
# FFmpeg join
# ---------------------------------------------------------------------------

def run_ffmpeg_join(
    ffmpeg_path: str,
    concat_list: Path,
    output_path: Path,
    logger: Logger,
) -> tuple[bool, str]:
    """FFmpeg concat demuxer で結合。(stdout, stderr をログへ)"""
    cmd = [
        ffmpeg_path,
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-map", "0",
        "-c", "copy",
        str(output_path),
    ]
    logger.write(f"ffmpeg コマンド: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
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
    logger: Logger,
) -> tuple[bool, str]:
    """出力ファイルのdurationと入力合計を比較。"""
    probe = probe_file(ffprobe_path, output_path)
    if probe is None:
        msg = "出力ファイルのffprobeに失敗しました"
        logger.write(f"[WARN] {msg}")
        return False, msg

    info = extract_stream_info(probe)
    actual = info["duration"]
    diff = abs(actual - expected_total)

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
# Main processing pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    files: list[Path],
    output_path: Path,
    ffmpeg_path: str,
    ffprobe_path: str,
    log_dir: Path,
    ui_log_cb,
    ui_status_cb,
    ui_done_cb,
    probe_progress_cb,
) -> None:
    """バックグラウンドスレッドで実行するメインパイプライン。"""
    logger = Logger(log_dir)

    def log(msg: str) -> None:
        logger.write(msg)
        ui_log_cb(msg)

    try:
        # 1. ファイル名チェック
        ui_status_cb("ファイル名チェック中...")
        ok, errs = validate_filenames(files)
        if not ok:
            for e in errs:
                log(f"[ERROR] {e}")
            log("ファイル名チェック: 失敗 — 結合を中止します")
            logger.write("結果: 失敗")
            ui_done_cb(False, "ファイル名チェックに失敗しました")
            return
        log("ファイル名形式: OK")

        # 2. ソート
        sorted_files = sort_files(files)
        logger.write("--- ソート後ファイル一覧 ---")
        for i, f in enumerate(sorted_files):
            logger.write(f"  [{i+1:03d}] {f.name}")

        # 3. 30秒間隔チェック
        ui_status_cb("30秒間隔チェック中...")
        ok, errs = check_interval(sorted_files)
        if not ok:
            for e in errs:
                log(f"[ERROR] {e}")
            log("30秒間隔チェック: 失敗 — 結合を中止します")
            logger.write("結果: 失敗")
            ui_done_cb(False, "30秒間隔チェックに失敗しました")
            return
        log("30秒間隔チェック: OK")

        # 4. 先頭・末尾情報
        first_dt = parse_filename(sorted_files[0].name)
        last_dt = parse_filename(sorted_files[-1].name)
        estimated_sec = len(sorted_files) * 30
        h, rem = divmod(estimated_sec, 3600)
        m, s = divmod(rem, 60)

        log(f"対象ファイル数: {len(sorted_files)}")
        log(f"開始ファイル: {sorted_files[0].name}")
        log(f"終了ファイル: {sorted_files[-1].name}")
        log(f"推定時間: {h}時間{m:02d}分{s:02d}秒")

        # 5. ffprobe 検査
        ui_status_cb("ffprobe 検査中...")
        ok, errs, infos = check_streams(
            sorted_files, ffprobe_path, logger, probe_progress_cb
        )
        if not ok:
            for e in errs:
                log(f"[ERROR] {e}")
            log("ffprobe 検査: 失敗 — 結合を中止します")
            logger.write("結果: 失敗")
            ui_done_cb(False, "ffprobe 検査に失敗しました")
            return
        log("動画トラック: OK")
        log("音声トラック: OK")
        log("動画/音声形式一致: OK")

        # 入力 duration 合計
        total_input_dur = sum(
            info.get("duration", 0.0) for info in infos if info
        )

        # 6. concat list 生成と結合
        ui_status_cb("結合中...")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            concat_list = make_concat_list(sorted_files, tmp_dir)
            log(f"出力先: {output_path}")

            ok, err_msg = run_ffmpeg_join(ffmpeg_path, concat_list, output_path, logger)
            if not ok:
                log(f"[ERROR] {err_msg}")
                log("結合: 失敗")
                logger.write("結果: 失敗")
                ui_done_cb(False, f"結合に失敗しました: {err_msg}")
                return
        log("結合: 完了")

        # 7. 結合後 duration チェック
        ui_status_cb("結合後 duration チェック中...")
        dur_ok, dur_msg = check_output_duration(
            ffprobe_path, output_path, total_input_dur, logger
        )
        log(f"結合後durationチェック: {dur_msg}")

        # 完了
        logger.write("結果: 成功")
        msg = f"結合完了: {output_path.name}"
        if not dur_ok:
            msg += f"\n[警告] {dur_msg}"
        ui_done_cb(True, msg)

    except Exception as e:
        log(f"[EXCEPTION] {e}")
        logger.write("結果: 例外発生")
        ui_done_cb(False, f"予期しないエラー: {e}")
    finally:
        logger.close()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Dashcam Video Joiner")
        self.resizable(True, True)
        self.minsize(700, 500)

        self._selected_files: list[Path] = []
        self._output_path: Optional[Path] = None
        self._log_queue: queue.Queue[str] = queue.Queue()

        # FFmpeg 検索
        self._ffmpeg, self._ffprobe = find_ffmpeg()

        self._build_ui()
        self._check_ffmpeg()
        self._poll_log_queue()

    # ----- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # Top frame: file / output selection
        top = ttk.LabelFrame(self, text="設定", padding=8)
        top.pack(fill="x", padx=10, pady=6)

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

        # Status
        self._status_var = tk.StringVar(value="待機中")
        status_bar = ttk.Label(self, textvariable=self._status_var, anchor="w",
                               relief="sunken", padding=(6, 2))
        status_bar.pack(fill="x", padx=10, pady=(0, 2))

        # Progress bar
        self._progress = ttk.Progressbar(self, mode="indeterminate")
        self._progress.pack(fill="x", padx=10, pady=(0, 4))

        # Log area
        log_frame = ttk.LabelFrame(self, text="ログ", padding=4)
        log_frame.pack(fill="both", expand=True, padx=10, pady=4)

        self._log_text = scrolledtext.ScrolledText(
            log_frame, state="disabled", wrap="word",
            font=("Consolas", 9), height=18
        )
        self._log_text.pack(fill="both", expand=True)

        # Bottom: start button
        self._start_btn = ttk.Button(
            self, text="結合開始", command=self._start_join, state="disabled"
        )
        self._start_btn.pack(pady=8)

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
        path = filedialog.asksaveasfilename(
            title="出力ファイル名を指定",
            defaultextension=".avi",
            filetypes=[("AVI file", "*.avi")],
            initialfile="joined.avi",
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

        log_dir = Path(__file__).parent.parent / "logs"

        def probe_progress(current: int, total: int) -> None:
            self._log_queue.put(f"ffprobe [{current}/{total}]...")
            self._status_var.set(f"ffprobe 検査中 {current}/{total}")

        thread = threading.Thread(
            target=run_pipeline,
            args=(
                self._selected_files,
                self._output_path,
                self._ffmpeg,
                self._ffprobe,
                log_dir,
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
