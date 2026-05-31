#!/usr/bin/env python3
"""Compile optional Cython extensions for aiobt.

Compiles Cython-eligible .py modules into platform-specific shared
libraries (.so on Unix, .pyd on Windows) in-place alongside the
source files.  Python's import system automatically prefers compiled
extensions over .py source when both exist in the same directory.

Usage::

    python build_cython.py            # compile all eligible modules
    python build_cython.py --check    # verify Cython + setuptools available
    python build_cython.py --clean    # remove compiled artifacts

Requires: cython, setuptools
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

# Modules eligible for optional Cython compilation.
# (dotted extension name, source path relative to project root)
CYTHON_MODULES: list[tuple[str, str]] = [
    ("aiobt.bencode", "src/aiobt/bencode.py"),
]


def _clean(project_root: Path) -> None:
    """Remove all Cython build artifacts."""
    patterns = [
        "src/aiobt/*.so",
        "src/aiobt/*.pyd",
        "src/aiobt/*.c",
        "src/aiobt/**/*.so",
        "src/aiobt/**/*.pyd",
        "src/aiobt/**/*.c",
    ]
    removed = 0
    for pattern in patterns:
        for path in glob.glob(str(project_root / pattern), recursive=True):
            p = Path(path)
            if p.is_file():
                p.unlink()
                print(f"  removed {p.relative_to(project_root)}")
                removed += 1

    # Remove build/ and temp dirs from setuptools
    build_dir = project_root / "build"
    if build_dir.exists():
        import shutil

        shutil.rmtree(build_dir)
        print("  removed build/")
        removed += 1

    print(f"Cleaned {removed} artifact(s)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile optional Cython extensions for aiobt"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only verify that Cython and setuptools are available",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove compiled artifacts instead of building",
    )
    args = parser.parse_args()

    project_root = Path(__file__).parent.resolve()

    if args.clean:
        _clean(project_root)
        return

    # Verify Cython is available
    try:
        from Cython.Build import cythonize
    except ImportError:
        print("ERROR: Cython is required.  pip install cython", file=sys.stderr)
        sys.exit(1)

    # Verify setuptools is available
    try:
        from setuptools import Distribution, Extension
        from setuptools.command.build_ext import build_ext
    except ImportError:
        print(
            "ERROR: setuptools is required.  pip install setuptools",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.check:
        import Cython

        print(f"Cython {Cython.__version__} — ready")
        sys.exit(0)

    os.chdir(project_root)

    # Verify source files exist
    for name, source in CYTHON_MODULES:
        if not Path(source).exists():
            print(f"ERROR: source not found: {source}", file=sys.stderr)
            sys.exit(1)

    extensions = [Extension(name, [source]) for name, source in CYTHON_MODULES]

    ext_modules = cythonize(
        extensions,
        language_level="3str",
        compiler_directives={
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
        },
    )

    dist = Distribution(
        {
            "ext_modules": ext_modules,
            "package_dir": {"": "src"},
        }
    )

    cmd = build_ext(dist)
    cmd.inplace = True
    cmd.ensure_finalized()
    cmd.run()

    # Verify the compiled extensions are importable
    print("\n✓ Compiled extensions:")
    for name, source in CYTHON_MODULES:
        # Find the .so/.pyd that was produced
        source_dir = Path(source).parent
        mod_stem = Path(source).stem
        artifacts = list(source_dir.glob(f"{mod_stem}.cpython-*")) + list(
            source_dir.glob(f"{mod_stem}.*.so")
        ) + list(source_dir.glob(f"{mod_stem}.*.pyd"))
        if artifacts:
            for art in artifacts:
                print(f"  {name} → {art.relative_to(project_root)}")
        else:
            print(f"  {name} — compiled (artifact in {source_dir})")


if __name__ == "__main__":
    main()
