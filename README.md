# Dashcam Video Joiner

コムテック製ドライブレコーダーの AVI 動画ファイルを 1 本に結合する Windows 用ツールです。

## 目的

前方カメラで録画された 30 秒単位の AVI ファイルを、無劣化で 1 本に結合します。
音ズレリスクを下げるため、結合前に複数のチェックを行います。

---

## 対応環境

- OS: Windows 11
- Python: 3.9 以上 (Python 実行時)
- GUI: Tkinter (Python 標準ライブラリ)
- 外部ツール: ffmpeg / ffprobe (別途配置が必要)

---

## 対応ファイル名

```
YYYYMMDD_HHMMSS_F_Nor.AVI

例:
  20260605_071930_F_Nor.AVI
  20260605_072000_F_Nor.AVI
```

前方カメラ (`Front` フォルダ) の動画を対象としてください。

---

## ffmpeg / ffprobe の配置方法

1. [ffmpeg 公式サイト](https://ffmpeg.org/download.html) または
   [gyan.dev ビルド](https://www.gyan.dev/ffmpeg/builds/) から
   Windows 向けビルドを入手します。
2. `tools/README.md` の手順に従い、`ffmpeg.exe` と `ffprobe.exe` を
   `tools/` フォルダに配置します。

```
dashcam-video-joiner/
  tools/
    ffmpeg.exe     ← ここに置く
    ffprobe.exe    ← ここに置く
```

システムの PATH に ffmpeg が登録されている場合は配置不要です。

---

## Python での実行方法

```powershell
# リポジトリのルートで実行
python src\dashcam_joiner.py
```

仮想環境を使う場合:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python src\dashcam_joiner.py
```

---

## 使い方

1. **動画ファイルを選択** — 結合したい AVI ファイルを複数選択します。
2. **出力先を選択** — 出力する AVI ファイルの保存先と名前を指定します。
3. **結合開始** — チェックと結合が自動で実行されます。
4. 完了後、`logs/` フォルダに詳細ログが保存されます。

### GUI チェック結果の表示例

```
対象ファイル数: 240
開始ファイル: 20260605_071930_F_Nor.AVI
終了ファイル: 20260605_091900_F_Nor.AVI
推定時間: 2時間00分00秒
ファイル名形式: OK
30秒間隔チェック: OK
動画トラック: OK
音声トラック: OK
動画/音声形式一致: OK
出力先: C:\Users\kora\Videos\joined.avi
```

---

## exe 化方法

### 前提条件

```powershell
pip install pyinstaller
```

### ビルド実行

```powershell
.\build_exe.ps1
```

### ビルド後の構成

```
dist\DashcamVideoJoiner\
  DashcamVideoJoiner.exe
  tools\          ← ここに ffmpeg.exe / ffprobe.exe を置く
  ...
```

`dist\DashcamVideoJoiner\tools\` フォルダを作成し、
`ffmpeg.exe` と `ffprobe.exe` をコピーしてから起動してください。

---

## 音ズレ対策として行っていること

本ツールは以下の対策で音ズレリスクを低減しています。
「完全に音ズレしないこと」を技術的に保証するものではありません。

| 対策 | 内容 |
|------|------|
| ファイル名形式チェック | 不正なファイルを事前に検出 |
| 30 秒間隔チェック | ファイル欠けや順序ミスを検出 |
| ffprobe ストリーム検査 | 動画・音声トラックの存在と仕様を確認 |
| 全ファイルの仕様一致チェック | codec / 解像度 / fps / sample rate / channels の不一致を検出 |
| FFmpeg concat demuxer | 再エンコードなしの無劣化結合 |
| 結合後 duration 比較 | 入力合計と出力の長さを比較 (差 2 秒以内で OK) |
| 詳細ログ保存 | 全ファイルの ffprobe 結果をログに記録 |

---

## 既知の制限

- 初期版では **AVI のみ** 対応 (MP4 出力なし)
- **Front フォルダ内の前方カメラ動画のみ** を対象とした設計
- リアカメラ動画との結合には非対応
- ドラッグ＆ドロップ非対応
- **完全な音ズレ検出はできません**
  (duration 比較はあくまでも補助チェックです)
- 最終ファイルが 30 秒未満の場合、duration チェックが警告を出すことがあります

---

## トラブルシューティング

### ffmpeg/ffprobe が見つからないと表示される

- `tools/ffmpeg.exe` と `tools/ffprobe.exe` が存在するか確認してください。
- または ffmpeg を PATH に追加してください。

### ファイル名形式エラー

- ファイル名が `YYYYMMDD_HHMMSS_F_Nor.AVI` 形式であることを確認してください。
- `Front` フォルダ内のファイルのみ選択してください。

### 30 秒間隔エラー

- ファイルが欠けていないか確認してください。
- SDカードから正しくコピーされているか確認してください。

### 結合後 duration 警告

- 軽微な場合 (数秒以内) は正常範囲内の場合があります。
- 大きくずれている場合は、入力ファイルに異常がある可能性があります。
