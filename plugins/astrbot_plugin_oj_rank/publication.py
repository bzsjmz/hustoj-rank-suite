"""Pin current data generation for one command; historical files/images stay global."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_root = ContextVar('oj_data_generation', default=None)
FILES = {'latest.json', 'daily.json', 'lookup.json', 'academic-labels.json',
         'computer-college.json', 'software-college.json', 'class-intensity.json',
         'major-intensity.json', 'weekly.json', 'monthly.json', 'historical-daily.json',
         'class-images/manifest.json', 'major-images/manifest.json'}


def resolve_root(root):
    pointer = root/'current-data'
    if not pointer.exists() and not pointer.is_symlink():
        return root
    target = pointer.resolve(strict=True)
    if target.parent != (root/'.data-versions').resolve():
        raise ValueError('invalid data generation pointer')
    return target


@contextmanager
def pin_publication(root=Path('/oj-rank-share')):
    root = Path(root)
    token = _root.set((root, resolve_root(root)))
    try:
        yield
    finally:
        _root.reset(token)


def published_path(path):
    path = Path(path)
    base, target = _root.get() or (Path('/oj-rank-share'), None)
    try:
        relative = path.relative_to(base).as_posix()
    except ValueError:
        return path
    if relative not in FILES and not relative.startswith(('class-ranklists/', 'major-ranklists/')):
        return path
    return (target if target is not None else resolve_root(base))/relative
