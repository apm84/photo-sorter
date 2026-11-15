# Third-party notices

Photo Sorter's own code is MIT-licensed (see `LICENSE`). It uses the models, data and libraries below, each
under its own license. Nothing here is modified unless stated.

## AI models (downloaded on first run, not inside the installer)

| Model | Use | License | Source |
|---|---|---|---|
| SigLIP 2 base patch16-224 (Google) | image types, tags, near-duplicates, text search | Apache-2.0 | https://huggingface.co/google/siglip2-base-patch16-224 |
| YuNet face detector (2023mar), OpenCV Zoo, (c) 2020 Shiqi Yu | face detection | MIT | https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet |
| SFace face recognizer (2021dec), OpenCV Zoo | face matching | Apache-2.0 | https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface |
| CLIP ViT-B/32 (OpenAI), ONNX by Xenova (optional) | alternative embedder | MIT | https://huggingface.co/Xenova/clip-vit-base-patch32 |
| InsightFace buffalo_l / buffalo_s (optional, never bundled) | alternative faces | non-commercial research use only | https://github.com/deepinsight/insightface |

The SigLIP 2 files in the `models-v1` release were converted to ONNX by `tools/export_siglip2.py` (vision tower
fp32; the text tower's token-embedding table int8-quantized). The weights are otherwise unmodified. The YuNet and
SFace files in that release are byte-for-byte copies of the OpenCV Zoo files (same SHA-256), mirrored for reliable
downloads. Apache License 2.0: https://www.apache.org/licenses/LICENSE-2.0

## Data

- Offline place names: GeoNames `cities1000` (via `reverse_geocoder`), (c) GeoNames, licensed under
  Creative Commons Attribution 4.0 (https://creativecommons.org/licenses/by/4.0/). https://www.geonames.org/

## Libraries bundled in the Windows installer

| Library | License |
|---|---|
| Python, Tcl/Tk | PSF License; Tcl/Tk BSD-style |
| ONNX Runtime (onnxruntime-directml), DirectML redistributable | MIT; Microsoft DirectML license (redistributable) |
| OpenCV (opencv-python-headless) | Apache-2.0 |
| NumPy, SciPy, scikit-learn | BSD-3-Clause |
| Pillow | MIT-CMU (HPND) |
| ImageHash | BSD-2-Clause |
| piexif | MIT |
| tokenizers (Hugging Face) | Apache-2.0 |
| FastAPI, Starlette, Uvicorn, Pydantic | MIT, BSD-3-Clause, BSD-3-Clause, MIT |
| Send2Trash | BSD-3-Clause |
| reverse_geocoder | LGPL-2.1 (shipped as plain, replaceable `.py` files in `_internal`) |
| pi-heif (HEIC decoding) | BSD-3-Clause; bundles libheif and libde265 (LGPL-3.0) as separate, replaceable DLLs. Source: https://github.com/bigcat88/pillow_heif, https://github.com/strukturag/libheif, https://github.com/strukturag/libde265 |

The installer does not include the GPL-licensed x265 encoder (it uses decode-only pi-heif instead of pillow-heif).
Running from source (`Setup-PhotoSorter.bat`) installs pillow-heif from PyPI on your own PC.

## FFmpeg (videos, 0.6.0 and later)

The Windows installer includes `ffmpeg.exe`, `ffprobe.exe` and the FFmpeg libraries (`avcodec`, `avformat`,
`avfilter`, `avdevice`, `avutil`, `swresample`, `swscale`) as separate, replaceable DLLs in `_internal\ffmpeg`.
Photo Sorter runs them as separate programs to read video details, take frames, make preview strips and make
H.264 copies of videos browsers can't play. Your original videos are only read.

- FFmpeg (https://ffmpeg.org), (c) the FFmpeg developers, licensed under the GNU Lesser General Public License
  version 3 or later (LGPL-3.0-or-later). This build is configured with `--enable-version3` and without
  `--enable-gpl` or `--enable-nonfree`, so it contains no GPL-only or non-free parts; the CI build checks this.
  The full licence text is installed as `FFMPEG-LICENSE.txt` and in `_internal\ffmpeg\LICENSE.txt`.
- Binaries: the "win64-lgpl-shared" build of FFmpeg 8.1 from BtbN FFmpeg-Builds
  (https://github.com/FFmpeg-Builds/FFmpeg-Builds). Source code: https://github.com/FFmpeg/FFmpeg (release/8.1);
  the build scripts and the exact versions of every library linked into the build are in the FFmpeg-Builds
  repository. You may replace the DLLs/exes with your own build of the same FFmpeg version.
- Libraries built into those DLLs include, among others: Cisco OpenH264 (BSD-2-Clause; H.264 encoding when
  Windows' own encoder is unavailable), dav1d (BSD-2-Clause), libvpx (BSD-3-Clause), libopus (BSD-3-Clause),
  libvorbis/libogg (BSD-3-Clause), zimg (WTFPL) and others listed in the FFmpeg-Builds repository, each under
  its own permissive or LGPL licence.
- H.264 encoding prefers the encoder built into Windows (Media Foundation) or the graphics card (AMD AMF,
  NVIDIA NVENC, Intel Quick Sync) and only falls back to OpenH264.

Running from source: install FFmpeg yourself (put `ffmpeg` and `ffprobe` on PATH, or set
`PHOTOSORTER_FFMPEG_DIR`). Without it, Photo Sorter sorts photos and skips videos.
