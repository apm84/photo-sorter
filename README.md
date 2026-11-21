# Photo Sorter 0.2

Current release: 0.2.1.

A local Windows tool that sorts and de-duplicates a large photo collection (built for ~15,000 photos).
It **copies** photos into a clean folder tree and builds a searchable index. Your original folders are
only read, never moved, renamed or deleted. Everything runs on your PC; after the one-time model download
nothing goes online and nothing is uploaded.

## What it does

| Step | How |
|---|---|
| Scan | jpg, jpeg, png, heic/heif, webp, gif, bmp, tif/tiff in any number of source folders |
| Exact duplicates | SHA-256 of the file bytes |
| Date, camera, GPS | EXIF, then dates in filenames (`IMG_20190704_...`, `Screenshot_2020...`, WhatsApp), optional file-date fallback |
| Places | Offline reverse geocoding (GeoNames cities, about 150k places) to City, State / Country |
| Look-alike duplicates | Perceptual hashes (pHash + dHash), confirmed by image-AI similarity |
| Type sorting | Photo / Screenshot / Meme / Document / Graphic, from SigLIP 2 zero-shot plus rules (camera EXIF, phone screen sizes, filenames) |
| Tags | About 60 everyday tags (dog, beach, birthday cake, snow, ...) |
| Faces | Detects faces, groups them into people; you name each group once on the People page (names are kept on later runs) |
| Search | Local web page that filters as you type, with suggestions for people, places and tags, filters (year, month, place, state, type, person, tag, duplicates) plus plain-English search ("dog in the snow") |
| Clean up | Local web page to approve or change each duplicate decision with one key; removed copies go to the Recycle Bin, with Undo |

## Output folder

```
<Output>\
  Photos\2019\2019-07 Seattle, Washington\IMG_0001.jpg
  Photos\2019\2019-07\IMG_0042.jpg          (no GPS)
  Photos\Unknown Date\...
  Screenshots\2020\...   Screenshots\Unknown Date\...
  Memes\   Documents\   Graphics\
  Duplicates\group-00012\IMG_0001_small.jpg + _kept.txt (where the best copy went)
  Other\                                     (files that could not be read)
  People\<Name>\                             (optional hard links, made from the People page)
  _index\photosorter.sqlite                  (the index: files, hashes, EXIF, places, types, tags, faces, people, AI vectors)
  _index\reports\exact-duplicates.csv        (byte-identical copies: listed, not copied again; shows Clean up status)
  _index\_trash\                             (only if the Recycle Bin is unavailable: copies removed by Clean up)
```

