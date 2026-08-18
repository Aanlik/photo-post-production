# Test Fixtures

This directory is reserved for small, synthetic, metadata-only fixtures used by
the test suite. Do not add personal photographs, RAW/JPEG/PSD/TIFF exports,
catalog files, contact sheets, local absolute paths, device metadata, or run
artifacts here.

Unit tests create temporary images and working directories at runtime. Any
future committed fixture must be reproducible, non-identifying, and small
enough for normal source control. Real Lightroom and Photoshop acceptance
artifacts belong outside the repository and should be summarized without
including source paths or personal metadata.
