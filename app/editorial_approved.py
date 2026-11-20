"""Emit approved editorial titles as timed ASS paths. Keep the old review helpers for reference."""
from functools import lru_cache
from pathlib import Path
import json
import re

from PIL import Image, ImageDraw, ImageFont, ImageFilter

ASSETS = Path(__file__).resolve().parents[1] / 'fonts' / 'editorial_approved'
IDS = ('1', '2', '3', '5', '9')
W, H = 720, 1280


@lru_cache(maxsize=4)
def data(name):
    return json.loads((ASSETS / name).read_text())


def shape(text, sid, role):
    rules = data('character_alternates.json').get(f'{sid}:{role}', [])
    def word(match):
        chars = list(match.group())
        for rule in rules:
            at = 0 if rule['when'] == 'initial' else len(chars) - 1
            if chars[at] == rule['char']:
                chars[at] = chr(rule['codepoint'])
        return ''.join(chars)
    return re.sub(r'[^\W\d_]+', word, text)


@lru_cache(maxsize=128)
def part_layer(sid, lang, role, text, visible_words=None):
    record = data('reference_geometry.json')[sid][lang][role]
    original = record['text']
    layer = Image.new('RGBA', (W, H))
    if not text or visible_words == 0:
        return layer
    # The original three-word approved title retains exact outlines and ornaments.
    if text.casefold().rstrip('.!') == original.casefold().rstrip('.!'):
        with Image.open(ASSETS / f'{sid}_{lang}_{role}.png') as image:
            return image.convert('RGBA')
    if original.isupper():
        text = text.upper()
    elif original[0].islower():
        text = text.lower()
    full = shape(text, sid, role)
    font = ImageFont.truetype(str(ASSETS / data('unicode_font_manifest.json')[sid][role]['font']), 600)
    measured = font.getlength(full)
    if measured > 8192:
        font = ImageFont.truetype(font.path, max(1, int(600 * 8192 / measured)))
    box = font.getbbox(full)
    art = Image.new('RGBA', (box[2]-box[0]+24, box[3]-box[1]+24))
    ImageDraw.Draw(art).text((12-box[0],12-box[1]), full, font=font, fill=tuple(record['color']))
    crop = art.getbbox()
    if not crop:
        return layer
    if visible_words is not None and visible_words < len(text.split()):
        art = Image.new('RGBA', art.size)
        partial = shape(' '.join(text.split()[:visible_words]), sid, role)
        ImageDraw.Draw(art).text((12-box[0],12-box[1]), partial, font=font, fill=tuple(record['color']))
    art = art.crop(crop)
    x,y,mw,mh = record['box']
    x,y,mw,mh = round(x*W/660),round(y*H/1182),round(mw*W/660),round(mh*H/1182)
    if sid == '1':
        x,y,mw,mh = [(40,560,640,240),(40,825,140,65),(40,930,640,230)][role]
    if role in (0,2) and len(text)>5:
        x,mw = 40,640
    if role == 1 and len(text)>len(original):
        x=max(35,x-20);mw=min(220,W-x-35)
    if role == 0 and sid in ('3','9') and len(text)<=3:
        x,mw=50,180
    original_box = font.getbbox(shape(original,sid,role))
    preferred = mh/max(1,original_box[3]-original_box[1])
    if role==1:
        preferred=max(preferred,mh*.65/art.height)
    scale=min(preferred,mw/art.width/(.72 if role==2 else .9))
    art=art.resize((min(mw,max(1,round(art.width*scale))),max(1,round(art.height*scale))),Image.Resampling.LANCZOS)
    if sid=='1' and role==0:
        art=art.rotate(9,Image.Resampling.BICUBIC,expand=True)
        outline=Image.new('RGBA',art.size,'#36251f')
        outline.putalpha(art.getchannel('A').filter(ImageFilter.MaxFilter(3)))
        outline.alpha_composite(art);art=outline
        if art.width>mw:
            art=art.resize((mw,round(art.height*mw/art.width)),Image.Resampling.LANCZOS)
    layer.alpha_composite(art,(x+(mw-art.width)//2,y+(mh-art.height)//2))
    return layer


def rectangles(mask):
    """Merge identical horizontal runs vertically; preserves counters and thin strokes."""
    pixels=mask.load(); active={}; result=[]
    for y in range(mask.height+1):
        spans=[]
        if y<mask.height:
            x=0
            while x<mask.width:
                if pixels[x,y]<128:
                    x+=1;continue
                start=x
                while x<mask.width and pixels[x,y]>=128:
                    x+=1
                spans.append((start,x))
        current=set(spans)
        for span,top in list(active.items()):
            if span not in current:
                a,b=span
                result.append(f'm {a} {top} l {b} {top} {b} {y} {a} {y}')
                del active[span]
        for span in spans:
            active.setdefault(span,y)
    return ' '.join(result)


@lru_cache(maxsize=128)
def drawings(sid,lang,role,text,visible_words=None):
    image=part_layer(sid,lang,role,text,visible_words)
    alpha=image.getchannel('A')
    if not alpha.getbbox():
        return ()
    color=data('reference_geometry.json')[sid][lang][role]['color']
    if sid=='1' and role==0:
        # Separate the approved dark outline from the light coral face.
        face=Image.new('L', image.size)
        face.putdata([a if r>150 else 0 for r,g,b,a in image.getdata()])
        return (('#36251f',rectangles(alpha)), ('#ff9a68',rectangles(face)))
    return (('#'+''.join(f'{c:02x}' for c in color),rectangles(alpha)),)


def ass_events(cue,style,width,height,base_style):
    from .editorial_titles import fallback_parts,valid_parts,_timestamp
    sid=str(style['editorial_style_id']);lang='en' if cue.get('language')=='en' else 'ru'
    title=str(cue.get('text') or '')
    # The narrative pivot must remain readable even in depth-enabled presets.
    normalized_title = ' '.join(re.findall(r'[^\W_]+', title.casefold()))
    always_front = bool(cue.get('always_foreground')) or normalized_title in ('а что если', 'what if')
    parts=(style.get('semantic_parts_by_lang') or {}).get(lang)
    if not valid_parts(title,parts):
        parts=fallback_parts(title)
    roles=[0,1,2] if len(parts)==3 else ([0,2] if len(parts)==2 else [2])
    start=float(cue.get('start_sec') or 0);end=float(cue.get('end_sec') or 0)
    timing=cue.get('word_timing') if 'word_timing' in cue else (style.get('word_timing_by_lang') or {}).get(lang)
    timing=timing or []
    clean=lambda s: re.sub(r'[^\w]+','',s).casefold()
    if [clean(row.get('text','')) for row in timing] != [clean(word) for word in title.split()]:
        timing=[]
    if timing:
        start=max(start,float(timing[0]['start']))
        # Keep the completed headline briefly, then return to the undimmed video.
        timed_end=max(start+.01,float(timing[-1]['end'])+.35)
        end=min(end,timed_end) if 'word_timing' in cue else timed_end
    if end<=start:
        return []
    try:
        opacity=min(.6,max(0,float(style.get('dim_opacity',82/255))))
    except (TypeError,ValueError):
        opacity=82/255
    def event(layer,t0,t1,tags,path,name=""):
        return f'Dialogue: {layer},{_timestamp(t0)},{_timestamp(t1)},{base_style},{name},0,0,0,,{tags}{path}{{\\p0}}'
    alpha=round(255*(1-opacity))
    output=[event(0,start,end,f'{{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H000000&\\1a&H{alpha:02X}&\\p1}}',f'm 0 0 l {width} 0 {width} {height} 0 {height}')]
    from . import editorial_font_selection as selected
    semantic_parts = tuple(zip(roles, parts))
    offset=0
    for role,text in zip(roles,parts):
        count=len(text.split())
        states=[(None,start,end)] if not timing else [
            (k,float(timing[offset+k-1]['start']),float(timing[offset+k]['start']) if k<count else end)
            for k in range(1,count+1)]
        for visible,t0,t1 in states:
            if t1<=t0:
                continue
            ink = selected.drawings(sid, lang, semantic_parts, role, visible, foreground=always_front)
            for color,path in ink:
                bgr=color[5:7]+color[3:5]+color[1:3]
                tags=(f'{{\\an7\\pos(0,0)\\fscx{width/W*100:.5f}\\fscy{height/H*100:.5f}'
                      f'\\bord0\\shad0\\1c&H{bgr}&\\1a&H00&\\p1}}')
                behind = role == 0 and sid in ('3','9') and not always_front
                output.append(event(1 if behind else 2,max(start,t0),t1,tags,path,
                                    'CF_DEPTH_BACK' if behind else ''))
        offset+=count
    return output
