# tools フォルダ

このフォルダには **ffmpeg.exe** と **ffprobe.exe** を配置してください。

## 配置ファイル

```
tools/
  ffmpeg.exe      ← ここに置く
  ffprobe.exe     ← ここに置く
  README.md       ← このファイル
```

## 注意事項

- ffmpeg / ffprobe の本体バイナリは **GitHub にはコミットしないでください**。
  `.gitignore` により除外されています。
- システムの PATH に ffmpeg が登録されている場合は、このフォルダへの配置は不要です。
  ツールは以下の順で ffmpeg を検索します:
  1. `tools/ffmpeg.exe` (このフォルダ)
  2. 実行ファイルと同じ階層の `ffmpeg.exe`
  3. PATH 上の `ffmpeg`

## 入手方法

公式ビルドまたは gyan.dev / BtbN のビルドを使用してください。

- 公式サイト: https://ffmpeg.org/download.html
- Windows 向けビルド例: https://www.gyan.dev/ffmpeg/builds/

"ffmpeg-release-essentials.zip" を展開し、`bin/ffmpeg.exe` と `bin/ffprobe.exe` を
このフォルダにコピーしてください。
