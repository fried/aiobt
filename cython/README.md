# Cython Extensions

aiobt uses [Meson](https://mesonbuild.com/) via
[meson-python](https://meson-python.readthedocs.io/) to optionally compile
performance-critical modules (currently `bencode.py`) into C extensions.

## How it works

- `meson.build` defines `py.extension_module('bencode', 'bencode.py', ...)`
- Meson invokes Cython to transpile `bencode.py` → C → shared library
- Both the `.py` source and compiled `.so` are installed; Python prefers the `.so`
- If Cython compilation fails, the pure-Python `.py` is used automatically

## Building with Cython

```bash
pip install meson-python meson ninja cython
pip install -e ".[dev]" --no-build-isolation
```

## Verifying compilation

```python
from aiobt._compiled import compilation_status
print(compilation_status())  # {'bencode': True} if compiled
```

## Cython compatibility rules for `bencode.py`

To keep `bencode.py` compilable by Cython:

- No `match` statements (use `if`/`elif`)
- No PEP 695 `type` statements (use `typing.Union`)
- No walrus operator in complex expressions
- Stick to simple control flow and basic types
