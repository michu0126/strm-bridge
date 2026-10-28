"""Plan first. Never download video content while scanning or generating STRM."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from .progress import ScanProgress

VIDEO = set(('3g2 3gp 3gp2 3gpp amv asf avi avs bdav bik bink dat divx dpg dv evo f4v flc fli flv h264 h265 hdmov hevc img iso ivf m1v m2p m2t m2ts m2v m4v m4p mk3d mkv mov mp2v mp4 mpe mpeg mpg mpv mts mxf nsv nut ogm ogv ogx qt ram rec rm rmvb rpl smk svi tod tp trp ts vdr vob vro webm wm wmv wtv xvid y4m').split())
SUB = set('srt ass ssa sub idx vtt sup smi sami txt ttml dfxp sbv scc stl lrc usf aqt jss mpl pjs rt sst tts'.split())
SKIP_DIRS = {'.strm-trash', '.strm-sync-trash', '.snapshot', '#recycle', '@eaDir', '$RECYCLE.BIN', '.git', 'lost+found'}
CLEANUP_LIMIT = 100_000_000
EPISODE = re.compile(r'(?:s(\d{1,3})[ ._-]*e(\d{1,4})(?!\d)|(\d{1,3})x(\d{1,4})(?!\d))', re.I)

def inside(path, roots):
    p = Path(path).absolute()
    if p.is_symlink():
        raise ValueError('不接受符号链接：' + str(p))
    resolved = p.resolve()
    if not any(resolved.is_relative_to(Path(r).resolve()) for r in roots):
        raise ValueError('目录不在已挂载的允许范围内：' + str(p))
    return resolved

def signature(p):
    s = p.stat()
    return [s.st_size, s.st_mtime_ns, s.st_ino]

def episode(s):
    m = EPISODE.search(s)
    return tuple(int(x) for x in (m.group(1, 2) if m.group(1) else m.group(3, 4))) if m else None

def media_extensions(extra=''):
    extensions = VIDEO | {x.strip().lower().lstrip('.') for x in re.split(r'[,;\s]+', extra) if x.strip()}
    if any(not re.fullmatch('[a-z0-9]{1,16}', x) for x in extensions):
        raise ValueError('扩展名只能包含字母或数字')
    if extensions & (SUB | {'strm', 'nfo', 'json'}):
        raise ValueError('不能把字幕、STRM 或元数据作为视频扩展名')
    return extensions

def walk(root, cancel=lambda: False, visit=lambda folder: None):
    errors = []
    def onerror(e):
        errors.append(str(e))
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        if cancel():
            raise ValueError('任务已取消')
        visit(folder)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not Path(folder, d).is_symlink())
        for name in sorted(files):
            if cancel():
                raise ValueError('任务已取消')
            p = Path(folder, name)
            if not p.is_symlink() and p.is_file():
                yield p
    if errors:
        raise ValueError('扫描不完整，请检查挂载或权限：' + '; '.join(errors[:3]))

def subtitle_match(sub, videos):
    """Only infer unnamed subtitles for a single local video; never guess across episodes."""
    candidates = [v for v in videos if sub.parent == v.parent or
                  (sub.parent.name.lower() in ('subs', 'subtitles', '字幕') and sub.parent.parent == v.parent)]
    direct = [v for v in candidates if sub.stem.casefold() == v.stem.casefold() or
              sub.stem.casefold().startswith(v.stem.casefold() + '.') or
              sub.stem.casefold().startswith(v.stem.casefold() + ' ')]
    if direct:
        longest = max(len(v.stem) for v in direct)
        direct = [v for v in direct if len(v.stem) == longest]
        return direct[0] if len(direct) == 1 else None
    ep = episode(sub.stem)
    if ep:
        matches = [v for v in candidates if episode(v.stem) == ep]
        return matches[0] if len(matches) == 1 else None
    return candidates[0] if len(candidates) == 1 else None

def subtitle_tail(sub, video):
    if sub.stem.casefold().startswith(video.stem.casefold()):
        tail = sub.stem[len(video.stem):].strip(' ._-')
    else:
        # Keep useful language/forced labels without dragging release names into the library.
        tags = re.findall(r'(?:^|[ ._\-])(zh[-_]?cn|zh[-_]?tw|chs|cht|chi|zho|eng|en|ja|jpn|zh|简体|繁体|中字|forced|default|sdh)(?=$|[ ._\-])', sub.stem, re.I)
        tail = '.'.join(tags)
    return ('.' + tail if tail else '') + sub.suffix.lower()

def make_plan(options, config, cancel=lambda: False, progress=lambda info: None):
    src = inside(options['source'], config['source_roots'])
    dst = inside(options['target'], config['target_roots'])
    if not src.is_dir() or not dst.is_dir():
        raise ValueError('请选择已存在的源目录和目标目录')
    if dst.is_relative_to(src) or src.is_relative_to(dst):
        raise ValueError('源目录和目标目录不能相同或互相包含')
    kind = options.get('kind', 'movie')
    if kind not in ('movie', 'tv', 'jav'):
        raise ValueError('未知媒体类型')
    cleanup = options.get('cleanup', False) is True
    if cleanup and kind != 'jav':
        raise ValueError('仅 JAV 支持小文件清理')
    if options.get('subtitle_mode', 'copy') not in ('copy', 'move'):
        raise ValueError('字幕处理方式无效')
    mode = options.get('mode', config.get('default_mode', 'path'))
    if mode not in ('redirect', 'path'):
        raise ValueError('播放方式无效')
    if mode == 'redirect':
        from .resolver import cloud_path, valid_url
        valid_url(config['public_url'])
        cloud_path(src, config)
        if config.get('provider') == 'none':
            raise ValueError('尚未配置可选直链服务，请选择「挂载路径」即可独立生成')
    report = ScanProgress(progress)
    report.update(force=True)
    files, directories = [], set()
    def visiting(folder):
        directories.add(Path(folder))
        report.update(files=len(files), directories=len(directories), current=str(Path(folder).relative_to(src)))
    for p in walk(src, cancel, visiting):
        files.append(p)
        report.update(files=len(files), directories=len(directories), current=str(p.relative_to(src)))
    ext = media_extensions(options.get('extra_extensions', ''))
    videos = [p for p in files if p.suffix.lower().lstrip('.') in ext]
    report.update(force=True, phase='planning', videos=len(videos), current='整理生成清单')
    cleanup_files = []
    if cleanup:
        # Determine the media folders before filtering small video advertisements.
        parents = {v.parent for v in videos}
        cleanup_files = [p for p in files if p.parent in parents and p.stat().st_size < CLEANUP_LIMIT]
        cleanup_set = set(cleanup_files)
        videos = [v for v in videos if v not in cleanup_set]
    actions, warnings, used = [], [], set()
    targets = {}
    for v in videos:
        if cancel():
            raise ValueError('任务已取消')
        report.update(current=str(v.relative_to(src)))
        out = dst / v.relative_to(src).with_suffix('.strm')
        if out in used:
            out = out.with_name(v.name + '.strm')
        if out in used:
            raise ValueError('目标文件名冲突：' + str(out))
        used.add(out)
        targets[v] = out
        actions.append({'type': 'strm', 'source': str(v), 'target': str(out), 'signature': signature(v)})
    if kind in ('movie', 'tv'):
        by_parent = {}
        for video in videos:
            by_parent.setdefault(video.parent, []).append(video)
        for p in files:
            if cancel():
                raise ValueError('任务已取消')
            if p.suffix.lower().lstrip('.') not in SUB:
                continue
            parent = p.parent.parent if p.parent.name.lower() in ('subs', 'subtitles', '字幕') else p.parent
            video = subtitle_match(p, list(dict.fromkeys(by_parent.get(p.parent, []) + by_parent.get(parent, []))))
            if video is None:
                warnings.append('字幕匹配不唯一，已跳过：' + str(p.relative_to(src)))
                continue
            base = targets[video].stem
            tail = subtitle_tail(p, video)
            out = targets[video].parent / (base + tail)
            # VobSub .idx/.sub share the same suffix group, including duplicate-language pairs.
            group = hashlib.sha256(str(p.with_suffix('')).encode()).hexdigest()[:6]
            if out in used:
                out = out.with_name(base + '.' + group + tail)
            if out in used:
                raise ValueError('字幕目标文件冲突：' + str(out))
            used.add(out)
            actions.append({'type': options.get('subtitle_mode', 'copy'), 'source': str(p), 'target': str(out), 'signature': signature(p)})
    for p in cleanup_files:
        actions.append({'type': 'trash', 'source': str(p), 'signature': signature(p)})
    report.update(force=True, phase='ready', current='')
    return {'id': uuid.uuid4().hex, 'created': time.time(), 'options': dict(options, source=str(src), target=str(dst), mode=mode),
            'actions': actions, 'warnings': warnings, 'counts': {k: sum(a['type'] == k for a in actions) for k in ('strm', 'copy', 'move', 'trash')},
            'config_fingerprint': config_fingerprint(config)}

def config_fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

def exclusive_write(target, writer):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as f:
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        # hardlink commits a complete file, and refuses to overwrite an existing target.
        os.link(temp, target)
    finally:
        temp.unlink(missing_ok=True)

def execute(plan, config, playback_url, progress=lambda *a: None, cancel=lambda: False, journal=lambda *a: None, generated=None):
    if config_fingerprint(config) != plan['config_fingerprint']:
        raise ValueError('设置已变更，请重新预览')
    if time.time() - plan['created'] > 3600:
        raise ValueError('预览超过一小时，请重新扫描')
    src = inside(plan['options']['source'], config['source_roots'])
    dst = inside(plan['options']['target'], config['target_roots'])
    result = {'created': 0, 'skipped': 0, 'subtitles': 0, 'trashed': 0, 'errors': [], 'cancelled': False}
    failed = False
    for i, a in enumerate(plan['actions']):
        if cancel():
            result['cancelled'] = True
            break
        p = inside(a['source'], [src])
        try:
            if signature(p) != a['signature']:
                raise ValueError('源文件已变更，请重新预览')
            if a['type'] == 'trash':
                if failed:
                    raise ValueError('生成发生错误，本次清理已停止')
                if plan['options']['kind'] != 'jav' or not plan['options'].get('cleanup'):
                    raise ValueError('不允许清理')
                if p.stat().st_size >= CLEANUP_LIMIT:
                    raise ValueError('受保护文件')
                trash = inside(src / '.strm-trash' / plan['id'] / p.relative_to(src), [src])
                trash.parent.mkdir(parents=True, exist_ok=True)
                journal('pending_trash', {'source': str(p), 'trash': str(trash)})
                if trash.exists():
                    raise ValueError('回收站文件已存在')
                p.rename(trash)
                journal('trash', {'source': str(p), 'trash': str(trash)})
                result['trashed'] += 1
            else:
                out = inside(a['target'], [dst])
                if a['type'] == 'strm':
                    if plan['options'].get('mode', 'redirect') == 'path':
                        content = str(p)
                    else:
                        content = playback_url(p)
                    data = (content + '\n').encode('utf-8')
                    if generated:
                        result[generated.write(out, data, a)] += 1
                        generated.record(plan, a, data)
                    elif out.exists():
                        if out.stat().st_size == len(data) and out.read_bytes() == data:
                            result['skipped'] += 1
                        else:
                            raise ValueError('目标已存在且内容不同，已保留原文件')
                    else:
                        exclusive_write(out, lambda f: f.write(data))
                        result['created'] += 1
                else:
                    if out.exists():
                        if out.stat().st_size != p.stat().st_size or not same_file_content(p, out):
                            raise ValueError('已有同名字幕且内容不同，已保留原文件')
                        result['skipped'] += 1
                    else:
                        if p.stat().st_size > 128 * 1024 * 1024:
                            raise ValueError('字幕超过 128MiB，已跳过')
                        exclusive_write(out, lambda f: copy_to(p, f))
                        result['subtitles'] += 1
                    if signature(p) != a['signature']:
                        raise ValueError('复制期间源字幕发生变化，源文件已保留')
                    if a['type'] == 'move':
                        journal('subtitle_move', {'source': str(p), 'target': str(out)})
                        p.unlink()
            progress(i + 1, len(plan['actions']), str(p))
        except (OSError, ValueError) as e:
            failed = True
            result['errors'].append({'path': str(p), 'message': str(e)})
            progress(i + 1, len(plan['actions']), str(p))
    return result

def copy_to(p, f):
    with p.open('rb') as source:
        shutil.copyfileobj(source, f, 1024 * 1024)

def same_file_content(a, b):
    def digest(p):
        with p.open('rb') as f:
            return hashlib.file_digest(f, 'sha256').digest()
    return digest(a) == digest(b)
