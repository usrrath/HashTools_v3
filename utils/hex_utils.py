from pathlib import Path
BYTES_PER_ROW = 16

def read_page(path: str, offset: int, page_size: int) -> tuple[bytes, int, int]:
    size = Path(path).stat().st_size
    if size <= 0:
        return b"", 0, 0
    offset = max(0, min(offset, max(0, size - 1)))
    offset -= offset % BYTES_PER_ROW
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(page_size)
    return data, offset, size

def ascii_char(byte: int) -> str:
    return chr(byte) if 32 <= byte <= 126 else "."

def query_to_bytes(query: str, mode: str) -> bytes:
    query = query.strip()
    if not query:
        return b""
    if mode.lower().startswith("hex"):
        return bytes.fromhex("".join(query.split()))
    return query.encode("utf-8")

def find_bytes(path: str, needle: bytes, start_offset: int = 0) -> int:
    if not needle:
        return -1
    block_size = 1024 * 1024
    overlap = max(len(needle) - 1, 0)
    absolute = max(0, start_offset)
    with open(path, "rb") as handle:
        handle.seek(absolute)
        previous = b""
        while True:
            chunk = handle.read(block_size)
            if not chunk:
                return -1
            data = previous + chunk
            pos = data.find(needle)
            if pos != -1:
                return absolute - len(previous) + pos
            absolute += len(chunk)
            previous = data[-overlap:] if overlap else b""

def file_signature_from_path(path: str) -> str:
    with open(path, "rb") as handle:
        data = handle.read(16)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG image: 89 50 4E 47 0D 0A 1A 0A"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG image: FF D8 FF"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "GIF image"
    if data.startswith(b"%PDF"):
        return "PDF document"
    if data.startswith(b"PK\x03\x04"):
        return "ZIP/Office archive"
    return "Unknown / generic binary"
