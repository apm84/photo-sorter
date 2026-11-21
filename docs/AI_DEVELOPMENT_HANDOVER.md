# Handover: Photo Sorter

## Status (v0.2.1, 2026-11-14)
- v0.2.1 adds: per-group Delete all (`cleanup.delete_group`, empty keep set only with `allow_empty`, Recycle Bin,
  undo, `keeper_id` NULL), clearer Keep/Remove toggles (Approve with zero keeps opens the Delete all confirm),
  Setup falls back to Python 3.13 then 3.14, undo rebuilds People folders, Keep all does not copy a second
  byte-identical file. Endpoint: `POST /api/cleanup/group/{gid}/delete-all` with `confirm: true`.
- Tests: see `tests/test_cleanup.py` (delete all + undo, no-keeper group survives a later incremental batch,
  keep-all identical skip, undo rebuilds People links, web API). Normal apply still refuses an empty keep set.
- Not yet run on Windows.

## Status (v0.2.0, 2026-11-14)
- v0.2 adds: the Clean up page (`/cleanup`, `photosorter/cleanup.py`), face naming improvements (`/people`), dynamic
  search with autosuggest, stable duplicate group ids across batches, and an additive schema migration (version 2).
- Tests: 29 pass without models (+2 real-model tests with `PHOTOSORTER_TEST_MODELS`). New: `tests/test_cleanup.py`.
- E2E on the box: a v0.1 index of 44 real files upgraded in place; approve-all + undo restored the output byte for byte,
  originals untouched; Clean up, People and Search pages driven in headless Chrome (keyboard, dialog, autosuggest).

### v0.1 status
- (Superseded: the repo is now public at github.com/apm84/photo-sorter.)
- Tests: 17 pass on Linux with the CPU provider (15 without models, plus 2 real-model tests when `PHOTOSORTER_TEST_MODELS` is set).
- End-to-end run on 44 real and synthetic files (HEIC, webp, png, jpg): tree, index and web search all work. A PyInstaller build was smoke-tested on Linux.
- Not yet run on Windows or DirectML (no Windows machine available to the agent).

## Architecture
- `photosorter/pipeline.py`: the stages scan, analyze, geocode, embed, classify, faces, people, dedupe and organize. Per-file stage flags in SQLite make runs resumable and incremental. Pause and stop are checked between batches.
- `imaging.py` handles EXIF, dates, hashes and decoding. `geo.py` does offline reverse geocoding. `classify.py` combines rules with AI probabilities. Low-confidence non-photo guesses fall back to photo (UNSURE=0.45).
- `embedders.py` holds the pluggable image/text models: SigLIP2 (default) and CLIP. `faces.py` holds the pluggable face backends: yunet_sface (default) and InsightFace buffalo_l/s (optional, non-commercial).
- `models.py` is the registry, downloader and ORT sessions. It uses DirectML when available (mem-pattern off, sequential execution) and falls back to the CPU if a GPU session errors.
- `web/app.py` is the FastAPI app, local only: the middleware rejects non-loopback clients, and POST requests need an `X-PhotoSorter: 1` header (a CSRF guard). The static pages are in `web/static`.
- `gui.py` is a Tk window. `cli.py` provides the commands run, web, download-models and providers.

- `cleanup.py`: duplicate review decisions. Applies keep/remove to OUTPUT copies only (`_guard` refuses absolute paths,
  `..`, `_index`, anything outside the output, and any path equal to / same file as the original). Removal =
  send2trash (Recycle Bin), else `_index/_trash/action-N/`. Each apply/approve-all is one `review_actions` row with
  ops + prior DB state; undo reverses ops (a recycled copy is restored by re-copying the untouched original).
  `file_review` holds per-file keep/remove. Refuses to act while a pipeline heartbeat (meta `pipeline_heartbeat`) is fresh (<90 s).
- `db.migrate()`: additive ALTER TABLE for older indexes (`dup_groups.suggested_keeper_id/note`, `faces.locked/not_person`)
  plus new tables `file_review`, `review_actions`. Runs on every connect; idempotent.

