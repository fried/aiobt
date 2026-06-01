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

# Cython extension — compiled .so from .pyx, with .py fallback
py.install_sources('bencode.py', subdir: 'aiobt')
py.extension_module(
  'bencode',
  'bencode.pyx',
  subdir: 'aiobt',
  install: true,
)

subdir('storage')
