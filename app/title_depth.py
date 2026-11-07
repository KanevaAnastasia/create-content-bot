"""Per-frame foreground segmentation and layered title composition, fully local."""
from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

from PIL import Image

LOGGER = logging.getLogger('bot.title_depth')
ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / 'artifacts/models/birefnet-general-lite.onnx'
MODEL_MD5 = '4fab47adc4ff364be1713e97b7e66334'
MODEL_URL = 'https://github.com/danielgatis/rembg/releases/download/v0.0.0/BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx'
BACK_MARKER = 'CF_DEPTH_BACK'
_session = None
_lock = threading.Lock()


def model_session():
    global _session
    with _lock:
        if _session is None:
            import onnxruntime as ort
            ort.disable_telemetry_events()
            if not MODEL_PATH.is_file():
                raise RuntimeError('Модель выделения персонажа не установлена. Запустите scripts/setup_title_depth.py.')
            with MODEL_PATH.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'md5').hexdigest()
            if digest != MODEL_MD5:
                raise RuntimeError('Файл модели выделения персонажа повреждён; повторите setup_title_depth.py.')
            options = ort.SessionOptions()
            options.intra_op_num_threads = 4
            options.inter_op_num_threads = 1
            _session = ort.InferenceSession(str(MODEL_PATH), sess_options=options,
                                           providers=['CPUExecutionProvider'])
        return _session


def foreground_mask(frame: Image.Image) -> Image.Image:
    import numpy as np
    model = model_session()
    pixels = np.asarray(frame.convert('RGB').resize((1024, 1024), Image.Resampling.LANCZOS), dtype=np.float32)
    pixels /= max(float(pixels.max()), 1e-6)
    pixels = (pixels - np.array([.485,.456,.406], dtype=np.float32)) / np.array([.229,.224,.225], dtype=np.float32)
    tensor = np.transpose(pixels, (2,0,1))[None]
    logits = model.run(None, {model.get_inputs()[0].name: tensor})[0][0,0]
    mask = 1 / (1 + np.exp(-np.clip(logits, -80, 80)))
    # Do not stretch near-constant predictions into an opaque arbitrary silhouette.
    low, high = float(mask.min()), float(mask.max())
    if high - low > .05:
        mask = (mask-low)/(high-low)
    mask = np.uint8(np.clip(mask, 0, 1)*255)
    return Image.fromarray(mask).resize(frame.size, Image.Resampling.LANCZOS)



