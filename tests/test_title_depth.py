from pathlib import Path
import subprocess

import pytest
from PIL import Image, ImageDraw

from app import title_depth
from app.ai_approval_service import _resolve_ffmpeg
from app.post_production import _write_burned_subtitle_ass, _render_subtitled_music_master


def write_title(path, sid='9'):
    _write_burned_subtitle_ass(path,[],title_cues=[{'text':'Почему не ты?','language':'ru',
        'start_sec':0,'end_sec':1}],title_style={'editorial_style_id':sid},video_width=720,video_height=1280)


def test_split_keeps_only_upper_text_behind_and_dim_below_subject(tmp_path, title_event_ink):
    source=tmp_path/'title.ass';write_title(source)
    assert title_depth.has_depth(source)
    base,back,front,intervals=title_depth.split_ass(source,tmp_path)
    event_lines=lambda p:[s for s in p.read_text().splitlines() if s.startswith('Dialogue:')]
    assert len(event_lines(base))==1
    assert len(event_lines(back))==2
    assert len(event_lines(front))==2
    assert all(title_depth.BACK_MARKER not in line for line in event_lines(front))
    assert intervals==[(0,1)]
    assert 'PlayResY: 1280' in front.read_text()


@pytest.mark.parametrize('sid',['1','2','5'])
def test_front_styles_do_not_require_segmentation(tmp_path,sid,title_event_ink):
    source=tmp_path/'title.ass';write_title(source,sid)
    assert not title_depth.has_depth(source)


def test_mask_cache_tracks_source_and_ends_with_black_frame(tmp_path,monkeypatch):
    ffmpeg=_resolve_ffmpeg()
    if not ffmpeg:pytest.skip('ffmpeg missing')
    monkeypatch.setattr(title_depth,'ROOT',tmp_path)
    source=tmp_path/'source.mp4'
    subprocess.run([ffmpeg,'-y','-f','lavfi','-i','color=c=blue:s=72x128:r=10:d=1',
                    '-c:v','libx264','-pix_fmt','yuv420p',str(source)],check=True,capture_output=True)
    calls=[]
    def segment(frame):
        calls.append(frame.size)
        return Image.new('L',frame.size,255)
    mask=title_depth.build_mask_video(ffmpeg,source,[(.2,.5)],segment=segment)
    assert len(calls)==1
    assert title_depth.build_mask_video(ffmpeg,source,[(.2,.5)],segment=segment)==mask
    assert len(calls)==1
    # Endpoint of the cache is black: repeated EOF cannot cover text later on.
    last=tmp_path/'last.png'
    subprocess.run([ffmpeg,'-y','-sseof','-0.1','-i',str(mask),'-frames:v','1',str(last)],check=True,capture_output=True)
    assert Image.open(last).convert('L').getextrema()==(0,0)


def test_real_compositor_hides_back_text_but_keeps_front_text_and_audio(tmp_path,monkeypatch):
    ffmpeg=_resolve_ffmpeg()
    if not ffmpeg:pytest.skip('ffmpeg missing')
    source=tmp_path/'source.mp4'
    subprocess.run([ffmpeg,'-y','-f','lavfi','-i','color=c=blue:s=120x160:r=10:d=1',
                    '-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264',
                    '-pix_fmt','yuv420p','-c:a','aac',str(source)],check=True,capture_output=True)
    mask=tmp_path/'mask.mkv'
    image=Image.new('L',(120,160));ImageDraw.Draw(image).rectangle((40,0,79,159),fill=255)
    image.save(tmp_path/'mask.png')
    subprocess.run([ffmpeg,'-y','-loop','1','-i',str(tmp_path/'mask.png'),'-t','1','-r','10',
                    '-c:v','ffv1','-pix_fmt','gray',str(mask)],check=True,capture_output=True)
    monkeypatch.setattr(title_depth,'build_mask_video',lambda *args,**kwargs:mask)
    ass=tmp_path/'title.ass'
    _write_burned_subtitle_ass(ass,[],video_width=120,video_height=160)
    text=ass.read_text()
    def event(layer,name,color,top,bottom):
        return (f'Dialogue: {layer},0:00:00.00,0:00:01.00,Default,{name},0,0,0,,'
                f'{{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H{color}&\\p1}}'
                f'm 0 {top} l 120 {top} 120 {bottom} 0 {bottom}{{\\p0}}\n')
    text+=event(1,title_depth.BACK_MARKER,'FFFFFF',20,40)
    text+=event(2,'','00FF00',90,110)
    ass.write_text(text)
    dest=tmp_path/'output.mp4'
    assert _render_subtitled_music_master(ffmpeg=ffmpeg,clean_path=source,subtitles_path=ass,
                                       music_path=None,music_volume=0,dest_path=dest) is True
    frame=tmp_path/'result.png'
    subprocess.run([ffmpeg,'-y','-ss','0.5','-i',str(dest),'-frames:v','1',str(frame)],check=True,capture_output=True)
    im=Image.open(frame).convert('RGB')
    # White behind text remains at the side, blue foreground covers its center.
    assert min(im.getpixel((20,30)))>220
    r,g,b=im.getpixel((60,30));assert b>180 and r<40 and g<40
    # The front green text remains visible even in the subject's mask.
    r,g,b=im.getpixel((60,100));assert g>180 and r<40 and b<40


def test_optical_flow_follows_subject_and_rejects_scene_cuts():
    import numpy as np
    rng=np.random.default_rng(42)
    patch=rng.integers(30,255,size=(40,40,3),dtype=np.uint8)
    first=np.zeros((100,100,3),dtype=np.uint8);second=first.copy()
    first[30:70,25:65]=patch;second[30:70,31:71]=patch
    mask=Image.new('L',(100,100));ImageDraw.Draw(mask).rectangle((25,30,64,69),fill=255)
    tracked=title_depth.track_mask(Image.fromarray(first),Image.fromarray(second),mask)
    assert tracked.getpixel((68,50))>200  # The right edge followed the six-pixel motion.
    assert tracked.getpixel((26,50))<50
    assert title_depth.track_mask(Image.new('RGB',(100,100),'black'),Image.new('RGB',(100,100),'white'),mask) is None


@pytest.mark.parametrize('sid', ['3', '9'])
@pytest.mark.parametrize('language,text', [('ru', 'А что если?'), ('en', 'What if?')])
def test_reframe_phrase_never_goes_behind_subject(tmp_path, sid, language, text, title_event_ink):
    source = tmp_path / 'pivot.ass'
    _write_burned_subtitle_ass(source, [], title_cues=[{
        'text': text, 'language': language, 'start_sec': 32, 'end_sec': 34,
    }], title_style={'editorial_style_id': sid}, video_width=720, video_height=1280)
    assert not title_depth.has_depth(source)
    events = [line for line in source.read_text().splitlines() if line.startswith('Dialogue:')]
    assert len(events) > 1
    assert all(line.startswith('Dialogue: 2,') for line in events[1:])
