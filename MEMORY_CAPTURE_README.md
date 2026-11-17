# Memory Capture tab

Adds one top-level tab without replacing existing tabs. **Live Capture** creates a ZIP with process/network/session/interface metadata, JSON manifest, and streamed SHA-256/MD5 verification. It does **not** capture physical RAM or pagefile. **Verify Memory Image** hashes an existing file read-only. Physical RAM mode is intentionally disabled until a compatible acquisition backend is integrated and validated for the target OS/architecture. On Apple Silicon macOS, no supported physical acquisition backend is supplied. No fake captures.

Install: `python -m pip install -r requirements.txt`; launch `python main.py`. Keep evidence on a suitable protected volume. Live response can include sensitive process arguments and network metadata; handle accordingly. SHA-256 is the integrity hash; MD5 is included for interoperability. Cancellation is cooperative. No external acquisition drivers are installed.

## Physical acquisition (experimental external-tool integration)

Windows x64: install a trusted, compatible `winpmem.exe` on PATH. Linux x64: install a trusted compatible `avml` on PATH. Restart the application to refresh detection. The integrated command templates are `winpmem.exe -o OUTPUT` and `avml OUTPUT`; confirm your exact tool build documents this syntax before using it on evidence. Requires appropriate elevated privileges and OS support. Other architectures and macOS physical RAM capture are explicitly unsupported. This project does not ship drivers or acquisition executables. Capture output is not marked verified until the tool exits successfully and a nonempty file is hashed. On failure or cancellation, partial files and logs are retained, not validated. Progress remains indeterminate until completion.
