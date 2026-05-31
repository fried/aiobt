# Optional Cython Compilation

The `aiobt.bencode` module is structured as pure Python with module-level
functions and simple types, making it straightforward to compile with Cython
for ~10x faster torrent file parsing.

## Quick Start

```bash
# Install build deps
pip install cython setuptools

# Compile all eligible modules
python build_cython.py

# Verify
python -c "from aiobt import is_compiled; print(is_compiled('bencode'))"
# True
```

## How It Works

Cython can compile pure `.py` files directly into C extension modules
(`.so` on Unix, `.pyd` on Windows).  When both `bencode.py` and
`bencode.cpython-314-x86_64-linux-gnu.so` exist in the same directory,
Python's import system automatically prefers the compiled extension.

The original `.py` file is always shipped alongside the compiled
extension, so pure Python fallback is guaranteed if the `.so` is
missing or incompatible.

### Runtime introspection

```python
from aiobt import is_compiled, compilation_status

is_compiled("bencode")   # True if .so is loaded
compilation_status()      # {'bencode': True}
```

## Design Constraints for Cython Compatibility

The bencode module avoids:

- Closures and nested functions in hot paths
- Complex Python-only features (generators in decode loops, etc.)
- Class methods for core encode/decode (uses standalone functions)
- Global mutable state

This means Cython can compile it with minimal friction — primarily
benefiting from C-level integer/byte operations and reduced interpreter
overhead in the recursive parse/encode loops.

## Build Script

`build_cython.py` at the project root handles everything:

```bash
python build_cython.py            # compile all eligible modules
python build_cython.py --check    # verify Cython is available
python build_cython.py --clean    # remove all compiled artifacts
```

Eligible modules are listed in `CYTHON_MODULES` inside the script.
To add a new module for Cython compilation, add its dotted name and
source path to that list.

## Compiler Directives

The build script uses these Cython compiler directives for performance:

- `boundscheck=False` — skip array bounds checks
- `wraparound=False` — skip negative index wrapping
- `cdivision=True` — use C integer division (no ZeroDivisionError check)

These are safe for the bencode module because all indexing is
bounds-checked manually in the Python source.

## Key Functions to Optimize

- `_decode_any` — the dispatch function called recursively
- `_decode_bytes` — called for every string/key in a torrent file
- `_find_byte` — linear scan that benefits from C-level iteration
- `_encode_any` — recursive encoder

## Performance Notes

A typical torrent file is 10–100 KB of bencoded data.  The pure Python
implementation handles this in <1ms on modern hardware.  Cython compilation
is most valuable when:

- Processing thousands of torrent files (tracker or indexer service)
- Parsing very large torrent files (>10 MB with thousands of file entries)
- Running a high-throughput distribution service with compact storage

## CI / Release

- **CI** (`ci.yml`): compiles Cython, verifies the extension loads,
  runs the full test suite with compiled extensions.
- **Release** (`release.yml`): builds platform-specific wheels
  (Linux/macOS/Windows) that ship *both* `.so` and `.py` for each
  Cython-eligible module.  A pure Python `py3-none-any` wheel is also
  published as a universal fallback.
