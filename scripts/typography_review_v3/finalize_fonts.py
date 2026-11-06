from pathlib import Path
import sys,json,copy,string
from PIL import Image,ImageDraw,ImageFont
sys.path.insert(0,'/tmp/advice-font-tools')
from fontTools.ttLib import TTFont
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
def draw_flat(gs,name,pen,matrix):
 record=DecomposingRecordingPen(gs);gs[name].draw(record);record.replay(TransformPen(pen,matrix))
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
R=Path('/opt/content_fabric');O=R/'artifacts/editorial_titles/review_v3';D=O/'unicode_fonts';manifest=json.loads((O/'unicode_font_manifest.json').read_text());alt_records={}
# Full architectural alphabet: flared stem terminals on every capital, not only on ПОЧЕМУ.
p=D/'CF Reviewed 7 1.ttf';f=TTFont(p);cmap=f.getBestCmap();gs=f.getGlyphSet();pf=ImageFont.truetype(str(p),1000)
known=set('ПОЧЕМУOEMWHYHN');caps='АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ'+string.ascii_uppercase
for ch in caps:
 if ch in known:continue
 name=cmap[ord(ch)];g=f['glyf'][name];pen=TTGlyphPen(gs)
 for dx in [-5,5]:draw_flat(gs,name,pen,(1,0,0,1,dx,0))
 ink=Image.new('L',(1500,1800));ImageDraw.Draw(ink).text((500,1400),ch,font=pf,fill=255,anchor='ls')
 for y,inside in [(0,30),(810,780)]:
  row=1400-(15 if y==0 else 795);row2=1400-inside;pix=ink.load();runs=[]
  for x in range(250,1100):
   if pix[x,row]>128:
    if not runs or x>runs[-1][1]+1:runs.append([x,x])
    else:runs[-1][1]=x
  for left,right in runs:
   if right-left<25:continue
   # Add flares only where a stroke continues inward, not to the bottom of a round bowl.
   mid=(left+right)//2
   if pix[mid,row2]<128:continue
   a=left-500;b=right-500;dy=1 if y==0 else -1
   pen.moveTo((a-20,y));pen.qCurveTo((a-2,y+5*dy),(a,y+35*dy));pen.lineTo((b,y+35*dy));pen.qCurveTo((b+2,y+5*dy),(b+20,y));pen.closePath()
 ng=pen.glyph();ng.recalcBounds(f['glyf']);f['glyf'][name]=ng;advance,_=f['hmtx'][name];f['hmtx'][name]=(advance+6,ng.xMin)
# Small capitals preserve the same construction for lower-case input.
for ch in caps:
 low=ch.lower()
 if low==ch or ord(low) not in cmap:continue
 gs=f.getGlyphSet();pen=TTGlyphPen(gs);draw_flat(gs,cmap[ord(ch)],pen,(.88,0,0,.78,0,0));g=pen.glyph();g.recalcBounds(f['glyf']);name=cmap[ord(low)];f['glyf'][name]=g;advance,_=f['hmtx'][cmap[ord(ch)]];f['hmtx'][name]=(round(advance*.88),g.xMin)
