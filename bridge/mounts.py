"""Discover user directory mounts without requiring Docker socket access."""
import re
from pathlib import Path, PurePosixPath


def media_mounts(text, data_dir):
    excluded = ['/app', '/data', '/proc', '/sys', '/dev', '/etc', '/usr',
                '/bin', '/sbin', '/lib', '/lib64', '/run', '/var/run', str(data_dir)]
    roots = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or '-' not in fields:
            continue
        separator = fields.index('-')
        if separator + 1 >= len(fields):
            continue
        filesystem = fields[separator + 1]
        if filesystem in ('overlay', 'proc', 'sysfs', 'tmpfs', 'devpts', 'cgroup', 'cgroup2'):
            continue
        mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
        path = PurePosixPath(mount)
        if not path.is_absolute() or mount == '/' or any(
                path == PurePosixPath(p) or PurePosixPath(p) in path.parents for p in excluded):
            continue
        if mount not in roots:
            roots.append(mount)
    return roots


def discover_media_mounts(data_dir):
    if not (Path('/.dockerenv').exists() or Path('/run/.containerenv').exists()):
        return []
    try:
        mounts = media_mounts(Path('/proc/self/mountinfo').read_text(encoding='utf-8'), data_dir)
        return [p for p in mounts if Path(p).is_dir() and not Path(p).is_symlink()]
    except OSError:
        return []


def apply_mount_roots(config, data_dir):
    mounts = discover_media_mounts(data_dir)
    for key in ('source_roots', 'target_roots', 'upload_roots'):
        roots = list(config.get(key, []))
        if key == 'upload_roots' and not roots:
            roots = list(dict.fromkeys(config['source_roots'] + config['target_roots']))
        for mount in mounts:
            if not any(Path(root) == Path(mount) or Path(root) in Path(mount).parents for root in roots):
                roots.append(mount)
        config[key] = roots
