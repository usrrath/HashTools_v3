"""Everything-style file index and query engine (no Qt dependency).

Design (why it is fast)
-----------------------
* The tree is scanned ONCE into compact, immutable *segments*. A segment stores all
  its case-folded names in one big string (one name per line) plus an offsets array,
  so a plain substring search is a C-speed ``str.find`` over the blob instead of a
  Python loop over millions of objects.
* Directory entries are appended contiguously, so every directory owns one range of
  entry ids. ``parent:`` / ``path:`` queries become range unions, not scans.
* ``ext:pdf`` / ``*.pdf`` / ``report*`` search the line ends / line starts of that same blob,
  so no per-entry tables have to be built during the scan (build time stays low).
* Queries are answered from memory in milliseconds and can run while the scan is
  still going (segments are published as they are completed).

Query language (Everything-inspired; space = AND, ``|`` = OR, ``!`` = NOT)
--------------------------------------------------------------------------
    report              file/folder NAME contains "report"          (case-insensitive)
    "annual report"     quoted phrase
    *.pdf   rep?rt*     wildcards, matched against the whole name
    dir/name            a term containing "/" is matched against the full PATH
    ext:pdf;docx        extension (files only); "ext:" alone = files without extension
    parent:cases/2024   immediate parent folder equals / ends with the given path
    path:cases/2024     full path contains
    file:  folder:      only files / only folders
    regex:^IMG_\\d+     regular expression on the name
    size:>10mb  size:1kb..5mb  size:=0
    dm:>2024-01-31  dm:2024-05-03  dm:2024-01-01..2024-02-01   (date modified)
    !draft              exclude
    cat dog | bird      (cat AND dog) OR bird
"""
from __future__ import annotations

import bisect
import datetime
import os
import re
import threading
import time
from array import array
from collections import deque
from itertools import accumulate, count, repeat
from operator import add

IS_DIR = 1
IS_HIDDEN = 2

_WINDOWS = os.name == "nt"
_FLUSH_MIN = 4096
_FLUSH_MAX = 262144
_MERGE_WHEN = 48          # merge the newest segments once there are this many
_MERGE_COUNT = 16


class QueryError(ValueError):
    """Raised for an invalid query (for example a bad regular expression)."""


# --------------------------------------------------------------------------- helpers
def fold(name: str) -> str:
    """Case-fold a name for matching; newlines would break the one-name-per-line blob."""
    folded = name.casefold()
    return folded.replace("\n", " ") if "\n" in folded else folded


def ext_of(folded_name: str) -> str:
    dot = folded_name.rfind(".")
    return folded_name[dot + 1:] if dot > 0 else ""


def _norm(text: str) -> str:
    return text.replace("\\", "/").casefold()


_SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "kib": 1024, "m": 1024 ** 2, "mb": 1024 ** 2,
               "mib": 1024 ** 2, "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3,
               "t": 1024 ** 4, "tb": 1024 ** 4, "tib": 1024 ** 4}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*$")
_OP_RE = re.compile(r"^(<=|>=|<|>|=)?(.*)$")


def _parse_size(text: str) -> int:
    match = _SIZE_RE.match(text.casefold())
    if not match or match.group(2) not in _SIZE_UNITS:
        raise QueryError(f"Invalid size: {text!r} (examples: size:>10mb, size:1kb..5mb)")
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2)])


def _parse_date(text: str):
    """Return (start_epoch, end_epoch_exclusive) for a day or a full timestamp."""
    text = text.strip()
    for fmt, span in (("%Y-%m-%d", datetime.timedelta(days=1)), ("%Y-%m-%d %H:%M", datetime.timedelta(minutes=1)),
                      ("%Y-%m-%d %H:%M:%S", datetime.timedelta(seconds=1))):
        try:
            start = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return start.timestamp(), (start + span).timestamp()
    raise QueryError(f"Invalid date: {text!r} (use YYYY-MM-DD)")


