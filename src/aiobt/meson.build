# Pure Python sources — installed as-is
py.install_sources(
  '__init__.py',
  '_compiled.py',
  '_version.py',
  'client.py',
  'create.py',
  'discovery.py',
  'engine.py',
  'events.py',
  'network.py',
  'peer.py',
  'torrent.py',
  'tracker.py',
  'py.typed',
  subdir: 'aiobt',
)

# Cython extensions — compiled .so from .pyx, with .py fallback

py.install_sources('bencode.py', subdir: 'aiobt')
py.extension_module(
  'bencode',
  'bencode.pyx',
  subdir: 'aiobt',
  install: true,
)

py.install_sources('piece.py', subdir: 'aiobt')
py.extension_module(
  'piece',
  'piece.pyx',
  subdir: 'aiobt',
  install: true,
)

py.install_sources('protocol.py', subdir: 'aiobt')
py.extension_module(
  'protocol',
  'protocol.pyx',
  subdir: 'aiobt',
  install: true,
)

subdir('storage')