## Decisions
- Copy, never move. Byte-identical copies are listed in the CSV rather than copied (a toggle changes this). Near duplicates are copied to Duplicates/group-N.
- Each identical-content group has one representative (`rep_id`). Only it gets AI vectors and faces. The representative prefers names without "copy" or "(1)".
- SigLIP 2 is exported by `tools/export_siglip2.py`. The text tower int8-quantizes only the Gather (token table): cosine is at least 0.996 against fp32, versus 0.92 to 0.98 when the MatMuls are also quantized.
- Tags: a softmax over the vocabulary of at least 0.2, plus a sigmoid of at least 5e-4, up to 3 tags.
- The face cluster eps comes from the backend (SFace 0.55, ArcFace 0.5), with min_samples 2. Named people survive re-clustering through majority vote and centroid matching.
- Incremental batches: the `files` table keeps every photo ever indexed for an output folder, and dedupe/people run
  over all of them, so a batch that selects only a new source folder still matches earlier photos.
- Duplicate group ids are stable (`assign_stable_group_ids`): a new group containing members of an old group keeps
  the old id (most members wins, a split keeps the id on the larger part); new groups get ids above meta
  `max_dup_group`, never reused. `choose_keeper`: if any member is already in the main tree or user-kept, the keeper
  stays one of those; a better newcomer becomes `suggested_keeper_id` ("needs review"), and organize puts it in
  `Duplicates/group-N`. Files the user removed are never picked as keeper again.
- Keeper swap target folder: the old keeper's library folder (keeps albums consistent); otherwise `main_dest()`
  (the same rules organize uses). Removals run before promotions so a same-named new keeper takes the old name.
- Face grouping: `cluster_faces(db, eps, min_samples, method)` with DBSCAN (default) or average-linkage
  agglomerative (only up to 8000 faces, O(n^2) memory). User work is never undone: faces with `locked=1` (merged,
  assigned, removed) are skipped; `not_person` stops a removed face rejoining that person; named and hidden people
  survive. Faces left unassigned (e.g. a single new face in a later batch) join the closest named/kept person when
  cosine similarity >= max(0.4, 1 - eps). Strictness set on the People page is saved in meta `face_cluster`.
- Search: an exact person name in the text box filters to that person; partial names match in keyword mode.
  `/api/suggest` returns people/places/tags (LIKE, prefix first). The page debounces 200 ms and aborts stale fetches;
  SigLIP text vectors are cached (256 entries).
- The .exe excludes pillow-heif (GPL codecs), so the source install is the primary path.