def _range_predicate(value: str, parse_one):
    """Build f(number) for '>x', '>=x', '<x', '<=x', '=x', 'x', 'a..b'."""
    value = value.strip()
    if ".." in value:
        low_text, _, high_text = value.partition("..")
        low = parse_one(low_text)[0] if low_text.strip() else None
        high = parse_one(high_text)[1] if high_text.strip() else None
        return lambda n: (low is None or n >= low) and (high is None or n < high)
    op, rest = _OP_RE.match(value).groups()
    lo, hi = parse_one(rest)
    if op == ">":
        return lambda n: n >= hi
    if op == ">=":
        return lambda n: n >= lo
    if op == "<":
        return lambda n: n < lo
    if op == "<=":
        return lambda n: n < hi
    return lambda n: lo <= n < hi


def _glob_to_regex(pattern: str) -> re.Pattern:
    parts = []
    for ch in pattern:
        parts.append("[^\n]*" if ch == "*" else "[^\n]" if ch == "?" else re.escape(ch))
    return re.compile("".join(parts))


# --------------------------------------------------------------------------- query model
class Term:
    __slots__ = ("kind", "neg", "value", "rx", "values", "pred")

    def __init__(self, kind, value="", neg=False):
        self.kind, self.value, self.neg = kind, value, neg
        self.rx = self.values = self.pred = None


def _tokenize(query: str):
    tokens, current, quoted, started = [], [], False, False
    for ch in query:
        if ch == '"':
            quoted, started = not quoted, True
        elif ch.isspace() and not quoted:
            if started:
                tokens.append("".join(current))
            current, started = [], False
        elif ch == "|" and not quoted:
            if started:
                tokens.append("".join(current))
            tokens.append("|")
            current, started = [], False
        else:
            current.append(ch)
            started = True
    if started:
        tokens.append("".join(current))
    return tokens


def _make_term(token: str) -> Term | None:
    neg = False
    if token.startswith("!") and len(token) > 1:
        neg, token = True, token[1:]
    key, sep, value = token.partition(":")
    key = key.casefold()
    if sep and key in ("ext", "parent", "path", "file", "folder", "regex", "size", "dm"):
        if key == "ext":
            term = Term("ext", neg=neg)
            term.values = tuple(v.strip().lstrip(".").casefold() for v in re.split(r"[;,]", value)) if value else ("",)
            return term
        if key == "parent":
            v = _norm(value).rstrip("/")
            return Term("parent", v, neg) if v else None
        if key == "path":
            v = _norm(value)
            return Term("path", v, neg) if v else None
        if key in ("file", "folder"):
            return Term(key, neg=neg)
        if key == "regex":
            term = Term("regex", value, neg)
            try:
                term.rx = re.compile(value, re.IGNORECASE)
            except re.error as exc:
                raise QueryError(f"Invalid regular expression: {exc}") from exc
            return term
        if key == "size":
            term = Term("size", value, neg)
            term.pred = _range_predicate(value, lambda t: (_parse_size(t), _parse_size(t) + 1))
            return term
        term = Term("dm", value, neg)
        term.pred = _range_predicate(value, _parse_date)
        return term
    value = _norm(token)
    if not value:
        return None
    has_wild = "*" in value or "?" in value
    if "/" in value:
        if has_wild:
            term = Term("pathglob", value, neg)
            term.rx = _glob_to_regex(value)
            return term
        return Term("path", value, neg)
    if has_wild:
        term = Term("glob", value, neg)
        term.rx = _glob_to_regex(value)
        return term
    return Term("name", fold(value), neg)


def parse_query(query: str):
    """Return a list of OR-clauses; each clause is a list of AND-terms."""
    clauses, current = [], []
    for token in _tokenize(query):
        if token == "|":
            clauses.append(current)
            current = []
            continue
        term = _make_term(token)
        if term is not None:
            current.append(term)
    clauses.append(current)
    return [c for c in clauses if c] or [[]]


class _Ctx:
    """Per-entry attributes, computed lazily (shared by index verification and brute force)."""
    __slots__ = ("name_cf", "path_cf", "parent_cf", "is_dir", "stat_fn", "_stat")

    def stat(self):
        if self._stat is None:
            try:
                info = self.stat_fn()
                self._stat = (info.st_size, info.st_mtime)
            except OSError:
                self._stat = (0, 0.0)
        return self._stat


