from dataclasses import dataclass

# status values
OK = "OK"
ERROR = "ERROR"
CANCELLED = "CANCELLED"


@dataclass
class HashResult:
    file_name: str
    file_path: str
    size: int
    md5: str = ""
    sha1: str = ""
    sha256: str = ""
    sha512: str = ""
    success: bool = True
    error: str = ""
    status: str = OK
    # Non-empty when the digests are valid for the bytes read but the file
    # changed (size or mtime) while it was being read.
    warning: str = ""
    blake3: str = ""  # b3sum-compatible 256-bit digest (appended last to keep positional order)