In each duplicate group, the best copy stays in the main tree. Best means the highest resolution, then
having an EXIF date, a camera, GPS, the largest file, and a name without "copy" / "(1)". The other copies
go to `Duplicates\` for you to review. Byte-identical copies are only listed in the CSV, unless you tick
"Also copy byte-identical duplicates".

Running more batches into the same output folder is safe: duplicate group numbers never change between
runs (`Duplicates\group-00012` stays group 12), and a photo already in your library is never swapped out
silently. If a later batch brings a better copy of a photo you already have, the better copy goes to
`Duplicates\group-N\` and the group is flagged **needs review** on the Clean up page, where one key swaps it in.
Duplicates are always checked against every photo ever sorted into that output folder, even if next time you
only add a new folder (for example a new phone dump). New faces of people you already named are recognized and
added to them automatically.

## Install on Windows (DESKTOP-V0JAAMP)

1. Install **Python 3.12 (64-bit)** from python.org, and tick "Add python.exe to PATH".
   Python 3.13 or 3.14 is also accepted by `Setup-PhotoSorter.bat` if 3.12 is not installed.
2. Unzip or clone this folder, for example to `C:\PhotoSorter`.
3. Double-click **`Setup-PhotoSorter.bat`**. It:
   - creates `.venv` and installs the packages, including `onnxruntime-directml`, which lets the AI run on a DirectX 12 GPU (e.g. an AMD Radeon RX 570);
   - downloads the face models (about 40 MB);
   - prepares the image-AI model (SigLIP 2). Until a hosted copy exists, it runs `Export-Models.bat` once.
     That downloads PyTorch into a temporary `.exportenv` folder (about 1.5 GB) and converts the model.
     The conversion takes about 10 minutes. You can delete `.exportenv` afterwards.
4. Models are stored in `%LOCALAPPDATA%\PhotoSorter\models` (about 0.95 GB).

## Use

1. Double-click **`Run-PhotoSorter.bat`**.
2. Click **Add folder...** for each photo folder. Choose an empty **Output folder** on a drive with enough space (about the size of your photos).
3. Click **Start / Resume**. You can **Pause** or **Stop** at any time. Progress is saved in the index, so Start continues where it left off. Adding new photos later and pressing Start processes only the new ones.
4. When it finishes, click **Open search page**, or run `Run-Search.bat`. It opens `http://127.0.0.1:8765/`, which only your PC can reach.
   - **Search**: filters plus plain-English text. "Show in folder" opens File Explorer with the file selected.
   - **Search**: results update as you type. Typing a name you gave someone, a place or a tag shows suggestions
     (arrow keys + Enter, Esc closes); picking one filters to it. Typing a person's full name also works.
   - **People**: type a name under a face group and press Enter. Hover a face and click **x** if it is not that
     person (it will not be put back there). Merge two groups of the same person, hide strangers, and name single
     faces under "Faces not in any group". "Face grouping settings" changes how strict grouping is (names, merges and
     removed faces are kept). "Build People folders" creates `People\<Name>\` using NTFS hard links, which use no extra disk space.
   - **Clean up** (or the **Clean up duplicates** button, or `Run-Cleanup.bat`, `http://127.0.0.1:8765/cleanup`):
     one duplicate group at a time, side by side: the recommended keeper (green) and the copies marked duplicate
     (red), with resolution, file size, date, camera, GPS, paths and why the keeper was picked.
     - <kbd>Enter</kbd> approves (or applies your change), <kbd>&larr;</kbd>/<kbd>&rarr;</kbd> picks a different keeper,
       <kbd>1</kbd>-<kbd>9</kbd> toggles keep/remove per photo, <kbd>A</kbd> keeps all (not duplicates),
       <kbd>S</kbd> skips, <kbd>P</kbd> goes back, <kbd>U</kbd> undoes the last action.
     - **Approve all remaining...** applies every recommendation after a confirmation that shows what will happen.
     - Only the copies in the output folder are touched, never your originals. Removed copies go to the
       Windows Recycle Bin (or `_index\_trash\` if that is unavailable). A new keeper moves into the old keeper's
       `Photos\YYYY\YYYY-MM Place\` folder. Byte-identical copies that were never copied are just marked done.
     - Progress ("12 of 340 groups reviewed") is saved, so you can stop and come back. Don't clean up while a batch
       is running; the page waits until it finishes.
     - <kbd>D</kbd> (or **Delete all**) removes every photo in the current group, keeping none, after a confirmation
       that names how many copies will go to the Recycle Bin. Originals are never touched. Undo restores the group.
     - Each photo has a Keep / Remove toggle and a green Keep or red Remove badge. A live line shows
       "Keeping N, removing M". If you mark every photo Remove, Approve asks you to confirm Delete all.
     - After Undo, People folders are rebuilt automatically. Keep all no longer copies a second file for a
       byte-identical photo that is already in your library; it just marks that copy as kept.

GPU: the window shows "GPU (DirectML): available" when the GPU is usable. If the GPU fails, the tool switches to the CPU on its own. Face detection always runs on the CPU (OpenCV) and is fast.

Rough speed for 15k photos: hashing and EXIF take minutes, the image AI takes about 1 to 2 hours on a mid-range GPU such as an RX 570 (longer on the CPU only), and faces take about 30 to 60 minutes.

Command line (optional): `.venv\Scripts\python -m photosorter run --src "D:\Pictures" --out "E:\Sorted"`,
`... web --out "E:\Sorted" [--page cleanup|people]`, `... download-models`, `... providers`.

## Upgrading from 0.1

1. Close Photo Sorter and any search page window.
2. Unzip the new version over the old folder (for example `D:\PhotoSorter`), replacing files. Keep `.venv` and `models`.
3. Install the one new package (send2trash, for the Recycle Bin): double-click `Setup-PhotoSorter.bat` again (it skips
   models you already have), or run `D:\PhotoSorter\.venv\Scripts\python -m pip install send2trash`.
   Without it, Clean up still works and puts removed copies in `_index\_trash\`.
4. Your existing output folder and index carry over. The index is upgraded automatically the first time it opens
   (new tables and columns only; nothing is re-scanned or re-copied).

## Upgrading from 0.2 to 0.2.1

1. Close Photo Sorter and any search page window.
2. Unzip over the app folder (for example `D:\PhotoSorter\PhotoSorter-v0.1-src`), replacing files. Keep `.venv` and `models`.
3. You do not need to re-run Setup for this update if `send2trash` is already installed. Running
   `Setup-PhotoSorter.bat` again is safe; it skips models you already have.

## Optional .exe

`Build-Exe.bat` makes `dist\PhotoSorter\PhotoSorter.exe` (one folder, about 190 MB without models).
**The .exe leaves out HEIC support.** pillow-heif ships GPL-licensed codecs, which should not be bundled
into a closed program, so HEIC files land in `Other\`. Use `Run-PhotoSorter.bat` if you have iPhone photos.

## Models and licenses

Models download once to the local cache and are not bundled in this repo or the .exe.

| Use | Model | Size | License |
|---|---|---|---|
| Types, tags, near-duplicate check, text search (default) | google/siglip2-base-patch16-224, self-exported ONNX (vision fp32, text with int8 token table) | 372 + 514 + 34 MB | Apache-2.0 |
| Faces (default) | OpenCV Zoo YuNet 2023mar + SFace 2021dec | 0.2 + 37 MB | MIT / Apache-2.0 |
| Faces (optional upgrade) | InsightFace buffalo_l (SCRFD-10G + ArcFace R50) or buffalo_s | 289 / 128 MB zip | **Weights: non-commercial research use only.** User-downloaded, never bundled |
| Alternative embedder (optional) | OpenAI CLIP ViT-B/32 (Xenova ONNX) | 352 + 65 MB | MIT |
| Places | GeoNames cities1000 via `reverse_geocoder` | bundled with the package | Data: CC-BY 4.0, (c) GeoNames (geonames.org) |
| HEIC | pillow-heif (libheif, libde265, x265) | pip package | pillow-heif BSD-3. Bundled codecs LGPL/GPL, so it is not redistributed in the .exe |

No Ultralytics YOLO is used (AGPL). No Immich model files are used (no license).
The model layer is pluggable. Image embedders live in `photosorter/embedders.py` (`EMBEDDERS`) and face backends in `photosorter/faces.py` (`FACE_BACKENDS`).
Switching models re-runs only the affected step. Names you gave people are kept, but their faces need
re-grouping after you change the face model.

## Development

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # Linux, CPU
python -m pytest -q                                                   # unit + end-to-end, no models needed
PHOTOSORTER_TEST_MODELS=/path/to/models python -m pytest -q           # also runs the real-model tests
python tools/export_siglip2.py --out /path/to/models                 # needs requirements-export.txt + torch
```

`PHOTOSORTER_MODELS` overrides the model cache, and `PHOTOSORTER_HOME` overrides the app-data folder.
See `docs/AI_DEVELOPMENT_HANDOVER.md` for status and decisions.

## What's new in 0.3

Average-linkage face grouping at any library size with quality filters, learning from your People corrections
(reference faces, "not this person", a borderline queue), an unreadable-files report with retry, Albums (hard-link
folders, no extra disk space), a full-screen photo viewer, select circles with bulk "Add to album", a date range
filter, redesigned People and Clean up pages, and much faster browsing (infinite scroll, cached WebP thumbnails,
gzip). Index schema 3.

## What's new in 0.4

**Run in background (on by default).** Sorting runs at below-normal Windows priority on about half your CPU cores,
and thumbnail and sync work waits while you're using the keyboard or mouse (it carries on 30 seconds after you
stop, and never stalls for long). Pick the speed in the window's **Speed** list: *Run in background*
(recommended), *Lowest priority* (only spare CPU), or *Full speed* (every core, the 0.3 behaviour). Priority
changes straight away; thread limits apply from the next Start. Command line: `run --speed full`.

