# NumPy numerical dependency

Bundled wheel: NumPy 2.2.6, CPython 3.12, Linux x86_64 manylinux2014.

Official metadata: https://pypi.org/pypi/numpy/2.2.6/json

SHA256: `fd83c01228a688733f1ded5201c678f0c53ecc1006ffbc404db9f7a899ac6249`.

The file was downloaded from the Tsinghua PyPI mirror and verified against the
official PyPI release hash. Packaging it permits installation without network
access during image construction (after the base image is available).

The wheel includes NumPy/OpenBLAS license notices in its `.dist-info` directory;
installation preserves those notices. It supplies generic array mathematics.
Database storage, SQL execution, transactions and the vector index are project
source code and do not use an external database or vector indexing engine.
