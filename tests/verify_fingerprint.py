#!/usr/bin/env python3
"""The tree fingerprint does not read build caches (§harness.development.snapshot). Model-free, with controls.

2026-11-23: an Xcode derived-data folder of evidence (31 GB, 112,843 untracked files) was read in full on every claim
in the projects folder; a fingerprint took 1 to 2.5 minutes and the load rose to 61 on 12 cores.

① a tree without a build cache keeps the fingerprint it had before (the same bytes in the same order)
② untracked files in an Xcode derived-data folder (the folder holding *.noindex folders: Build/Products, info.plist)
   and in a folder with a signed CACHEDIR.TAG are not read: made unreadable, the fingerprint is still taken
③ … they are hashed by size and modification time: the same size and time with other bytes does not move the tree,
   a new modification time does, and the moved file is named; controls, read as before (other bytes move the tree):
   an ordinary untracked file, a file beside a CACHEDIR.TAG without the signature, a tracked file inside a cache
   folder, a *.noindex folder at the root does not make the root a cache
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
from harness import development  # noqa: E402

FAILED = []
SIGNATURE = b'Signature: 8a477f597d28d172789f06886806bc55\n# a cache directory tag\n'


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout


def put(root, name, data=b'x'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def old_fingerprint(root):
    """The fingerprint as main ca62209 took it: every tracked and visible untracked file read in full."""
    names = set(git(root, 'ls-files', '-z').split(b'\0'))
    names.update(git(root, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0'))
    result = hashlib.sha256()
    for raw in sorted(names - {b''}):
        path = Path(root) / os.fsdecode(raw)
        result.update(raw + b'\0')
        if path.is_symlink():
            result.update(b'link:' + os.fsencode(os.readlink(path)))
        elif path.is_file():
            result.update(str(path.stat().st_mode).encode())
            result.update(path.read_bytes())
        else:
            result.update(b'missing')
    result.update(git(root, 'rev-parse', 'HEAD'))
    return result.hexdigest()


def same_size_other_bytes(path):
    """Rewrite a file with other bytes of the same length and put its modification time back."""
    status = path.stat()
    data = path.read_bytes()
    path.write_bytes(bytes((b + 1) % 256 for b in data))
    os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))


def moves(root, path, change):
    before = development.fingerprint(root)
    change(path)
    return development.fingerprint(root) != before


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-fingerprint-'))
    root = base / 'repo'
    root.mkdir()
    git(root, 'init', '-q')
    put(root, 'src/app.py', b'print(1)\n')
    put(root, 'dd/tracked-note.txt', b'kept in git\n')
    git(root, 'add', '.')
    git(root, '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '-m', 'init')
    put(root, 'notes/draft.md', b'an ordinary untracked file\n')
    put(root, 'loose.txt', b'at the root\n')

    check('① a tree without a build cache keeps its fingerprint', development.fingerprint(root) == old_fingerprint(root))
    put(root, 'odd.noindex/kept.txt', b'a noindex folder at the root\n')

    derived = [put(root, 'dd/info.plist', b'<plist/>\n'),
               put(root, 'dd/ModuleCache.noindex/Swift.pcm', os.urandom(4096)),
               put(root, 'dd/Build/Intermediates.noindex/app.o', os.urandom(8192)),
               put(root, 'dd/Build/Products/Debug/app.app/app', os.urandom(8192))]
    tagged = [put(root, 'target/CACHEDIR.TAG', SIGNATURE), put(root, 'target/debug/big.bin', os.urandom(8192))]
    fake = [put(root, 'fakecache/CACHEDIR.TAG', b'not a cache tag\n'), put(root, 'fakecache/data.bin', os.urandom(64))]
    finder = getattr(development, 'cache_folders', None)   # before the change there is none: the check fails
    folders = finder(root, git(root, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0')) if finder else set()
    check('② the cache folders are found by their marks', {'dd', 'dd/Build', 'target'} <= folders and 'fakecache' not in folders
          and '' not in folders and 'odd.noindex' in folders, sorted(folders))

    for path in derived + tagged[1:]:
        os.chmod(path, 0)
    try:
        development.fingerprint(root)
        read = None
    except (PermissionError, OSError) as error:
        read = error
    check('② files in the build caches are not read (unreadable, the fingerprint is still taken)', read is None, read)
    for path in derived + tagged[1:]:
        os.chmod(path, 0o644)

    products = root / 'dd/Build/Products/Debug/app.app/app'
    check('③ a cache file with the same size and time but other bytes does not move the tree',
          not moves(root, products, same_size_other_bytes))
    check('③ a cache file with a new modification time moves the tree',
          moves(root, products, lambda p: os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 10**9))))
    before = development.snapshot(root)[1]
    put(root, 'target/debug/big.bin', os.urandom(9000))
    after = development.snapshot(root)[1]
    check('③ … and the moved file is named', development.moved_entries(before, after) == ['target/debug/big.bin'],
          development.moved_entries(before, after))
    check('③ control: an ordinary untracked file is read', moves(root, root / 'notes/draft.md', same_size_other_bytes))
    check('③ control: beside a CACHEDIR.TAG without the signature, files are read',
          moves(root, root / 'fakecache/data.bin', same_size_other_bytes))
    check('③ control: a tracked file inside a cache folder is read',
          moves(root, root / 'dd/tracked-note.txt', same_size_other_bytes))
    check('③ control: a *.noindex folder at the root does not make the root a cache',
          moves(root, root / 'loose.txt', same_size_other_bytes))

    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