## Known gaps and next steps
- The SigLIP 2 ONNX has no hosted download yet. Either publish the exported files as a release asset (Apache-2.0 allows this with the NOTICE) and set `PHOTOSORTER_MODEL_BASE_URL`, or keep `Export-Models.bat`.
- DirectML speed and stability on the RX 570 are untested. The fallback to the CPU is automatic.
- Meme detection has not been checked on real memes. Game screenshots and character crops land in Screenshots or Memes.
- Photos without GPS don't inherit a place from GPS photos taken on the same day.
- Burst shots and similar shots are not grouped, only true near-duplicates.
- If a later run merges two old groups, files already copied to the second group's `Duplicates/group-M` folder stay
  there (the Clean up page still handles them correctly, it works from each file's stored path).
- Clean up does not re-create `People\<Name>` hard links after undo (press "Build People folders" again).
- Keep-all on byte-identical copies copies the extra originals into the library with " (1)" names.
- Fixed in 0.2.1: undo calls `build_people_folders` so People hard links come back after a restore.
- Fixed in 0.2.1: Keep all marks a never-copied byte-identical photo as kept without copying a second file.
- Untested on Windows: the real Recycle Bin path (send2trash) and Explorer behavior; covered on Linux by a fake.
- There's no object boxes or detector. Tags come only from the embedder (YOLOX or RT-DETR could be added if boxes are needed).
- AI text search ranks every candidate and has no relevance cutoff yet.
- Windows long paths (over 260 characters) depend on the OS LongPathsEnabled setting.

## Status (v0.4.0, 2026-11-14)

- `background.py`: run modes background / lowest / full. Windows priority via ctypes `SetPriorityClass`
  (BELOW_NORMAL / IDLE), thread budget = half the cores (pipeline pools, ORT intra-op threads, cv2, BLAS env vars set
  before numpy import in `cli.main`/`gui.main`). `IdleGate` uses `GetLastInputInfo`; thumbnails, verify hashing and
  sync preview generation call `wait()` (max 20 s per call, so work trickles on). Settings.run_mode, idle_resume_s.
- `prefs.py`: learning from Clean up. Table `keep_decisions` (schema 4) stores kept-over-removed pairs with features
  frozen at decision time, weight 1/(kept x removed) per group. Recorded only in `cleanup.apply_decision` when some
  copies are kept and some removed (not Keep all / Delete all / Approve all); `undo_action` deletes them. MAP
  logistic regression on feature differences, Gaussian prior centred on weights mimicking `quality_key`, per-feature
  precision (firm for resolution/size/camera, loose for habit features). Active after 3 groups. Used by
  `dedupe.pick_keeper(model)` in `stage_dedupe` (future groups; anchored library keepers are still never silently
  swapped) and by `cleanup.group_detail` for pending groups when P >= 0.6, with a "Learned from your choices" reason.
- `verify.py`: read-only check of the originals. Hash cache + results in `_index/verify.sqlite` keyed by
  path/size/mtime; library hashes cover the whole output tree (hard links counted once via st_ino). Buckets
  safe_copy / safe_deduped (file_review=remove and a kept member's hash exists in the library) / missing / near_only
  (same dup group, no decision). CSV in `_index/reports/verify-originals.csv`; `rescue()` copies buckets 3-4 to
  `<output>/Rescued/<source name>/<rel>` (copy2 via .partial, never overwrites, skips identical).
- `sync.py`: one-way PC -> Pi. Config `_index/sync.json`, token in app-data `sync-secrets.json`, state/control JSON in
  `_index`, worker process `python -m photosorter sync --out` with a lock file (`_index/sync.lock`, msvcrt/fcntl).
  Content-named media: `<orig sha>.t|p.<webp|jpg>` (derived, content hash sent separately), `<sha>.o.<ext>`,
  `<sha>.f.jpg` (face), `<sha>.catalog.ndjson.gz`. Protocol implemented by the family calendar receiver (`family_calendar/family_photos.py`)
  (`/api/photosync/status|check|upload|catalog|commit`, Bearer pairing token, LAN-only, no Origin/Cookie). Client
  refuses non-private addresses. Token bucket throttles the body generator (16 KB pieces).
- Web: `/verify`, `/sync` pages, header sync chip, `/api/verify/*`, `/api/sync/*`, `/api/cleanup/learning[/reset]`.
- Tests: `tests/test_v04.py` (19), including an end-to-end sync against a real family calendar receiver when its
  source is next to this repo (`FAMILY_CALENDAR_SRC`).
- Untested on real hardware: Windows priority/idle APIs, pythonw detached worker, Startup-folder autostart, Wi-Fi
  throughput on the Pi 2, exFAT/NTFS group-write mount options for the sync folder.

## Status (v0.5.0, 2026-11-14)

- Public repo: https://github.com/apm84/photo-sorter (MIT). The history was published as a fresh, sanitized initial
  commit; the earlier local history isn't public.
- `onboarding.py` holds the logic and `gui_setup.py` the Tk wizard (Welcome, This PC, Folders, AI models, Self-test, Done).
  It is shown by `gui.main()` when `%LOCALAPPDATA%\PhotoSorter\onboarding.json` isn't completed, or when the chosen
  models are missing. Set `PHOTOSORTER_SKIP_ONBOARDING=1` to bypass it.
  - The GPU probe (`photosorter gpu-probe --json f`) runs DXGI `EnumAdapters1`/`GetDesc1` through ctypes in a child
    process, falls back to CIM `Win32_VideoController`, and also lists the ORT providers. `recommend()` picks GPU when
    DirectML is present and a non-software adapter exists.
  - The self-test (`photosorter selftest [--cpu] --json f`) also runs in a child process. If the GPU run fails, it
    retries on the CPU and switches the wizard to CPU.
- `config.py` keeps the models folder in `app.json` (`models_dir`). The installed exe (`sys.frozen`) never uses a
  models folder inside the install dir. `install_dir()` is the exe folder when frozen.
- `models.download_file`: `.part` plus HTTP Range resume, restart when the range is ignored, mirrors, retries with
  backoff, pinned SHA-256 and size, and cancel. `MODELS` pins hashes for SigLIP 2 and YuNet/SFace. The default
  base is the `models-v1` release (`PHOTOSORTER_MODEL_BASE_URL` still overrides it). `import_models_from()` copies
  hash-checked files from an old models folder.
- `updater.py` reads the Releases API `releases/latest` (no auth) and needs an asset named `PhotoSorterSetup*.exe`.
  The SHA-256 comes from `SHA256SUMS.txt` and is cross-checked against the GitHub asset `digest` when present; a
  mismatch or a missing hash means no update is offered. The installer is downloaded to `%LOCALAPPDATA%\PhotoSorter\updates`
  and run with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /RELAUNCH=1` (the Inno `[Run]` Check
  relaunches the app). The daily check uses `app.json` `update_check` and `update_last_check`.
- Sync worker when frozen: `PhotoSorter.exe sync --out ...`. `packaging/launcher.py` points None stdout/stderr at
  `console.log`.
- Packaging: `packaging/photosorter.spec` (pi-heif bundled, pillow-heif and the GPL x265 encoder excluded,
  reverse_geocoder as plain .py files for LGPL) and `packaging/PhotoSorter.iss` (per-user, `{localappdata}\Programs\PhotoSorter`,
  `[InstallDelete] {app}\_internal`, uninstall removes the sync Startup .cmd).
- CI: `.github/workflows/ci.yml` (Linux tests) and `release.yml` (windows-latest: tests, PyInstaller, exe smoke test,
  Inno Setup, silent install/upgrade/uninstall test, Release with `SHA256SUMS.txt`, notes from `docs/releases/<tag>.md`).
- Tests: `tests/test_v05.py`.
- Untested on real hardware: DXGI probe on an RX 570, DirectML self-test, SmartScreen, the in-app silent upgrade
  while the sync worker runs, and Inno closing the running app.

## 0.6.0: videos and Delete on selections

- Delete: `cleanup.delete_files(db, out, ids)` (one `_Action` "delete N selected", so `/api/cleanup/undo` reverts it):
  `_guard` each copy, drop People/album hard links, `remove_copy` (send2trash or `_index/_trash`), set
  `dest_path=NULL, organized=1, is_keeper=0`, `file_review='remove'` (so runs don't re-copy and Verify says
  `safe_deleted`), clear a group's `keeper_id` if it was the keeper. API: `POST /api/files/delete {file_ids, confirm}`
  (X-PhotoSorter header). UI: `PS.selectionBar` adds Delete.../Undo delete for every grid; `PS.deleteFiles`, `PS.undoLast`.
  Search/facets hide removed files (`NOT_REMOVED`).
- Videos live in `files` (`media='video'`, `type='video'`, `duration`, `vcodec`, `acodec`, `playable`, `live`); per-video
  cache data in the new `videos` table (`sig` = dHash per fixed frame, `hdr`, sprite layout, `proxy` state). Schema 5.
  `faces.frame_t` = the second a video face was seen.
- `photosorter/video.py`: finds ffmpeg/ffprobe (`PHOTOSORTER_FFMPEG_DIR` ("none" disables), `<install>/ffmpeg`,
  `_MEIPASS/ffmpeg`, `packaging/ffmpeg/bin`, PATH); runs them hidden at lowered priority (`_popen_kw`). `probe()`
  (duration, rotation-aware size, codecs, HDR, `creation_time` / Apple `creationdate`, ISO 6709 `location`/`©xyz`),
  `browser_playable()` (H.264 + AAC/MP3, MP4/MOV, yuv420p, SDR), keyframe-seek `frame()`/`frames()` (HDR tonemapped
  with zscale when present), `signature()`/`same_video()` (duration within max(1 s, 2%) and median dHash distance <= 10),
  `make_sprite()` (<= 100 tiles, about 1/s, 160 px wide, 10 per row), `make_proxy()` (720p H.264, encoder order
  h264_mf, h264_amf, h264_nvenc, h264_qsv, libopenh264, libx264, each probe-tested; `PHOTOSORTER_H264_ENCODER`),
  `Jobs` (one low-priority thread for on-demand sprites/proxies from the web UI).
- Pipeline: scan includes `VIDEO_EXTS` when `use_videos` and ffmpeg exist; analyze uses `analyze_video`; Live Photo
  clips (same stem as a photo, <= 4.5 s) get `live=1`; embed = mean SigLIP 2 vector over `sample_times()` frames;
  faces = YuNet/SFace on the same frames, one face per person per video (`pick_video_faces`, cosine >= 0.45), crops in
  `_index/thumbs/faces`; dedupe adds `_video_pairs` (signature + embedding cosine >= 0.85 when both exist); new stage
  `videos` makes sprites then proxies (respects `IdleGate`). Organize is unchanged (videos sort like photos).
- Web: `/videos` (`videos.html/js`), `/watch?id=` (`watch.html/js`; Up next list from sessionStorage `ps-upnext`, else
  `/api/search?media=video`), `/api/video/{id}`, `/video/{id}/stream` (original if playable, else proxy; Range via
  Starlette `FileResponse`), `/video/{id}/original`, `/video/{id}/sprite.jpg`, `/video/{id}/thumbs.vtt`.
  `/api/search` gains `media=video|photo` and `hide_live`. The Videos nav link is added by `common.js` so the older
  page templates stay as they were.
- Sync: catalog records for videos carry `mv:"video"`, `du`, `pb`, `v` (`<sha>.v.<ext>`, the full video) and `x`
  (`<sha of proxy>.x.mp4`, only for non-playable videos with a proxy). Order f, t, p, o, x, v (videos last). Only sent
  when the Pi's `/api/photosync/status` says `videos: true` (Family Photos 1.1) and `include_videos` is on.
  Live Photo clips are skipped unless they are in an album.
- Packaging: release.yml downloads BtbN `ffmpeg-n8.1-latest-win64-lgpl-shared-8.1.zip`, refuses GPL/nonfree builds,
  copies `bin` (no ffplay) to `packaging/ffmpeg/bin`; the spec bundles it into `_internal/ffmpeg`; Inno installs
  `FFMPEG-LICENSE.txt`. The selftest JSON reports `ffmpeg` and `h264_encoder`. ci.yml installs Ubuntu's ffmpeg.
- Tests: `tests/test_v06.py` (video tests skip without ffmpeg; the content-search test needs `PHOTOSORTER_TEST_MODELS`).

### v0.6.0: network sync switch
- `sync.json` key `network_sync` (default False; existing files without it read as False). `Syncer.run`,
  `request_sync`, `request_after_run` and `daemon_main` do nothing while it's off; `status()["line"]` says "off".
  The checkbox is injected by `sync.js` (sync.html is unchanged). Test: `test_network_sync_switch_off_by_default`.

### v0.6.1: search page server
- New `photosorter/webserver.py`: `SearchServer` starts uvicorn in a daemon thread, waits until the
  port accepts connections (up to 30 s), tries the next port if 8765 is taken, catches `SystemExit`
  from uvicorn bind failures, and logs a full traceback. `boost_thread_priority()` (Windows
  THREAD_PRIORITY_HIGHEST) keeps the server thread scheduled during a background/lowest-priority scan.
- GUI: the server starts when the window opens if the output folder already has an index, and again
  after a run finishes (`finished` → `ensure_web_server`). `open_web` logs "Starting search page..."
  and waits for readiness before opening the browser; failures go to `photosorter.log` and a
  message box, and state is reset so the next click retries. CLI `web` (`serve()`) also waits until
  ready and falls over to the next port.
- Tests: `tests/test_webserver.py`.

### v0.7.0: Family sync sends only albums picked for CalendarPeace
- Owner decision: Family sync must send ONLY albums selected in the Photo Sorter browser UI; never the whole library
  and never single files.
- DB: `albums.sync_to_pi INTEGER DEFAULT 0` (in the CREATE TABLE and in `MIGRATIONS` for older indexes).
  `albums.list_albums`/`get_album` return `synced`; `albums.set_sync(db, out, ids, on)` sets it.
- Web: `POST /api/albums/action` `{album_ids, action: "sync"|"unsync"|"delete", confirm}` (X-PhotoSorter header;
  delete needs `confirm`). The Albums list uses one selection (same `.card-sel` circle as photo grids, Shift-click
  range) plus an Actions select (Sync with CalendarPeace, Stop syncing, Delete...) built in `albums.js`; a green
  "Synced" badge marks picked albums (also next to the title inside an album). albums.html is unchanged.
- `sync.build_plan`: rows = files in albums with `sync_to_pi=1` (same keeper/rep/not-removed filters as before);
  albums = only picked albums; people = only named people in those photos; returns `synced_albums`. The old
  "all non-screenshot photos + every album" scope is gone; `include_screenshots` is no longer used (its checkbox
  still shows on the sync page because existing UI text was left unchanged).
  No album picked = empty catalog, so the receiver's commit removes everything it holds under media/.
- `sync.status()` adds `albums_selected` and `no_albums_message`; `status_line` says
  "Family sync: no albums selected for CalendarPeace" (page chip and sync page).
- Family Photos receiver (family-photos 1.1.0) needed no change: it accepts an empty catalog.
- Tests: `test_build_plan_sends_only_albums_picked_for_calendarpeace`, `test_album_actions_endpoint`,
  `test_albums_sync_column_is_added_to_old_indexes`; the end-to-end sync tests pick albums via
  `sync_album_of_everything()` and check Stop syncing / no-albums behaviour against the real receiver.

### v0.8.0: suggested albums
- New `photosorter/suggest.py` (tables `album_suggestions`, `album_suggestion_items` created by `db.connect`).
  `eligible_photos`: `type='photo'`, `media IS NULL`, dated (not `date_source='mtime'`), kept copy, not removed in
  Clean up, has a sorted copy. Screenshots/memes/documents/graphics (SigLIP prompt types) and videos never count.
- `cluster`: split on > 3 h gap or > 25 km jump between geotagged photos; merge same-day neighbours <= 8 h apart at
  the same place (centroids <= 2 km, else same main city); drop runs < 6 photos.
- `find`: skips events with >= 60% of photos already in albums and events overlapping >= 50% with any existing
  suggestion (open, kept or dismissed), so dismissed ones never come back and nothing is duplicated.
- Names (`name_for`): scene from stored image tags (`tags` table, TAG_VOCAB; needs >= 2 photos and 25%; `SCENES`
  map with `PRIORITY`), "in <city>" from reverse-geocoded place, "with" up to 3 named visible people seen in >= 2
  photos, then the date or date range. Fallbacks: "Day in/Trip to <city>", "Get-together", "Outing"/"Trip".
  Only real data; no embedding or model run (reuses what the sort stored).
- Web: `/api/suggestions` (list), `/find`, `/{id}`, `/{id}/remove` (file_ids), `/{id}/rename`, `/{id}/keep`
  (creates the album via `albums.create_album` + `add_files`, hard links), `/{id}/dismiss`. `/api/search` gains
  `suggestion=<id>` so the review grid is the normal PhotoGrid. UI is built in `albums.js` (albums.html unchanged):
  Suggested section + Find suggestions under the albums, review view with one selection + Actions > Remove from
  suggestion, Rename, Keep as album, Dismiss. Merge of suggestions was skipped.
- GUI: `after_run_suggest()` runs `suggest.find` in a below-normal-priority thread after each sort run.
- Tests: `tests/test_v08.py` (clustering, exclusions, naming, album/dismiss memory, endpoints, a real index).