f.save(p)
# Final/initial swashes are character alternates, not complete-word substitutes.
for n,role,chars,basepath,shear,sy in [(6,0,[('у','final'),('y','final')],R/'artifacts/editorial_titles/style1_letters_v3/CF Brush One.ttf',.04,1),(10,0,[('у','final'),('y','final')],R/'artifacts/editorial_titles/style1_letters_v3/CF Brush One.ttf',.1,1),(5,1,[('н','initial'),('е','final')],R/'artifacts/editorial_titles/reference_fonts/good-vibes-pro.ttf',0,1),(7,1,[('н','initial'),('е','final')],R/'fonts/CF-MarckScript.ttf',.26,1.15)]:
 path=D/manifest[str(n)][role]['font'];f=TTFont(path);base=TTFont(basepath);cmap=f.getBestCmap();bc=base.getBestCmap();order=list(f.getGlyphOrder());rules=[];records=[]
 if 'GSUB' in f:del f['GSUB']
 for table in f['cmap'].tables:
  if table.isUnicode():
   for cp in list(table.cmap):
    if 0xe000<=cp<=0xf8ff:del table.cmap[cp]
 for i,(ch,when) in enumerate(chars):
  name=cmap[ord(ch)];alternate=name+'.'+when;f['glyf'][alternate]=copy.deepcopy(f['glyf'][name]);f['hmtx'][alternate]=f['hmtx'][name];order.append(alternate);cp=0xe100+i
  for table in f['cmap'].tables:
   if table.isUnicode():table.cmap[cp]=alternate
  gs=base.getGlyphSet();pen=TTGlyphPen(gs);draw_flat(gs,bc[ord(ch)],pen,(1,0,shear,sy,0,0));g=pen.glyph();g.recalcBounds(f['glyf']);f['glyf'][name]=g;adv,_=base['hmtx'][bc[ord(ch)]];f['hmtx'][name]=(adv,g.xMin)
  ignore=f'ignore sub @Letters {name}\';' if when=='initial' else f'ignore sub {name}\' @Letters;'
  rules.append(f'lookup Context{i} {{ {ignore} sub {name}\' by {alternate}; }} Context{i};');records.append({'char':ch,'when':when,'codepoint':cp})
 f.setGlyphOrder(order);letters=' '.join(sorted({name for cp,name in f.getBestCmap().items() if chr(cp).isalpha() and cp<0xe000}))
 addOpenTypeFeaturesFromString(f,'languagesystem DFLT dflt; languagesystem cyrl dflt; languagesystem latn dflt; @Letters=['+letters+']; feature calt { '+' '.join(rules)+' } calt;')
 f.save(path);alt_records[f'{n}:{role}']=records
# Match the brush stroke pressure across the rest of each alphabet.
for n,role,shift,skip in [(3,2,18,set('ты?')),(6,0,6,set('ПWh')),(7,1,15,set()),(10,0,12,set('ПWh'))]:
 path=D/manifest[str(n)][role]['font'];f=TTFont(path);gs=f.getGlyphSet();changed={};cmap=f.getBestCmap()
 for cp,name in cmap.items():
  if cp>=0xe000 or chr(cp) in skip or not chr(cp).isalpha() or name in changed:continue
  pen=TTGlyphPen(gs)
  for dx,dy in [(-shift,0),(shift,0),(0,-3),(0,3)]:draw_flat(gs,name,pen,(1,0,0,1,dx,dy))
  changed[name]=pen.glyph()
 for name,g in changed.items():
  f['glyf'][name]=g;g.recalcBounds(f['glyf']);adv,_=f['hmtx'][name];f['hmtx'][name]=(adv+shift,g.xMin)
 f.save(path)
(O/'character_alternates.json').write_text(json.dumps(alt_records,ensure_ascii=False,indent=2))
print('Architectural alphabet harmonized; contextual swashes isolated; brush stroke weights aligned.')

# Match high-contrast serif stem weight on the rest of the alphabet, retaining hairlines.
for n in [5,10]:
 path=D/manifest[str(n)][2]['font'];f=TTFont(path);gs=f.getGlyphSet();cmap=f.getBestCmap();changed={}
 for cp,name in cmap.items():
  if cp>=0xe000 or not chr(cp).isalpha() or chr(cp) in 'ТЫTYOU' or name in changed:continue
  pen=TTGlyphPen(gs)
  for dx in [-22,22]:draw_flat(gs,name,pen,(1,0,0,1,dx,0))
  changed[name]=pen.glyph()
 for name,g in changed.items():
  f['glyf'][name]=g;g.recalcBounds(f['glyf']);a,_=f['hmtx'][name];f['hmtx'][name]=(a+10,g.xMin)
 f.save(path)
