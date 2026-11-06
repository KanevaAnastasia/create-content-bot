"""Technical checks complement (and do not replace) side-by-side visual review."""
from pathlib import Path
import sys,json,subprocess,hashlib
sys.path[:0]=['/tmp/advice-font-tools','/opt/content_fabric']
from fontTools.ttLib import TTFont
from PIL import Image
from app.ai_approval_service import _resolve_ffmpeg
R=Path('/opt/content_fabric');O=R/'artifacts/editorial_titles/review_v4';B=O.parent
manifest=json.loads((O/'unicode_font_manifest.json').read_text());alphabet='АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюяABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789?!'
report={'fonts':[],'videos':[],'images':0,'visual_status':'reviewed_comparisons_available_not_exact_match_certified'}
for n,roles in manifest.items():
 for role in roles:
  p=O/'unicode_fonts'/role['font'];f=TTFont(p,checkChecksums=2);cmap=f.getBestCmap();assert all(ord(c) in cmap for c in alphabet)
  composite=[name for name in f.getGlyphOrder() if f['glyf'][name].isComposite()];assert not composite,(p,composite[:3])
  report['fonts'].append({'file':p.name,'ru_en_coverage':'pass','compound_glyphs':0,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
# Regression check for the actual repeated-transform failure on shared Latin/Cyrillic outlines.
f=TTFont(O/'unicode_fonts/CF Reviewed 6 3.ttf');cmap=f.getBestCmap()
for a,b in [('Н','H')]:
 ga=f['glyf'][cmap[ord(a)]];gb=f['glyf'][cmap[ord(b)]]
 assert abs((ga.xMax-ga.xMin)-(gb.xMax-gb.xMin))<=2 and (ga.yMin,ga.yMax)==(gb.yMin,gb.yMax),(a,b)
report['shared_glyph_transform_regression']='pass'

# The high-contrast heading must retain its fine crossbar rather than reverting to monoline.
from PIL import ImageDraw,ImageFont
font=ImageFont.truetype(str(O/'unicode_fonts/CF Reviewed 9 1.ttf'),1000);bb=font.getbbox('П');mask=Image.new('L',(bb[2]-bb[0]+10,bb[3]-bb[1]+10));ImageDraw.Draw(mask).text((5-bb[0],5-bb[1]),'П',font=font,fill=255);mask=mask.crop(mask.getbbox())
bar=0
for y in range(mask.height):
 if mask.getpixel((mask.width//2,y))>128:bar+=1
 else:break
assert .02<bar/mask.height<.055,('heading contrast',bar,mask.height)
report['style9_crossbar_to_cap_height']=round(bar/mask.height,4)

for n in manifest:
 for lang in ['ru','en']:
  for i in range(1,6):
   p=O/f'style_{n}/{lang}_phrase_{i}.jpg'
   with Image.open(p) as im:assert im.size==(720,1280);im.verify()
   report['images']+=1
  p=O/f'style_{n}/{lang}_preview.mp4'
  run=subprocess.run([_resolve_ffmpeg(),'-v','error','-i',str(p),'-f','null','-'],capture_output=True,text=True)
  assert run.returncode==0 and not run.stderr,(p,run.stderr)
  report['videos'].append({'file':str(p.relative_to(O)),'full_decode':'pass','bytes':p.stat().st_size})
  timing=json.loads((B/f'timed_title/{lang}.json').read_text());starts=[r[1] for r in timing['words']];assert len(starts)==3 and starts==sorted(starts)
report['technical_status']='pass'
(O/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(f"PASS: {len(report['fonts'])} decomposed RU/EN fonts, {report['images']} images, {len(report['videos'])} fully decoded videos; shared-glyph transform regression")
