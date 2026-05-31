# Optional Cython Compilation

The `aiobt.bencode` module is structured as pure Python with module-level
functions and simple types, making it straightforward to compile with Cython
for ~10x faster torrent file parsing.

## How It Works

The bencode module avoids:
- Closures and nested functions in hot paths
- Complex Python-only features (generators in decode loops, etc.)
- Class methods for core encode/decode (uses standalone functions)

This means a `.pyx` file can shadow the pure Python module with minimal
changes — primarily adding `cdef` type declarations.

## Building

1. Copy `src/aiobt/bencode.py` to `cython/bencode.pyx`
2. Add Cython type annotations (`cdef`, `cpdef`, typed memoryviews)
3. Build:

```bash
cd cython/
cythonize -i bencode.pyx
```

4. The compiled `.so` will be importable as a drop-in replacement:

```python
try:
    from cython.bencode import encode, decode  # compiled
except ImportError:
    from aiobt.bencode import encode, decode   # pure Python fallback
```

## Key Functions to Optimize

- `_decode_any` — the dispatch function called recursively
- `_decode_bytes` — called for every string/key in a torrent file
- `_find_byte` — linear scan that benefits from C-level iteration
- `_encode_any` — recursive encoder

## Example .pyx Annotations

```cython
cpdef tuple _decode_bytes(const unsigned char[:] buf, int pos):
    cdef int colon, length, start, end
    colon = _find_byte(buf, 58, pos)  # ':'
    ...
```

## Performance Notes

A typical torrent file is 10-100 KB of bencoded data.  The pure Python
implementation handles this in <1ms on modern hardware.  Cython compilation
is most valuable when processing thousands of torrent files (e.g., a
tracker or indexer service) or parsing very large torrent files (>10 MB
with thousands of file entries).