def track_mask(previous, current, mask):
    """Backward optical flow maps each new pixel into the previous mask."""
    import cv2
    import numpy as np
    before = cv2.cvtColor(np.asarray(previous), cv2.COLOR_RGB2GRAY)
    after = cv2.cvtColor(np.asarray(current), cv2.COLOR_RGB2GRAY)
    if float(cv2.absdiff(before, after).mean()) > 35:
        return None  # Scene cut: a mask from the previous shot is never reused.
    flow = cv2.calcOpticalFlowFarneback(after, before, None, .5, 3, 21, 4, 7, 1.5, 0)
    yy, xx = np.mgrid[:after.shape[0], :after.shape[1]].astype(np.float32)
    result = cv2.remap(np.asarray(mask), xx+flow[:,:,0], yy+flow[:,:,1],
                       cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return Image.fromarray(result)

def has_depth(path: Path) -> bool:
    return BACK_MARKER in path.read_text(encoding='utf-8')


def seconds(value):
    h,m,s = value.split(':')
    return int(h)*3600 + int(m)*60 + float(s)


def split_ass(source: Path, directory: Path):
    """Keep dim below the subject, and all non-background text above it."""
    source_text = source.read_text(encoding='utf-8')
    base,back,front = [],[],[]
    intervals = []
    for line in source_text.splitlines():
        if not line.startswith('Dialogue:'):
            base.append(line);back.append(line);front.append(line)
            continue
        fields = line.split(',', 9)
        if len(fields) != 10:
            raise ValueError('Malformed title ASS event')
        if fields[4] == BACK_MARKER:
            back.append(line)
            intervals.append((seconds(fields[1]), seconds(fields[2])))
        elif fields[0] == 'Dialogue: 0':
            base.append(line);back.append(line)
        else:
            front.append(line)
    paths = [directory/name for name in ('depth_base.ass','depth_back.ass','depth_front.ass')]
    for path, lines in zip(paths, (base,back,front)):
        path.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    if not intervals:
        raise ValueError('No background title interval')
    return (*paths, intervals)


def video_fps(ffmpeg: str, source: Path) -> float:
    result = subprocess.run([ffmpeg,'-hide_banner','-i',str(source)],capture_output=True,text=True,timeout=30)
    match = re.search(r'(\d+(?:\.\d+)?) fps',result.stderr)
    if not match:
        raise RuntimeError('Не удалось определить частоту кадров для маски персонажа.')
    return max(1.0,min(60.0,float(match.group(1))))


def build_mask_video(ffmpeg: str, source: Path, intervals, *, segment=None) -> Path:
    """Cache by source bytes, exact intervals and model version; track every frame.

    Last frame is always black so FFmpeg's EOF repeat cannot obscure later text.
    Extraction uses the movie's presentation timeline, including variable-rate input.
    """
    segment = segment or foreground_mask
    fps = video_fps(ffmpeg,source)
    with source.open('rb') as stream:
        source_hash = hashlib.file_digest(stream,'sha256').hexdigest()
    key = hashlib.sha256(json.dumps([source_hash,intervals,fps,MODEL_MD5,'depth-flow-v2']).encode()).hexdigest()
    cache = ROOT/'artifacts/title_depth_masks'
    cache.mkdir(parents=True,exist_ok=True)
    target = cache/f'{key}.mkv'
    if target.is_file():
        return target
    # Verify dependencies before decoding frames, and use bounded extraction.
    if segment is foreground_mask:
        model_session()
    end = max(b for a,b in intervals)
    if not 0 < end <= 120:
        raise ValueError('Invalid title interval for foreground mask')
    count = math.ceil(end*fps)+1
    with tempfile.TemporaryDirectory(prefix='cf_title_mask_') as tmp:
        work = Path(tmp)
        subprocess.run([ffmpeg,'-y','-i',str(source),'-t',f'{end+1/fps:.6f}',
                        '-vf',f'fps={fps:.8f},scale=360:640:force_original_aspect_ratio=decrease',
                        '-frames:v',str(count),str(work/'input_%06d.png')],
                       check=True,capture_output=True,timeout=180)
        frames = sorted(work.glob('input_*.png'))
        if not frames:
            raise RuntimeError('Не удалось прочитать кадры для выделения персонажа.')
        start = time.monotonic()
        last_size = (360,640)
        previous_frame = previous_mask = None
        last_inference = -999
        refresh_frames = max(1, round(fps * .5))
        for i,path in enumerate(frames):
            with Image.open(path) as image:
                frame = image.convert('RGB')
            t=i/fps
            active = any(a <= t < b for a,b in intervals)
            tracked = (track_mask(previous_frame, frame, previous_mask)
                       if active and previous_frame is not None else None)
            if active and (tracked is None or i-last_inference >= refresh_frames):
                mask = segment(frame)
                last_inference = i
            else:
                mask = tracked if active else Image.new('L',frame.size)
            previous_frame,previous_mask = (frame,mask) if active else (None,None)
            last_size = mask.size
            mask.save(work/f'mask_{i+1:06d}.png')
            if i%12==0:
                LOGGER.info('title_depth.mask_frame frame=%d total=%d elapsed=%.1f',i+1,len(frames),time.monotonic()-start)
        Image.new('L',last_size).save(work/f'mask_{len(frames)+1:06d}.png')
        output = work/'mask.mkv'
        subprocess.run([ffmpeg,'-y','-framerate',str(fps),'-i',str(work/'mask_%06d.png'),
                        '-c:v','ffv1','-pix_fmt','gray',str(output)],check=True,capture_output=True,timeout=180)
        # Atomic placement avoids exposing a partial cache to another build.
        import shutil
        pending = cache/f'{key}.{Path(tmp).name}.part'
        shutil.copyfile(output,pending)
        pending.replace(target)
        (cache/f'{key}.json').write_text(json.dumps({'source_sha256':source_hash,'fps':fps,
            'intervals':intervals,'frames':len(frames),'model_md5':MODEL_MD5},indent=2))
        return target


def render_background(ffmpeg: str, source: Path, ass_path: Path, directory: Path):
    from .post_production import _subtitle_filter, _run_ffmpeg, _video_dimensions, FFMPEG_UPSCALE_TIMEOUT_SEC
    base,back,front,intervals=split_ass(ass_path,directory)
    mask=build_mask_video(ffmpeg,source,intervals)
    width,height=_video_dimensions(source)
    if not width or not height:
        raise RuntimeError('Не удалось определить размер видео для маски персонажа.')
    output=directory/'depth_background.mp4'
    graph=(f'[0:v]split[background][foreground];'
           f'[background]{_subtitle_filter(back)},format=gbrp[background_text];'
           f'[foreground]{_subtitle_filter(base)},format=gbrp[foreground_dim];'
           f'[1:v]scale={width}:{height}:flags=lanczos,format=gbrp[mask];'
           '[background_text][foreground_dim][mask]maskedmerge[video]')
    _run_ffmpeg([ffmpeg,'-y','-i',str(source),'-i',str(mask),'-filter_complex',graph,
                 '-map','[video]','-map','0:a?','-c:v','libx264','-preset','medium','-crf','18',
                 '-pix_fmt','yuv420p','-c:a','copy','-movflags','+faststart',str(output)],
                timeout=FFMPEG_UPSCALE_TIMEOUT_SEC,stage='title_depth_composite')
    return output,front