**Family sync to the family calendar Pi.** The library stays on this PC; a one-way copy of what the family needs
goes to the Pi's SSD over your home network, so everyone can browse it on their phones (the calendar's
`/family/` app) and the TV can show your albums as slideshows.
- **Since 0.7, only the albums you pick are sent.** On the **Albums** page, select albums (click the circle on an
  album; Shift-click selects a range), then choose **Actions > Sync with CalendarPeace**. Picked albums show a
  green **Synced** badge. Nothing else from the library is sent, and no single files. **Actions > Stop syncing**
  removes that album's photos from the Pi on the next sync (unless another synced album also has them), and
  **Actions > Delete...** deletes the selected albums (photos stay in your library). With no album picked, the Pi
  ends up with nothing and the Family sync page says "No albums selected for CalendarPeace".
- Sent: a catalog (dates, places, named people, albums), 400 px thumbnails, 1600 px previews, a face picture per
  named person and, optionally, the full-size sorted photos. Thumbnails and previews go first. Removed or
  deduplicated photos are deleted on the Pi at the end of each sync. Nothing on the PC is changed.
- Only new or changed files are sent, in 1 MB pieces; an interrupted sync carries on where it stopped. The Pi
  checks every file's SHA-256.
- **Speed limit** (default 2 MB/s, so the Pi's Wi-Fi stays usable), optional **time window** (e.g. 23:00-06:00),
  **Pause**, **Sync now**, and a status line on every page ("Family sync: 1,240 items left (3.2 GB)").
- It runs in its own low-priority background process: started by Sync now, after each sort run, or at Windows
  sign-in (`Install-Sync-Autostart.bat`). Closing the browser or Photo Sorter doesn't stop it.
- Setup: on the Pi run `sudo ./deploy/enable-family-photos.sh` once (calendar 0.1.17+), then in the calendar admin
  *Display > Family photos* press **Make a pairing code**. In Photo Sorter open **Family sync**, enter the
  calendar's address (e.g. `https://calendar.example.org`) and paste the code. The code is stored only in
  `%LOCALAPPDATA%\PhotoSorter\sync-secrets.json` and is only ever sent to a home-network (private) address.

**Verify before deleting originals.** The **Verify** page reads every file in your original folders (never
writes to them) and sorts it into: *safe, identical copy in the library*; *safe, removed on purpose as a
duplicate (the kept copy is in the library)*; *needs attention, no copy* (videos, other file types, new files,
errors); *needs attention, only a look-alike copy*. You get counts and sizes, a "100% safe" or "N files need
attention" banner, a CSV of every file, and **Rescue not-safe files**, which copies the ones that need attention
into `<output>\Rescued\<folder>\...` (copy only). Hashes are cached, so checking again only reads new or changed
files. Photo Sorter never deletes your original folders; keep one extra backup (external drive or cloud) for a
month before you delete them yourself.

**Clean up learns what you keep.** Each group where you keep some copies and remove others is remembered
(resolution, file size, format, camera date/details, location, edited/exported markers, WhatsApp/screenshot
folders, "(1)"/"copy" names, sharpness, date, folder path). After three such choices a small local model
ranks the suggested keeper in groups still waiting for review and in groups found later, with a reason such as
"Learned from your choices: you usually keep the unedited original". Resolution, size and camera data stay firm
defaults, so a few choices can't swing it wildly. Keep all, Delete all, Approve all and undone choices don't
count. *Clean up > Clean up tasks > What Clean up has learned* shows it and has **Reset learning**.

## Upgrading to 0.4

1. Close Photo Sorter, unzip `PhotoSorter-v0.4.0-src.zip` over your Photo Sorter folder (your `.venv`, models and
   settings stay). **No need to re-run Setup**: 0.4 adds no new packages.
2. Start it as usual. The index upgrades itself to schema 4 the first time (adds one table; nothing is removed).
3. Optional: set up Family sync (above) and run `Install-Sync-Autostart.bat` to start it at sign-in.

## Windows installer (v0.5)

- Download `PhotoSorterSetup.exe` from the latest release: https://github.com/apm84/photo-sorter/releases/latest
  (direct link: https://github.com/apm84/photo-sorter/releases/latest/download/PhotoSorterSetup.exe). It installs
  for your Windows user only (`%LOCALAPPDATA%\Programs\PhotoSorter`), with no admin prompt and no Python needed.
- The first start opens **setup inside the app**: it checks the graphics card (DirectML, GPU or CPU), the free space,
  and your photo and output folders, then downloads the AI models (about 950 MB, resumable, SHA-256 checked) from the
  [`models-v1` release](https://github.com/apm84/photo-sorter/releases/tag/models-v1) and runs a self-test. It can
  copy the models from an earlier source install instead of downloading them. Run it again from *Help > Run setup again*.
- **Updates:** *Help > Check for updates* (optionally once a day). The app reads this repo's GitHub Releases, shows
  the notes, downloads the installer, checks its SHA-256 against the release's `SHA256SUMS.txt`, then closes, upgrades
  silently and reopens. Your photos, `_index`, settings (`%LOCALAPPDATA%\PhotoSorter`) and models are never touched.
- The installer is not code-signed, so SmartScreen may warn the first time (*More info > Run anyway*).
- Builds come from `.github/workflows/release.yml` on a `v*` tag: tests on Windows, PyInstaller (one folder,
  onnxruntime-directml, decode-only pi-heif for HEIC), and an Inno Setup installer. The models are never in the installer.
- License: MIT (see `LICENSE`); models and libraries keep their own licenses (see `THIRD_PARTY_NOTICES.md`).

## Videos and Delete (v0.6)

- **Delete...** appears whenever photos or videos are selected (Search, Videos, albums, People). It moves only the
  selected sorted copies to the Recycle Bin (or `_index\_trash`), after a confirmation, and **Undo delete** brings
  them back. Originals are never touched.
- Videos (`.mp4 .mov .m4v .3gp .avi .mkv .webm .mts .m2ts .wmv .mpg`) are copied into the sorted folders with the
  same date/place rules. The **Videos** tab has a grid with hover previews and a player with scrub thumbnails,
  people, Up next and autoplay. Faces, content search, duplicates, albums and family sync work for videos too.
- Videos browsers can't play get a cached H.264 copy in `_index\video` (low priority); originals stay untouched.
- The Windows installer bundles FFmpeg (LGPL). From source, install FFmpeg yourself (`ffmpeg`/`ffprobe` on PATH or
  `PHOTOSORTER_FFMPEG_DIR`); without it, videos are skipped.
- Family sync sends full videos last, after all photos (needs Family Photos 1.1+). Turn it off with **Include videos**
  on the Family sync page.
- **Sync to Pi over the network** (Family sync page) is off by default, including for existing installs; while it is
  off nothing is sent to the Pi. Turn it on to use the photo (and video) sync described above.

## Search page reliability (v0.6.1)

- The search page starts when Photo Sorter opens (once an index exists) and stays up during a scan.
  Opening it waits until the page is actually listening, instead of a fixed 1.2 second delay that
  could fail with "refused to connect" while a scan was running. If port 8765 is already in use,
  the next free port is used. Failures are written to `%LOCALAPPDATA%\PhotoSorter\photosorter.log`
  and shown in the window, and the next click tries again.
