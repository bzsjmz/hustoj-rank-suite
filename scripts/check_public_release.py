"""Read-only release gate. Reports paths/categories, never matched secret values.

This is a conservative gate, not a certification that every field is public.
Screenshots need human review and a matching SHA-256 approval; Git history is
checked separately with --history. No files or repository refs are modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

MEDIA = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.pdf', '.mp4'}
PRIVATE_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.pem', '.key', '.p12', '.pfx', '.orig', '.rej'}
SIGNATURES = (
    ('private-key', re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ('github-token', re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b')),
    ('api-key', re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b')),
)
MAX_TEXT_BYTES = 2 * 1024 * 1024


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(root), *args])


def path_issue(name: str, approvals: dict[str, str]) -> str | None:
    path = PurePosixPath(name)
    basename = path.name.lower()
    if basename == '.env' or (basename.startswith('.env.') and basename != '.env.example'):
        return 'private-environment'
    if (path.suffix.lower() in PRIVATE_SUFFIXES or basename.endswith(('.db-wal', '.db-shm'))
            or 'backups' in path.parts or 'webvpn-profile' in path.parts
            or 'statistics_exports' in path.parts
            or basename.startswith(('webvpn-storage-state', 'webvpn-session-', '.webvpn-diagnostics'))):
        return 'private-runtime-or-backup'
    if path.parts[0] in {'data', 'logs', 'share', 'exports', 'private'} and basename != '.gitkeep':
        return 'private-runtime-directory'
    if path.suffix.lower() in MEDIA and name not in approvals:
        return 'media-needs-review'
    return None


def content_issue(name: str, content: bytes, approvals: dict[str, str]) -> str | None:
    if PurePosixPath(name).suffix.lower() in MEDIA:
        if hashlib.sha256(content).hexdigest() != approvals.get(name):
            return 'media-approval-hash-mismatch'
        return None
    if len(content) > MAX_TEXT_BYTES:
        return 'large-file-needs-review'
    if b'\x00' in content:
        return 'binary-needs-review'
    for label, pattern in SIGNATURES:
        if pattern.search(content):
            return label
    return None


def audit(root: Path, *, history: bool = False) -> list[tuple[str, str, str]]:
    approvals_path = root / 'public-assets.json'
    approvals = json.loads(approvals_path.read_text()) if approvals_path.exists() else {}
    if not isinstance(approvals, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                             or not re.fullmatch('[0-9a-f]{64}', v) for k, v in approvals.items()):
        raise ValueError('invalid media approval file')
    findings = []
    names = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').decode().split('\0')
    for name in sorted(set(filter(None, names))):
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue  # An intentional working-tree deletion is not a release file.
        issue = path_issue(name, approvals)
        if path.is_symlink():
            issue = 'symlink-needs-review'
        elif path.is_dir():
            issue = 'external-tree-needs-review'
        if issue is None:
            if path.stat().st_size > MAX_TEXT_BYTES and path.suffix.lower() not in MEDIA:
                issue = 'large-file-needs-review'
            else:
                issue = content_issue(name, path.read_bytes(), approvals)
        if issue:
            findings.append(('worktree', name, issue))
    # A staged blob may differ from the file on disk (or the file may be deleted
    # only in the worktree). Check the index independently before any commit.
    for entry in git(root, 'ls-files', '--stage', '-z').split(b'\0'):
        if not entry:
            continue
        info, raw_name = entry.split(b'\t', 1)
        mode, oid, stage = info.decode().split()
        name = raw_name.decode()
        issue = path_issue(name, approvals)
        if stage != '0' or mode in {'120000', '160000'}:
            issue = 'unmerged-or-external-index-entry'
        if issue is None:
            size = int(git(root, 'cat-file', '-s', oid))
            if size > MAX_TEXT_BYTES and PurePosixPath(name).suffix.lower() not in MEDIA:
                issue = 'large-file-needs-review'
            else:
                issue = content_issue(name, git(root, 'cat-file', 'blob', oid), approvals)
        if issue:
            findings.append(('index', name, issue))
    if history:
        seen = set()
        commits = git(root, 'rev-list', '--all').decode().splitlines()
        for commit in commits:
            for entry in git(root, 'ls-tree', '-r', '-z', commit).split(b'\0'):
                if not entry:
                    continue
                info, raw_name = entry.split(b'\t', 1)
                mode, kind, oid = info.decode().split()
                name = raw_name.decode()
                key = (name, oid)
                if key in seen:
                    continue
                seen.add(key)
                issue = path_issue(name, approvals)
                if kind != 'blob' or mode == '120000':
                    issue = 'external-tree-or-symlink-needs-review'
                if issue is None:
                    size = int(git(root, 'cat-file', '-s', oid))
                    if size > MAX_TEXT_BYTES and PurePosixPath(name).suffix.lower() not in MEDIA:
                        issue = 'large-file-needs-review'
                    else:
                        issue = content_issue(name, git(root, 'cat-file', 'blob', oid), approvals)
                if issue:
                    findings.append(('history:' + commit[:12], name, issue))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--history', action='store_true', help='also inspect every locally reachable commit/tree')
    args = parser.parse_args()
    findings = audit(args.root.resolve(), history=args.history)
    for scope, name, reason in findings:
        print(f'{scope}\t{reason}\t{json.dumps(name, ensure_ascii=False)}')
    print(f'Public release gate: {len(findings)} blocking findings. Manual privacy review is still required.')
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
