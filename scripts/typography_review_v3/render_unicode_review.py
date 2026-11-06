from pathlib import Path
import json,sys,math,re
from PIL import Image,ImageDraw,ImageFont,ImageChops,ImageFilter,ImageOps
R=Path('/opt/content_fabric');B=R/'artifacts/editorial_titles';OUT=B/'review_v4';W,H=720,1280
rec=json.loads((OUT/'reference_geometry.json').read_text());manifest=json.loads((OUT/'unicode_font_manifest.json').read_text());FD=OUT/'unicode_fonts'
BG=Image.open(B/'eight_styles_v2/video_frames/001.png').convert('RGBA');BG=Image.alpha_composite(BG,Image.new('RGBA',(W,H),(0,0,0,82)))
from review_utils import headmask
label=ImageFont.truetype(str(R/'fonts/CF-Manrope.ttf'),23)
ALTERNATES=json.loads((OUT/'character_alternates.json').read_text())
def shape_text(text,n,role):
 rules=ALTERNATES.get(f'{n}:{role}',[])
 def word(m):
  chars=list(m.group(0))
  for rule in rules:
   at=0 if rule['when']=='initial' else len(chars)-1
   if chars[at]==rule['char']:chars[at]=chr(rule['codepoint'])
  return ''.join(chars)
 return re.sub(r'[^\W\d_]+',word,text)
def layer(n,lang,i,text=None):
 r=rec[str(n)][lang][i];text=shape_text(text or r['text'],n,i);f=ImageFont.truetype(str(FD/manifest[str(n)][i]['font']),600)
 bb=f.getbbox(text);a=Image.new('RGBA',(bb[2]-bb[0]+10,bb[3]-bb[1]+10));ImageDraw.Draw(a).text((5-bb[0],5-bb[1]),text,font=f,fill=tuple(r['color']));a=a.crop(a.getbbox())
 x,y,w,h=r['box']
 # Swash below the Russian blue lettering is a layout ornament, not a taller text line.
 if n==9 and lang=='ru' and i==2:h=895-y
 a=a.resize((round(w*W/660),round(h*H/1182)),Image.Resampling.LANCZOS);l=Image.new('RGBA',(W,H));l.alpha_composite(a,(round(x*W/660),round(y*H/1182)))
 return l

def ornament(n,lang,role,poly):
 r=rec[str(n)][lang][role];x,y,w,h=r['box'];mask=Image.open(OUT/f'style_{n}/{lang}_part{role+1}_mask.png');gate=Image.new('L',mask.size);ImageDraw.Draw(gate).polygon([(px-x,py-y) for px,py in poly],fill=255);mask=ImageChops.multiply(mask,gate)
 if n==9 and role==2:
  # The underline is one disconnected component: keep it whole, without nearby letter fragments.
  source=Image.open(OUT/f'style_{n}/{lang}_part{role+1}_mask.png').copy()
  seed=next((x,y) for y in range(source.height-1,-1,-1) for x in range(source.width) if source.getpixel((x,y)))
  ImageDraw.floodfill(source,seed,128,thresh=0);mask=source.point(lambda v:255 if v==128 else 0)
 a=Image.new('RGBA',mask.size,tuple(r['color']));a.putalpha(mask);a=a.resize((round(w*W/660),round(h*H/1182)),Image.Resampling.LANCZOS);l=Image.new('RGBA',(W,H));l.alpha_composite(a,(round(x*W/660),round(y*H/1182)));return l
ALL={}
for n in [1,2,3,5,6,7,9,10]:
 out=OUT/f'style_{n}';out.mkdir(exist_ok=True)
 for lang in ['ru','en']:
  if n==1:layers=[Image.open(B/f'eight_styles_v2/style_1/{lang}_word_{i}.png').convert('RGBA') for i in range(3)]
  else:layers=[layer(n,lang,i) for i in range(3)]
  if n==3 and lang=='ru':layers[1].alpha_composite(ornament(n,lang,2,[(180,690),(420,690),(420,750),(180,750)]))
  if n==7 and lang=='ru':layers[1].alpha_composite(ornament(n,lang,1,[(225,745),(310,715),(390,703),(500,699),(510,720),(380,733),(285,759),(225,775)]))
  if n==9 and lang=='ru':layers[2].alpha_composite(ornament(n,lang,2,[(70,900),(340,850),(520,850),(650,900),(650,1040),(70,1040)]))
  im=BG.copy()
  for i,l in enumerate(layers):
   im.alpha_composite(l)
   if i==0 and n in (3,7,9):im.paste(BG,(0,0),headmask(0,BG))
   l.save(out/f'{lang}_word{i}.png')
  im.convert('RGB').save(out/f'{lang}_new.jpg',quality=95);ALL[n,lang]=layers
  ref=Image.open(B/f'eight_real_drafts/original_{n}.png').crop((0 if lang=='ru' else 670,0,660 if lang=='ru' else 1330,1182)).resize((W,H))
  compare=Image.new('RGB',(W*2,H+85),'#eee6d7');d=ImageDraw.Draw(compare);d.text((20,20),f'№{n} {lang.upper()} · исходный эскиз',font=label,fill='#29231e');d.text((W+20,20),'Новые Unicode-буквы · проверка',font=label,fill='#29231e');compare.paste(ref,(0,85));compare.paste(im.convert('RGB'),(W,85));compare.save(out/f'{lang}_compare.jpg',quality=95)
 # Proof individual glyphs and the user pangram. This is a separate review from matching one headline.
 proof=Image.new('RGB',(1600,1380),'#eee6d7');d=ImageDraw.Draw(proof);d.text((25,15),f'№{n} · проверка самого алфавита',font=label,fill='#29231e')
 for i,fontrec in enumerate(manifest[str(n)]):
  y=65+i*430;d.text((25,y),f'Шрифт {i+1}',font=label,fill='#29231e')
  for j,text in enumerate(['АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ','абвгдеёжзийклмнопрстуфхцчшщъыьэюя','ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz','Съешь ещё этих мягких французских булок, да выпей же чаю']):
   f=ImageFont.truetype(str(FD/fontrec['font']),120);bb=f.getbbox(text);a=Image.new('RGBA',(bb[2]-bb[0]+8,bb[3]-bb[1]+8));ImageDraw.Draw(a).text((4-bb[0],4-bb[1]),text,font=f,fill='#29231e');a=a.crop(a.getbbox());sc=min(1530/a.width,75/a.height);a=a.resize((round(a.width*sc),round(a.height*sc)));proof.paste(a,(25,y+40+j*90),a)
 proof.save(out/'alphabet.jpg',quality=95)
contact=Image.new('RGB',(1440,1400),'#eee6d7');d=ImageDraw.Draw(contact)
for j,n in enumerate([1,2,3,5,6,7,9,10]):
 x=(j%4)*360;y=(j//4)*700;d.text((x+15,y+12),f'№{n}',font=label,fill='#29231e');contact.paste(Image.open(OUT/f'style_{n}/ru_new.jpg').resize((360,640)),(x,y+50))
contact.save(OUT/'unicode_review_all.jpg',quality=95)
print(OUT/'unicode_review_all.jpg')
