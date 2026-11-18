# Volatility 3 integrated into Memory Capture

The supplied Volatility 3 development source is included at `vendor/volatility3_src/` with its upstream LICENSE.txt. No external Volatility installation is required; install Python dependencies with `python -m pip install -r requirements.txt`. Optional plugin dependencies and symbols may still be required. The new **Volatility 3 Analysis** subtab is under **Memory Capture**; other tabs are unchanged.

Select an already acquired memory image, choose a plugin, and Analyze. Analysis is performed read-only in a cancellable worker subprocess. Export JSON records the selected plugin, original image path, SHA-256, UTC export time, and results. JSON export hashes the source image and can take time for large evidence files. A memory image's extension does not guarantee compatibility. Symbol availability and image format determine plugin success. WinPmem driver blocks are not resolved by Volatility.

For PyInstaller builds, ensure `vendor/volatility3_src` is included as data and `pefile` is installed. Running a frozen app with `sys.executable` to execute a Python script is unsupported; packaged Windows builds need a separate Python interpreter or a dedicated frozen Volatility CLI. This source-based integration is validated for Python execution, not frozen binaries.