def _test(term: Term, ctx: _Ctx) -> bool:
    kind = term.kind
    if kind == "name":
        hit = term.value in ctx.name_cf
    elif kind == "glob":
        hit = term.rx.fullmatch(ctx.name_cf) is not None
    elif kind == "ext":
        hit = not ctx.is_dir and ext_of(ctx.name_cf) in term.values
    elif kind == "path":
        hit = term.value in ctx.path_cf
    elif kind == "pathglob":
        hit = term.rx.fullmatch(ctx.path_cf) is not None
    elif kind == "parent":
        parent = ctx.parent_cf
        hit = parent == term.value or parent.endswith("/" + term.value)
    elif kind == "file":
        hit = not ctx.is_dir
    elif kind == "folder":
        hit = ctx.is_dir
    elif kind == "regex":
        hit = term.rx.search(ctx.name_cf) is not None
    elif kind == "size":
        hit = (not ctx.is_dir) and term.pred(ctx.stat()[0])
    else:  # dm
        hit = term.pred(ctx.stat()[1])
    return hit != term.neg


def compile_search(query: str):
    """Reference matcher ``matches(path, name, is_dir)`` (brute force, one entry at a time).

    The index engine is tested against this for identical results.
    """
    clauses = parse_query(query)

    def matches(path, name, is_dir):
        ctx = _Ctx()
        ctx.path_cf = _norm(path)
        ctx.name_cf = fold(name)
        ctx.parent_cf = ctx.path_cf.rsplit("/", 1)[0] if "/" in ctx.path_cf else ""
        ctx.is_dir = bool(is_dir)
        ctx.stat_fn = lambda: os.lstat(path)
        ctx._stat = None
        return any(all(_test(t, ctx) for t in clause) for clause in clauses)
    return matches


# --------------------------------------------------------------------------- storage
class Segment:
    """Immutable block of consecutive entries."""
    __slots__ = ("base", "count", "cf", "cf_offs", "orig", "orig_offs", "parent", "flags")

    @property
    def end(self):
        return self.base + self.count

    def name_cf(self, i):
        return self.cf[self.cf_offs[i]:self.cf_offs[i + 1] - 1]

    def name(self, i):
        return self.orig[self.orig_offs[i]:self.orig_offs[i + 1]]


class _Builder:
    def __init__(self):
        self.cf, self.orig, self.parent = [], [], array("I")
        self.flags = bytearray()
        self.dir_ranges = []  # (dir_id, start_local, end_local)

    def __len__(self):
        return len(self.cf)

    def freeze(self, base) -> Segment:
        seg = Segment()
        seg.base, seg.count = base, len(self.cf)
        seg.cf = "\n" + "\n".join(self.cf) + "\n"
        # offsets are built entirely by C iterators (no per-name Python code)
        seg.cf_offs = array("I", map(add, accumulate(map(len, self.cf), initial=0), count(1)))
        seg.orig = "".join(self.orig)
        seg.orig_offs = array("I", accumulate(map(len, self.orig), initial=0))
        seg.parent, seg.flags = self.parent, self.flags
        return seg


def _merge(segments) -> Segment:
    first = segments[0]
    merged = Segment()
    merged.base = first.base
    merged.count = sum(s.count for s in segments)
    cf_parts, orig_parts = [first.cf], [first.orig]
    merged.cf_offs = array("I", first.cf_offs[:-1])
    merged.orig_offs = array("I", first.orig_offs[:-1])
    merged.parent = array("I", first.parent)
    merged.flags = bytearray(first.flags)
    cf_len, orig_len = len(first.cf), len(first.orig)
    for seg in segments[1:]:
        shift = cf_len - 1
        merged.cf_offs.extend(o + shift for o in seg.cf_offs[:-1])
        merged.orig_offs.extend(o + orig_len for o in seg.orig_offs[:-1])
        cf_parts.append(seg.cf[1:])
        orig_parts.append(seg.orig)
        cf_len += len(seg.cf) - 1
        orig_len += len(seg.orig)
        merged.parent.extend(seg.parent)
        merged.flags.extend(seg.flags)
    merged.cf = "".join(cf_parts)
    merged.orig = "".join(orig_parts)
    merged.cf_offs.append(len(merged.cf))
    merged.orig_offs.append(len(merged.orig))
    return merged


