"""Shared helpers: size formatting, file categories and age/size buckets.

Sizes use decimal (SI) units, 1 GB = 1,000,000,000 bytes, which is what Ubuntu's
file manager, Docker and Streamlit's tables use. `df -h` / `du -h` print binary
units (GiB), so their numbers look ~7% smaller for the same bytes.
"""
from __future__ import annotations

import bisect
from datetime import datetime

KB = 1000
MB = 1000**2
GB = 1000**3
DAY = 86400

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(n: float | int | None) -> str:
    """Format a byte count, e.g. 5_700_000_000 -> '5.7 GB'."""
    if n is None:
        return "—"
    value = float(n)
    sign = "-" if value < 0 else ""
    value = abs(value)
    for unit in _UNITS:
        if value < 1000 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{sign}{value:.0f} B"
            return f"{sign}{value:.1f} {unit}"
        value /= 1000
    return f"{sign}{value:.1f} PB"  # unreachable


def unit_for(max_bytes: float) -> tuple[float, str]:
    """Pick one unit for a chart axis so every tick uses the same scale."""
    for divisor, unit in ((1000**4, "TB"), (GB, "GB"), (MB, "MB"), (KB, "KB")):
        if max_bytes >= divisor:
            return divisor, unit
    return 1, "B"


def display_path(path: str) -> str:
    """Make a path safe to render (undecodable filename bytes become U+FFFD)."""
    try:
        return path.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:  # Windows names can hold unpaired UTF-16 surrogates
        return path.encode("utf-8", "replace").decode("utf-8")


def fmt_time(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- buckets

# Upper bound (in days) of each age bucket; the last bucket is open-ended.
AGE_LIMITS_DAYS = [7, 30, 182, 365, 730, 1826]
AGE_BUCKETS = [
    "< 1 week",
    "1 wk – 1 month",
    "1 – 6 months",
    "6 – 12 months",
    "1 – 2 years",
    "2 – 5 years",
    "> 5 years",
]

SIZE_LIMITS = [4 * KB, MB, 10 * MB, 100 * MB, GB]
SIZE_BUCKETS = ["< 4 KB", "4 KB – 1 MB", "1 – 10 MB", "10 – 100 MB", "100 MB – 1 GB", "> 1 GB"]


def age_bucket(ts: float, now: float) -> int:
    days = (now - ts) / DAY
    return bisect.bisect_left(AGE_LIMITS_DAYS, days) if days > 0 else 0


def size_bucket(size: int) -> int:
    return bisect.bisect_left(SIZE_LIMITS, size)


# --------------------------------------------------------------------------- file types

_MULTI_EXT = (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst")


def file_extension(name: str) -> str:
    """Normalised extension used for grouping ('.tar.gz', '.so', '(none)', ...)."""
    lower = name.lower()
    if lower.endswith(".so") or ".so." in lower:
        return ".so"
    for multi in _MULTI_EXT:
        if lower.endswith(multi):
            return multi
    dot = lower.rfind(".")
    if dot <= 0 or dot == len(lower) - 1:
        return "(none)"
    ext = lower[dot:]
    body = ext[1:]
    if body.isdigit():
        return "(numbered)"  # rotated logs (.1), split archives, chunk files
    if len(ext) > 12 or not body.replace("_", "").replace("-", "").isalnum():
        return "(other)"
    return ext


_CATEGORIES: dict[str, set[str]] = {
    "Video": {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts"},
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".opus", ".wma"},
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".tif", ".tiff", ".heic", ".ico",
               ".psd", ".raw", ".cr2", ".nef", ".xcf"},
    "Archives & installers": {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".rar", ".deb", ".rpm",
                              ".snap", ".appimage", ".whl", ".jar", ".vsix", ".nupkg", ".apk", ".msi", ".exe",
                              ".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".crx"},
    "Disk images & VMs": {".iso", ".img", ".qcow2", ".vdi", ".vmdk", ".vhd", ".vhdx", ".ova", ".dmg"},
    "Documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".txt",
                  ".md", ".rtf", ".epub", ".tex", ".pages", ".key", ".numbers"},
    "Data & databases": {".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet", ".feather", ".arrow", ".avro",
                         ".orc", ".npy", ".npz", ".pkl", ".pickle", ".db", ".sqlite", ".sqlite3", ".ldb", ".mdb",
                         ".xml", ".yaml", ".yml", ".log", ".dat", ".h5", ".hdf5", ".nc", ".mat", ".sav", ".dump",
                         ".sql", ".bson", ".rdb", ".wal", ".journal"},
    "ML models": {".pt", ".pth", ".onnx", ".safetensors", ".ckpt", ".pb", ".tflite", ".gguf", ".ggml",
                  ".keras", ".joblib", ".cbm", ".model"},
    "Source code": {".py", ".ipynb", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".c", ".cc",
                    ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".html", ".htm",
                    ".css", ".scss", ".less", ".vue", ".svelte", ".swift", ".dart", ".lua", ".r", ".scala",
                    ".gd", ".tscn", ".map", ".lock", ".toml", ".ini", ".cfg"},
    "Binaries & libraries": {".so", ".a", ".o", ".dll", ".node", ".pyc", ".pyo", ".class", ".wasm", ".dylib",
                             ".bin", ".pak", ".asar", ".ko", ".elf", ".lib", ".pdb"},
    "Temp & backups": {".tmp", ".temp", ".bak", ".old", ".swp", ".part", ".crdownload", ".cache", ".orig"},
}
_EXT_TO_CATEGORY = {ext: cat for cat, exts in _CATEGORIES.items() for ext in exts}
CATEGORIES = list(_CATEGORIES) + ["Other"]


def category_of(ext: str) -> str:
    return _EXT_TO_CATEGORY.get(ext, "Other")
