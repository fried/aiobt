# Pure Python sources — installed as-is
py.install_sources(
  '__init__.py',
  '_compiled.py',
  '_version.py',
  'client.py',
  'discovery.py',
  'network.py',
  'peer.py',
  'piece.py',
  'protocol.py',
  'torrent.py',
  'tracker.py',
  'py.typed',
  subdir: 'aiobt',
)

# Cython extension — compiled .so replaces the pure-Python bencode.py
# at import time. The .py source is still installed as fallback.
py.install_sources('bencode.py', subdir: 'aiobt')
py.extension_module(
  'bencode',
  'bencode.py',
  subdir: 'aiobt',
  install: true,
)

subdir('storage')