class SearchResult:
    """Matching entry ids in index order; rows are materialised page by page."""

    def __init__(self, index, segments, hits, elapsed, build_complete):
        self._index, self._segments, self._hits = index, segments, hits  # hits: list[array('I')] per segment
        self.total = sum(len(h) for h in hits)
        self.elapsed = elapsed
        self.index_complete = build_complete

    def rows(self, offset=0, limit=150):
        """[(path, name, is_dir, size, mtime)] for hits offset..offset+limit."""
        out = []
        skip = offset
        for seg, ids in zip(self._segments, self._hits):
            if skip >= len(ids):
                skip -= len(ids)
                continue
            for i in ids[skip:skip + limit - len(out)]:
                name = seg.name(i)
                path = self._index.entry_path(seg.parent[i], name)
                is_dir = bool(seg.flags[i] & IS_DIR)
                try:
                    info = os.lstat(path)
                    size, modified = info.st_size, info.st_mtime
                except OSError:
                    size, modified = 0, 0.0
                out.append((path, name, is_dir, size, modified))
            skip = 0
            if len(out) >= limit:
                break
        return out


# --------------------------------------------------------------------------- index
class FileIndex:
    def __init__(self, root):
        self.root = os.fspath(root)
        self._lock = threading.Lock()
        self._segments: tuple = ()
        self.total = 0
        self.dir_paths = []   # original path of each directory (id 0 = root)
        self.dir_cf = []      # casefolded, '/'-separated, no trailing '/'
        self.dir_start = array("I")
        self.dir_end = array("I")
        self.dir_hidden = bytearray()
        self.complete = False
        self.cancelled = False
        self.error = ""
        self.started_at = None
        self.finished_at = None
        self.scan_seconds = 0.0
        self.flush_min = _FLUSH_MIN
        self.flush_max = _FLUSH_MAX

    # ---- scanning ---------------------------------------------------------------
    def _new_dir(self, path, hidden):
        self.dir_paths.append(path)
        self.dir_cf.append(_norm(path).rstrip("/"))
        self.dir_start.append(0)
        self.dir_end.append(0)
        self.dir_hidden.append(1 if hidden else 0)
        return len(self.dir_paths) - 1

    def entry_path(self, dir_id, name):
        parent = self.dir_paths[dir_id]
        return parent + name if parent.endswith(os.sep) else parent + os.sep + name

    def _publish(self, builder, final=False):
        if not len(builder) and not builder.dir_ranges:
            return
        seg = builder.freeze(self.total)
        with self._lock:
            for dir_id, start, end in builder.dir_ranges:
                self.dir_start[dir_id] = seg.base + start
                self.dir_end[dir_id] = seg.base + end
            segments = self._segments + (seg,) if seg.count else self._segments
            if len(segments) > _MERGE_WHEN:
                head, tail = segments[:-_MERGE_COUNT], segments[-_MERGE_COUNT:]
                segments = head + (_merge(tail),)
            self._segments = segments
            self.total += seg.count

    def _add_directory(self, builder, queue, dir_id, dir_hidden, entries):
        """Append one directory's entries. Comprehensions keep the per-entry work in C loops."""
        names = [e.name for e in entries]
        try:
            is_dir = [e.is_dir(follow_symlinks=False) for e in entries]
            if _WINDOWS:
                import stat as _stat
                mask = _stat.FILE_ATTRIBUTE_HIDDEN | _stat.FILE_ATTRIBUTE_SYSTEM
                attr_hidden = [bool(e.stat(follow_symlinks=False).st_file_attributes & mask) for e in entries]
            else:
                attr_hidden = None
        except OSError:
            # Rare: an entry that cannot be examined. Redo one by one, skipping the bad ones.
            kept, is_dir, attr_hidden = [], [], ([] if _WINDOWS else None)
            for entry in entries:
                try:
                    d = entry.is_dir(follow_symlinks=False)
                    if _WINDOWS:
                        import stat as _stat
                        h = bool(entry.stat(follow_symlinks=False).st_file_attributes
                                 & (_stat.FILE_ATTRIBUTE_HIDDEN | _stat.FILE_ATTRIBUTE_SYSTEM))
                except OSError:
                    continue
                kept.append(entry)
                is_dir.append(d)
                if _WINDOWS:
                    attr_hidden.append(h)
            entries = kept
            names = [e.name for e in entries]
        folded = [n.casefold() for n in names]
        if "\n" in "".join(folded):  # a newline in a name would break the one-name-per-line blob
            folded = [f.replace("\n", " ") for f in folded]
        if dir_hidden:
            flags = [3 if d else 2 for d in is_dir]
        elif attr_hidden is not None:
            flags = [(1 if d else 0) | (2 if (h or n.startswith(".")) else 0) for n, d, h in zip(names, is_dir, attr_hidden)]
        else:
            flags = [(1 if d else 0) | (2 if n.startswith(".") else 0) for n, d in zip(names, is_dir)]
        builder.cf.extend(folded)
        builder.orig.extend(names)
        builder.parent.extend(repeat(dir_id, len(names)))
        builder.flags.extend(flags)
        for k in [k for k, d in enumerate(is_dir) if d]:
            queue.append(self._new_dir(entries[k].path, bool(flags[k] & IS_HIDDEN)))

    def build(self, cancel=lambda: False, progress=None):
        """Scan self.root (breadth first). Safe to query from other threads meanwhile."""
        self.started_at = time.monotonic()
        scandir, sep = os.scandir, os.sep
        root_id = self._new_dir(self.root, False)
        queue = deque([root_id])
        builder = _Builder()
        threshold = self.flush_min
        last_flush = last_progress = time.monotonic()
        try:
            while queue and not cancel():
                dir_id = queue.popleft()
                path = self.dir_paths[dir_id]
                dir_hidden = bool(self.dir_hidden[dir_id])
                start = len(builder)
                try:
                    it = scandir(path)
                except OSError:
                    continue
                entries = []
                with it:
                    try:
                        entries.extend(it)
                    except OSError:
                        pass  # directory vanished / unreadable mid-listing: keep what was read
                if entries:
                    self._add_directory(builder, queue, dir_id, dir_hidden, entries)
                builder.dir_ranges.append((dir_id, start, len(builder)))
                now = time.monotonic()
                if len(builder) >= threshold or (len(builder) and now - last_flush >= 0.5):
                    self._publish(builder)
                    builder = _Builder()
                    threshold = min(threshold * 2, self.flush_max)
                    last_flush = now
                    if progress:
                        progress(self.total, False)
                        last_progress = now
                elif progress and now - last_progress >= 0.25:
                    progress(self.total + len(builder), False)
                    last_progress = now
            self._publish(builder, final=True)
        except Exception as exc:  # never lose what was already indexed
            self.error = f"{type(exc).__name__}: {exc}"
            self._publish(builder, final=True)
        self.cancelled = bool(cancel())
        self.scan_seconds = time.monotonic() - self.started_at
        self.finished_at = time.time()
        self.complete = not self.cancelled and not self.error
        if progress:
            progress(self.total, True)

    # ---- querying ----------------------------------------------------------------
    @property
    def segments(self):
        return self._segments

    def search(self, query, include_hidden=False, name_filter="", cancel=lambda: False) -> SearchResult:
        started = time.perf_counter()
        clauses = parse_query(query)
        if name_filter:
            literal = fold(name_filter.strip())
            if literal:
                extra = Term("name", literal)
                clauses = [list(c) + [extra] for c in clauses]
        with self._lock:
            segments = self._segments
            ndirs = len(self.dir_cf)
        complete = self.complete
        per_clause = [self._run_clause(segments, ndirs, clause, include_hidden, cancel) for clause in clauses]
        if len(per_clause) == 1:
            hits = per_clause[0]
        else:
            hits = []
            for seg_i in range(len(segments)):
                union = set()
                for clause_hits in per_clause:
                    union.update(clause_hits[seg_i])
                hits.append(array("I", sorted(union)))
        return SearchResult(self, segments, hits, time.perf_counter() - started, complete)

    # -- candidate generators: each returns one array('I') of segment-local ids per segment
    @staticmethod
    def _gen_name(segments, value):
        out = []
        for seg in segments:
            ids, blob, offs = array("I"), seg.cf, seg.cf_offs
            find, bis = blob.find, bisect.bisect_right
            pos = find(value)
            while pos != -1:
                i = bis(offs, pos) - 1
                ids.append(i)
                pos = find(value, offs[i + 1])
            out.append(ids)
        return out

    _NO_EXT = re.compile(r"^(?:\.?[^.\n]*|[^\n]*\.)$", re.MULTILINE)

    @staticmethod
    def _gen_ext(segments, keys):
        """Files whose extension is one of `keys` ('' = no extension), via line-end search."""
        out = []
        for seg in segments:
            blob, offs = seg.cf, seg.cf_offs
            find, bis = blob.find, bisect.bisect_right
            ids = set()
            for key in keys:
                if key == "":
                    for match in FileIndex._NO_EXT.finditer(blob):
                        start = match.start()
                        if 0 < start < len(blob):
                            ids.add(bis(offs, start) - 1)
                elif "." in key or "\n" in key:
                    continue  # an extension never contains a dot
                else:
                    needle = "." + key + "\n"
                    pos = find(needle)
                    while pos != -1:
                        i = bis(offs, pos) - 1
                        if pos > offs[i]:  # ".pdf" alone is a dotfile, not an extension
                            ids.add(i)
                        pos = find(needle, pos + 1)
            out.append(array("I", sorted(ids)))
        return out

    @staticmethod
    def _gen_glob(segments, term):
        value = term.value
        core = value.strip("*")
        if not core:  # only stars: everything
            return [array("I", range(seg.count)) for seg in segments]
        if "*" not in core and "?" not in core:
            lead, trail = value.startswith("*"), value.endswith("*")
            if lead and trail:
                return FileIndex._gen_name(segments, core)
            out = []
            for seg in segments:
                blob, offs = seg.cf, seg.cf_offs
                find, bis = blob.find, bisect.bisect_right
                ids = array("I")
                if lead:  # "*suffix": the name ends exactly where the next "\n" is
                    needle = core + "\n"
                    pos = find(needle)
                    while pos != -1:
                        i = bis(offs, pos) - 1
                        ids.append(i)
                        pos = find(needle, offs[i + 1])
                else:     # "prefix*"
                    needle = "\n" + core
                    pos = find(needle)
                    while pos != -1:
                        i = bis(offs, pos + 1) - 1
                        ids.append(i)
                        pos = find(needle, offs[i + 1] - 1)
                out.append(ids)
            return out
        pattern = re.compile("^" + term.rx.pattern + "$", re.MULTILINE)
        out = []
        for seg in segments:
            offs, blob_end = seg.cf_offs, len(seg.cf)
            ids = array("I")
            for match in pattern.finditer(seg.cf):
                start = match.start()
                if 0 < start < blob_end:  # ignore the empty "lines" before the first / after the last "\n"
                    ids.append(bisect.bisect_right(offs, start) - 1)
            out.append(ids)
        return out

    @staticmethod
    def _gen_regex(segments, rx):
        out = []
        for seg in segments:
            names = seg.cf.split("\n")[1:-1]
            search = rx.search
            out.append(array("I", (i for i, n in enumerate(names) if search(n))))
        return out

    def _dirs_where(self, ndirs, predicate):
        cf = self.dir_cf
        return [d for d in range(ndirs) if predicate(cf[d])]

    def _split_global(self, segments, global_ids):
        """Global sorted ids -> per-segment local id arrays."""
        out = []
        pos = 0
        for seg in segments:
            ids = array("I")
            n = len(global_ids)
            while pos < n and global_ids[pos] < seg.end:
                ids.append(global_ids[pos] - seg.base)
                pos += 1
            out.append(ids)
        return out

    def _ranges_to_ids(self, dirs):
        ranges = sorted((self.dir_start[d], self.dir_end[d]) for d in dirs if self.dir_end[d] > self.dir_start[d])
        ids = array("I")
        for start, end in ranges:
            ids.extend(range(start, end))
        return ids

    def _gen_path(self, segments, ndirs, value):
        """Entries whose full path contains `value` (names and directories both searched)."""
        cf = self.dir_cf
        whole = self._dirs_where(ndirs, lambda d: value in d + "/")
        ids = set(self._ranges_to_ids(whole))
        head, slash, tail = value.rpartition("/")
        if not slash:
            for seg, local in zip(segments, self._gen_name(segments, value)):
                ids.update(seg.base + i for i in local)
        else:
            # the "/" in the term falls on the folder/name boundary: dir endswith head, name startswith tail
            suffix_dirs = {d for d in range(ndirs) if (cf[d] + "/").endswith(head + "/")} if head else set(range(ndirs))
            if tail:
                for seg in segments:
                    blob, offs, parent = seg.cf, seg.cf_offs, seg.parent
                    needle = "\n" + tail
                    pos = blob.find(needle)
                    while pos != -1:
                        i = bisect.bisect_right(offs, pos + 1) - 1
                        if parent[i] in suffix_dirs:
                            ids.add(seg.base + i)
                        pos = blob.find(needle, offs[i + 1] - 1)
            else:
                ids.update(self._ranges_to_ids(suffix_dirs))
        return self._split_global(segments, sorted(ids))

    def _run_clause(self, segments, ndirs, clause, include_hidden, cancel):
        positives = [t for t in clause if not t.neg]
        generator, rest = None, list(clause)

        def pick(kind):
            for t in positives:
                if t.kind == kind:
                    return t
            return None

        term = pick("parent")
        if term is not None:
            value = term.value
            dirs = self._dirs_where(ndirs, lambda d: d == value or d.endswith("/" + value))
            candidates = self._split_global(segments, self._ranges_to_ids(dirs))
            generator = term
        else:
            term = pick("ext")
            if term is not None:
                candidates = self._gen_ext(segments, term.values)
                generator = term
            else:
                names = sorted((t for t in positives if t.kind == "name"), key=lambda t: -len(t.value))
                if names:
                    generator, candidates = names[0], self._gen_name(segments, names[0].value)
                else:
                    term = pick("glob")
                    if term is not None:
                        generator, candidates = term, self._gen_glob(segments, term)
                    else:
                        term = pick("path")
                        if term is not None:
                            generator, candidates = term, self._gen_path(segments, ndirs, term.value)
                        else:
                            term = pick("regex")
                            if term is not None:
                                generator, candidates = term, self._gen_regex(segments, term.rx)
                            else:
                                candidates = None
        if generator is not None:
            rest.remove(generator)
        # file:/folder: (and their negations) are plain flag checks: no per-entry context needed.
        dir_state = None  # None = either, True = folders only, False = files only
        if generator is not None and generator.kind == "ext":
            dir_state = False  # ext: applies to files only; the extension table also lists folders
        for t in [t for t in rest if t.kind in ("file", "folder")]:
            rest.remove(t)
            wants_dir = (t.kind == "folder") != t.neg
            if dir_state is not None and dir_state != wants_dir:
                return [array("I") for _ in segments]  # contradictory (file: folder:)
            dir_state = wants_dir
        needs_ctx = bool(rest)
        hidden_flag = 0 if include_hidden else IS_HIDDEN
        reject = hidden_flag | (IS_DIR if dir_state is False else 0)
        need_dir = IS_DIR if dir_state else 0
        out = []
        dirs_cf, dir_paths = self.dir_cf, self.dir_paths
        ctx = _Ctx()
        for seg_i, seg in enumerate(segments):
            if cancel():
                raise InterruptedError("search superseded")
            flags = seg.flags
            ids = candidates[seg_i] if candidates is not None else range(seg.count)
            if not needs_ctx:
                if reject or need_dir:
                    out.append(array("I", (i for i in ids if not flags[i] & reject and (not need_dir or flags[i] & need_dir))))
                else:
                    out.append(array("I", ids))
                continue
            kept = array("I")
            counter = 0
            parent = seg.parent
            for i in ids:
                flag = flags[i]
                if flag & reject or (need_dir and not flag & need_dir):
                    continue
                counter += 1
                if counter & 8191 == 0 and cancel():
                    raise InterruptedError("search superseded")
                d = parent[i]
                ctx.name_cf = seg.name_cf(i)
                ctx.parent_cf = dirs_cf[d]
                ctx.path_cf = ctx.parent_cf + "/" + ctx.name_cf
                ctx.is_dir = bool(flag & IS_DIR)
                ctx._stat = None
                ctx.stat_fn = (lambda s=seg, k=i, dd=d: os.lstat(self.entry_path(dd, s.name(k))))
                if all(_test(t, ctx) for t in rest):
                    kept.append(i)
            out.append(kept)
        return out
