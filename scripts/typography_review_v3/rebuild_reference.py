from pathlib import Path
import sys,json,statistics,shutil,html
from PIL import Image,ImageDraw,ImageFont,ImageChops,ImageFilter
sys.path.insert(0,'/tmp/advice-font-tools')
from fontTools.ttLib import TTFont
from fontTools.pens.ttGlyphPen import TTGlyphPen
R=Path('/opt/content_fabric');B=R/'artifacts/editorial_titles';O=B/'review_v3';FD=O/'fonts';FD.mkdir(exist_ok=True)
# Reference boxes in the original 1330 x 1182 image; independent RU/EN geometry.
BOX={
1:[[(45,530,650,815),(95,750,197,825),(195,710,560,1018)],[(775,510,1270,815),(760,745,875,825),(875,710,1310,1018)]],
2:[[(20,170,645,485),(240,585,420,685),(115,695,560,1065)],[(735,160,1300,490),(900,575,1125,685),(745,695,1285,1065)]],
3:[[(80,240,615,466),(190,600,370,705),(50,650,650,995)],[(830,240,1210,470),(875,595,1090,715),(750,645,1320,1040)]],
5:[[(45,555,640,680),(90,670,510,785),(45,760,590,990)],[(795,555,1230,682),(725,630,1165,775),(695,765,1320,995)]],
6:[[(50,260,650,575),(45,520,160,590),(135,570,625,970)],[(720,270,1245,580),(720,510,865,590),(735,570,1310,970)]],
7:[[(65,175,615,520),(155,580,510,775),(55,765,620,1095)],[(770,175,1265,520),(820,545,1185,780),(705,770,1300,1100)]],
9:[[(130,245,600,530),(95,595,245,685),(60,635,650,1030)],[(850,245,1215,530),(745,585,935,685),(700,630,1325,1050)]],
10:[[(40,260,650,620),(265,615,390,680),(110,685,610,980)],[(740,265,1310,620),(920,605,1075,680),(710,680,1305,985)]]}
KINDS={1:['coral','white','white'],2:['gold','white','white'],3:['white','white','pink'],5:['cream','coral','cream'],6:['mint','white','white'],7:['cream','cream','gold'],9:['white','white','blue'],10:['coral','cream','cream']}
TEXT={1:[['Почему','не','ТЫ?'],['Why','not','YOU?']],2:[['Почему','не','ТЫ?'],['Why','not','YOU?']],3:[['ПОЧЕМУ','не','ты?'],['WHY','not','you?']],5:[['ПОЧЕМУ','не','ТЫ?'],['WHY','not','YOU?']],6:[['Почему','не','ТЫ?'],['Why','not','YOU?']],7:[['ПОЧЕМУ','не','ТЫ?'],['WHY','not','YOU?']],9:[['ПОЧЕМУ','не','Ты?'],['WHY','not','you?']],10:[['Почему','не','ТЫ?'],['Why','not','YOU?']]}
BASES={1:['MarckScript','Manrope','Oswald'],2:['MarckScript','Manrope','Oswald'],3:['Oswald','CormorantItalic','MarckScript'],5:['CormorantItalic','MarckScript','Cormorant'],6:['MarckScript','Manrope','Oswald'],7:['Oswald','MarckScript','Oswald'],9:['Oswald','CormorantItalic','MarckScript'],10:['MarckScript','Manrope','Cormorant']}
# Exact reference contour tracing. Word aliases are explicitly recorded as lettering, not a full alphabet.
from review_utils import contours_from_mask
def select(c,kind):
 r,g,b=c
 if kind=='coral':return r>170 and r>g*1.5 and b<130
 if kind=='gold':return r>190 and g>135 and b<140
 if kind=='pink':return r>175 and r>g*1.5 and b>45 and b<170
 if kind=='mint':return g>185 and r>135 and b>135 and g-r>10 and g-b>5
 if kind=='blue':return b>160 and g>110 and r<150 and b>r*1.5
 if kind=='cream':return r>190 and g>175 and b>115 and r>=g*.96 and g>=b*.95
 return min(r,g,b)>190 and max(r,g,b)-min(r,g,b)<65 and r>=g*.99
records={};audits=[]
for n in BOX:
 src=Image.open(B/f'eight_real_drafts/original_{n}.png').convert('RGB');d=O/f'style_{n}';d.mkdir(exist_ok=True)
 records[str(n)]={}
 for role,base in enumerate(BASES[n]):
  family=f'CF Reference {n} Part {role+1}';font=TTFont(R/f'fonts/CF-{base}.ttf');upem=font['head'].unitsPerEm;order=list(font.getGlyphOrder())
  for li,lang in enumerate(['ru','en']):
   box=BOX[n][li][role];crop=src.crop(box);kind=KINDS[n][role];mask=Image.new('L',crop.size)
   data=[255 if select(c,kind) else 0 for c in crop.getdata()];mask.putdata(data)
   # Part 3 of style 3 contains the two original pink strokes, assigned to its own word layer.
   bb=mask.getbbox();assert bb,(n,lang,role);mask=mask.crop(bb)
   xy=[box[0]+bb[0]-(670 if lang=='en' else 0),box[1]+bb[1],mask.width,mask.height]
   pixels=[c for c,yes in zip(crop.getdata(),data) if yes];color=tuple(int(statistics.median(c[i] for c in pixels)) for i in range(3))
   if n==1 and role==0:color=(255,154,104)
   mask.save(d/f'{lang}_part{role+1}_mask.png')
   pen=TTGlyphPen(None);scale=upem/mask.height
   for contour in contours_from_mask(mask):
    pts=[(round(x*scale),round((mask.height-y)*scale)) for x,y in contour];pen.moveTo(pts[0]);[pen.lineTo(p) for p in pts[1:]];pen.closePath()
   name=f'reference.{lang}';font['glyf'][name]=pen.glyph();font['hmtx'][name]=(round(mask.width*scale),0);order.append(name)
   for t in font['cmap'].tables:
    if t.isUnicode():t.cmap[0xe000+li]=name
   record={'text':TEXT[n][li][role],'box':xy,'color':color,'font':family+'.ttf','codepoint':0xe000+li,'kind':'reference_word_outlines','head_occluded_in_source':role==0 and n in (3,7,9)}
   records[str(n)].setdefault(lang,[]).append(record)
  font.setGlyphOrder(order)
  for rec in font['name'].names:
   vals={1:family,2:'Regular',3:family+' 1.0',4:family,6:family.replace(' ','')+'-Regular',16:family,17:'Regular'}
   if rec.nameID in vals:rec.string=vals[rec.nameID].encode(rec.getEncoding(),errors='replace')
  font.save(FD/(family+'.ttf'));shutil.copy(R/f'fonts/CF-{base}-OFL.txt',FD/(family+'-OFL.txt'))
records['6']['ru'][1]['box']=[54,534,98,50]
records['6']['en'][1]['box']=[58,518,128,63]
( O/'reference_geometry.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
print('48 reference words vectorized with measured boxes and colors',flush=True)
