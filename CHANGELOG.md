# Changelog

## v2.2 – Evidence Explorer "Search Everything" rewritten as an index (`services/search_index.py`)
Before: every search re-walked the whole disk (3.2M items: 7.7-17 s per search, 630k items: 1.3-2.7 s).
Now: the folder is indexed once (630k items 0.96 s, 3.2M items 6.6 s), then every query is answered from memory:
630k items 3-110 ms, 3.2M items 25-575 ms (typing re-queries live, no Enter needed once the index exists).
Costs: memory (about 90 MiB per 630k items; 276 MiB at 3.2M) and the index is a snapshot - use **Re-index** if files changed.
- **Engine:** compact name blob + C-speed substring search, directory ranges for `parent:`/`path:`,
  suffix/prefix wildcard fast paths, queries run while the index is still being built (live results), new queries
  cancel older ones. Verified against a brute-force matcher on 1,800 random queries (incl. many tiny segments/merges).
- **New operators:** `|` (OR), `!` (NOT), `"quoted phrases"`, `regex:`, `size:` (`>10mb`, `1kb..5mb`), `dm:` (date modified),
  `file:`/`folder:` now combine properly. Invalid regex/size/date shows a message instead of failing.
- **Behaviour change (like Everything):** a plain word now matches the file/folder NAME, not the whole path (it used to
  match every file under a folder called "reports"). Use `path:reports` or a term with `/` for path matching.
  Wildcards must match the whole name (`*.pdf`, `rep?rt*`).
- **Bug:** `ext:pdf` matched a file literally named `pdf` (no extension); `ext:` with an empty value now means "no extension".
- **Bug:** hidden-file toggle now applies instantly in search mode (hidden state is stored in the index, folders inherit it).
- **Bug:** `test_filename_filter_sql` was failing in v2 (it tested a method that did not exist); replaced by tests that
  check `%` and `_` in the results filter are literal characters.
- **UI:** Re-index button, search-as-you-type, query help tooltip, status line shows index size/time and query time.
- **Tests:** 23 new tests (engine, brute-force equivalence, headless UI wiring). QtMultimedia crashes headless in some
  sandboxes (also in v2); the UI tests disable it.

## v2.1 – hash calculator performance + fixes (`services/hash_service.py`, `config/settings.py`, `classes/hash_result.py`)
Digests are byte-identical to v2 / `b3sum` / hashlib (verified on 1 GiB, 20,000 small and 200 medium files).
Speed (2-core test VM, warm cache): all 5 algorithms on a 1 GiB file 197 -> 346 MiB/s (1.75x);
BLAKE3 only 2,983 -> 6,354 MiB/s (2.1x, `b3sum` = ~8,200 MiB/s); 20k small files BLAKE3 23.7k -> 29k files/s.
- **Speed:** one thread per algorithm for files >= 1 MiB (hashlib/blake3 release the GIL); multi-threaded BLAKE3;
  zero-copy memory-mapped 16 MiB windows when BLAKE3 is selected (`HASH_USE_MMAP`); double-buffered `read()` with
  read-ahead for other algorithms; single `read()` for files <= 1 MiB; fewer syscalls (one `open`, `fstat` on the fd).
- **Bug:** progress updates were starved because the throughput callback and the progress loop shared one
  "last emitted" timer. Separate, throttled `tick()` now drives progress/ETA/throughput.
- **Bug:** progress % and ETA were per file count, so one huge file among tiny ones gave a useless ETA. Now byte-based.
- **Bug:** a live `SELECT` cursor was held across `commit()`s; the manifest is now read with keyset pagination.
- **Bug:** any non-`OSError` failure on one file (e.g. `ValueError`) aborted the entire job; it is now a per-file error row.
- **Bug:** algorithms other than the five stored in `HashResult` were computed then silently discarded; they are now
  rejected up front. A missing `blake3` package is reported once, clearly, instead of failing per file.
- **Bug:** `blake3` was a dynamic attribute on `HashResult`; it is now a real dataclass field.
- **Compat:** MD5/SHA-1 are created with `usedforsecurity=False` so they work on FIPS-enabled Python builds.
- **Docs:** `HASH_CHUNK_SIZE` comment said 1 MiB while the value was 4 MiB; now 16 MiB and the comment is accurate.
- **Tests:** +9 tests (all size boundaries x mmap/read x threaded/not, BLAKE3 official vector, truncated file, FIFO,
  worker order, error row, cancel). `test_search_logic.test_filename_filter_sql` was already failing in v2 and is untouched.
- **Note:** mmap is a speed/robustness trade-off (as in `b3sum`): if a mapped file is truncated or its drive is
  unplugged mid-read, the OS can raise SIGBUS instead of a Python error. The code re-checks the size per window, but
  set `HASH_USE_MMAP = False` for network shares / removable media if that matters more than the ~1.4x BLAKE3 gain.

## v2 – defect-fix release
Fixed (each has a test or a smoke-test check):
- **Hashing:** a cancelled hash was reported as a successful result (partial digests). Cancelled and failed reads now
  produce no digests; files that change during the read are flagged; default chunk 1 MiB.
- **Shutdown:** closing the window did not stop hashing/search (Qt sends `closeEvent` only to top-level windows).
  `HashToolsWindow.closeEvent` now cancels workers and removes temp data. A race in which a finished job's cleanup
  cleared a newer job's thread reference was fixed.
- **Scanning:** the selected folder was walked 5 times per selection; it is now walked once and the list passed on.
  Ignore rules (`.git`, `__MACOSX`, ...) apply only below the selected folder, not to its parents.
- **Metadata:** "Created" was really inode-change time on Linux/macOS; labels are now accurate and times show UTC + local.
- **LibreOffice:** on timeout only the launcher was killed; the whole process tree is now killed.
- **Report:** SHA-512 (computed and shown in the app) is now in the PDF; warnings/errors are printed per file.
- **Explorer:** one page-size constant (150) drives the UI labels and docs; old search databases are deleted on a new search.
- **Housekeeping:** removed unused `FileState`, unused imports and unused dependencies; `reports/` is no longer created at import.
- **Docs:** README/notes rewritten; contradictions about page size and Office rendering removed.

Earlier work (kept for history): PDF report naming and layout (Khmer-safe fonts, logo size, landscape tables),
folder upload ignores OS cache files, Everything-style search backed by SQLite, debounced search previews,
centralized LibreOffice conversion service, bounded image preview decoding.
