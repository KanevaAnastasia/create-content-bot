from pathlib import Path
import sys,json,shutil
from PIL import Image,ImageDraw
sys.path.insert(0,'/tmp/advice-font-tools')
from fontTools.ttLib import TTFont
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
R=Path('/opt/content_fabric');B=R/'artifacts/editorial_titles';O=B/'review_v4';D=O/'unicode_fonts';D.mkdir(parents=True,exist_ok=True)
from review_utils import contours_from_mask
F=R/'fonts';RF=B/'reference_fonts';catalog={}
def derive(n,role,path,sx=1,sy=1,shear=0):
 f=TTFont(path);gs=f.getGlyphSet();new={}
 if 'glyf' not in f:return f
 for name in f.getGlyphOrder():
  p=TTGlyphPen(None);record=DecomposingRecordingPen(gs);gs[name].draw(record);record.replay(TransformPen(p,(sx,0,shear,sy,0,0)));new[name]=p.glyph()
 for name,g in new.items():
  f['glyf'][name]=g;g.recalcBounds(f['glyf']);a,_=f['hmtx'][name];f['hmtx'][name]=(round(a*sx),getattr(g,'xMin',0))
 return f
spec={
1:[(B/'style1_letters_v3/CF Brush One.ttf',1,1,0),(F/'CF-Manrope.ttf',1,1,0),(F/'CF-Oswald.ttf',.7,1,0)],
2:[(RF/'good-vibes-pro.ttf',1,1,0),(F/'CF-Manrope.ttf',1,1,.20),(F/'CF-Oswald.ttf',.57,1,0)],
3:[(RF/'BebasNeue-BebasNeueBold.ttf',1,1,0),(F/'CF-CormorantItalic.ttf',1,1,0),(F/'CF-MarckScript.ttf',1,1.2,.2)],
5:[(F/'CF-CormorantItalic.ttf',1,1,0),(RF/'good-vibes-pro.ttf',1,1,0),(RF/'Prata.ttf',1,1,0)],
6:[(B/'style1_letters_v3/CF Brush One.ttf',1,1,.04),(F/'CF-Manrope.ttf',1,1,0),(F/'CF-Montserrat.ttf',.35,1,.08)],
7:[(O/'CFArchitecturalReference.ttf',1,1,0),(F/'CF-MarckScript.ttf',1,1.15,.26),(F/'CF-Montserrat.ttf',.55,1,0)],
9:[(RF/'BebasNeue-BebasNeueRegular.ttf',.75,1,0),(F/'CF-CormorantItalic.ttf',1,1,0),(RF/'good-vibes-pro.ttf',1,1,0)],
10:[(B/'style1_letters_v3/CF Brush One.ttf',1,1,.1),(F/'CF-Manrope.ttf',1,1,.07),(RF/'Prata.ttf',.86,1,.12)]}
fonts={(n,i):derive(n,i,*s) for n,roles in spec.items() for i,s in enumerate(roles)}
def set_mask(f,char,mask,origin,baseline,scale,advance):
 cmap=f.getBestCmap();name=cmap[ord(char)];p=TTGlyphPen(None)
 for contour in contours_from_mask(mask):
  pts=[(round((x-origin)*scale),round((baseline-y)*scale)) for x,y in contour]
  p.moveTo(pts[0]);[p.lineTo(x) for x in pts[1:]];p.closePath()
 g=p.glyph();g.recalcBounds(f['glyf']);f['glyf'][name]=g;f['hmtx'][name]=(round(advance*scale),g.xMin)
def segment(n,lang,role,poly):
 im=Image.open(O/f'style_{n}/{lang}_part{role+1}_mask.png'); gate=Image.new('L',im.size);ImageDraw.Draw(gate).polygon(poly,fill=255)
 from PIL import ImageChops
 return ImageChops.multiply(im,gate)
# Exact individual display letters replace the generic outlines. Other letters retain a calibrated full font.
bodycuts={2:{'ru':{'Т':(0,112,128),'Ы':(128,300,189),'?':(317,424,120)},'en':{'Y':(0,125,127),'O':(127,241,129),'U':(256,371,128)}},5:{'ru':{'Т':(0,170,172),'Ы':(172,393,224),'?':(396,508,120)}},7:{'ru':{'Т':(0,143,155),'Ы':(155,398,251),'?':(406,536,140)},'en':{'U':(305,436,138)}},10:{'ru':{'?':(348,460,125)}}}
for n,langs in bodycuts.items():
 f=fonts[n,2];cmap=f.getBestCmap();cap=f['glyf'][cmap[ord('T')]].yMax
 for lang,letters in langs.items():
  im=Image.open(O/f'style_{n}/{lang}_part3_mask.png');scale=cap/im.height
  for ch,(left,right,advance) in letters.items():
   cut=Image.new('L',im.size);cut.paste(im.crop((left,0,right,im.height)),(left,0));set_mask(f,ch,cut,left,im.height,scale,advance)
   if ch=='Т':f['glyf'][cmap[ord('T')]]=f['glyf'][cmap[ord(ch)]];f['hmtx'][cmap[ord('T')]]=f['hmtx'][cmap[ord(ch)]]
# Smooth/rough brush families: actual reference capital outlines, with source baselines and connected spacing.
rec=json.loads((O/'reference_geometry.json').read_text())
for n in [6,10]:
 f=fonts[n,0];box=rec[str(n)]['ru'][0]['box'];x0,y0,w,h=box
 srcpoly=[(50,260),(262,260),(248,305),(217,354),(207,514),(170,565),(50,565)] if n==6 else [(38,258),(270,258),(262,318),(216,395),(190,555),(55,575)]
 poly=[(x-x0,y-y0) for x,y in srcpoly];mask=segment(n,'ru',0,poly)
 baseline=450 if n==6 else 492;origin=65 if n==6 else 55
 set_mask(f,'П',mask,origin-x0,baseline-y0,4,145 if n==6 else 160)
 box=rec[str(n)]['en'][0]['box'];x0,y0,w,h=box;x0+=670
 polyabs=[(720,270),(950,270),(950,310),(912,355),(923,405),(958,454),(941,490),(870,535),(730,535)] if n==6 else [(740,265),(942,265),(959,330),(948,380),(988,425),(974,470),(934,534),(870,557),(740,557)]
 mask=segment(n,'en',0,[(x-x0,y-y0) for x,y in polyabs]);set_mask(f,'W',mask,(735 if n==6 else 750)-x0,(450 if n==6 else 490)-y0,4,190 if n==6 else 215)
# Preserve the reference's terminal у and its sweeping exit stroke as one Unicode glyph.
for n,poly,origin,baseline in [(6,[(519,310),(650,310),(650,435),(568,510),(480,570),(375,580),(375,540),(485,520),(547,460),(552,430),(375,505),(375,460),(470,427),(519,405)],521,450),(10,[(545,337),(650,337),(650,470),(559,535),(503,606),(389,625),(389,588),(484,574),(539,501),(550,464),(395,580),(395,540),(480,488),(545,440)],550,492)]:
 x0,y0,_,_=rec[str(n)]['ru'][0]['box'];mask=segment(n,'ru',0,[(x-x0,y-y0) for x,y in poly]);set_mask(fonts[n,0],'у',mask,origin-x0,baseline-y0,4,125)
# Reconstruct the looped Latin h and terminal y rather than substituting a straight-ascender script.
for n,ch,poly,origin,advance,baseline in [
 (6,'h',[(905,278),(1068,278),(1068,442),(940,450),(918,418)],925,145,450),
 (6,'y',[(1050,340),(1240,340),(1240,455),(1148,465),(1148,520),(1090,575),(1025,582),(1025,545),(1070,500),(1080,450),(1050,435)],1070,160,450),
 (10,'h',[(940,270),(1145,270),(1122,350),(1108,434),(1082,490),(960,510),(945,460)],965,140,492),
 (10,'y',[(1090,340),(1315,340),(1315,470),(1225,505),(1180,585),(1100,625),(1055,625),(1055,588),(1140,550),(1160,490),(1090,480)],1105,180,492)]:
 x0,y0,_,_=rec[str(n)]['en'][0]['box'];x0+=670;mask=segment(n,'en',0,[(x-x0,y-y0) for x,y in poly]);set_mask(fonts[n,0],ch,mask,origin-x0,baseline-y0,4,advance)
# Architectural capitals that are unobscured in the reference: preserve their exact construction.
f=fonts[7,0];im=Image.open(O/'style_7/ru_part1_mask.png');cap=810;scale=cap/im.height
for ch,left,right,advance in [('П',0,89,93),('М',342,448,106),('У',448,539,100)]:
 cut=Image.new('L',im.size);cut.paste(im.crop((left,0,right,im.height)),(left,0));set_mask(f,ch,cut,left,im.height,scale,advance)
im=Image.open(O/'style_7/en_part1_mask.png');scale=810/im.height
# The rightmost Y is completely visible; no head-shaped hole is baked into it.
cut=Image.new('L',im.size);left=328;cut.paste(im.crop((left,0,im.width,im.height)),(left,0));set_mask(f,'Y',cut,left,im.height,scale,im.width-left+8)
# Blue calligraphy: a restrained source capital T, not the unrelated ornamental loop.
f=fonts[9,2]
for ch,poly,origin,advance in [('Т',[(60,675),(400,675),(390,723),(318,735),(245,825),(180,904),(120,910),(162,825),(178,780),(60,825)],120,160),('?',[(533,635),(650,635),(650,865),(533,865)],535,120)]:
 x0,y0,_,_=rec['9']['ru'][2]['box'];mask=segment(9,'ru',2,[(x-x0,y-y0) for x,y in poly]);set_mask(f,ch,mask,origin-x0,895-y0,2.3,advance)
# Slanted high-contrast serif T and Ы of style 10.
f=fonts[10,2];cap=f['glyf'][f.getBestCmap()[ord('T')]].yMax
for ch,poly,origin,advance in [('Т',[(115,685),(345,685),(327,733),(251,733),(222,950),(244,972),(130,972),(157,948),(187,733),(115,733)],121,150),('Ы',[(276,685),(466,685),(466,974),(222,974),(239,949),(268,735)],270,195)]:
 x0,y0,_,_=rec['10']['ru'][2]['box'];mask=segment(10,'ru',2,[(x-x0,y-y0) for x,y in poly]);set_mask(f,ch,mask,origin-x0,968-y0,cap/274,advance)
# Pink brush: custom т/ы/? preserve the low entry stroke and the round question mark.
f=fonts[3,2];im=Image.open(O/'style_3/ru_part3_mask.png');x0,y0,w,h=rec['3']['ru'][2]['box']
for ch,polyabs,origin,advance in [
 ('т',[(50,760),(332,760),(332,914),(210,990),(50,990)],60,270),
 ('ы',[(332,720),(520,720),(520,905),(332,905)],335,180),
 ('?',[(515,650),(650,650),(650,900),(515,900)],515,140)]:
 mask=segment(3,'ru',2,[(x-x0,y-y0) for x,y in polyabs]);set_mask(f,ch,mask,origin-x0,890-y0,2.7,advance)
# Reconstruct the actual Unicode letters of the small connective words, not a word-picture alias.
smallcuts={2:{'ru':{'н':(0,79,82),'е':(82,156,80)},'en':{'n':(0,65,70),'o':(70,139,73),'t':(143,190,52)}},3:{'ru':{'н':(0,83,84),'е':(84,143,65)},'en':{'n':(0,78,82),'o':(82,144,66),'t':(148,196,53)}},9:{'ru':{'н':(0,67,68),'е':(68,118,55)},'en':{'n':(0,65,67),'o':(67,117,56),'t':(123,163,45)}},10:{'ru':{'н':(0,49,55),'е':(55,104,54)},'en':{'n':(0,43,50),'o':(50,97,51),'t':(101,132,36)}}}
for n,langs in smallcuts.items():
 f=fonts[n,1];cmap=f.getBestCmap();xh=f['glyf'][cmap[ord('н')]].yMax
 for lang,letters in langs.items():
  im=Image.open(O/f'style_{n}/{lang}_part2_mask.png');first=next(iter(letters.values()));nb=im.crop((first[0],0,first[1],im.height)).getbbox();scale=xh/(nb[3]-nb[1])
  for ch,(left,right,advance) in letters.items():
   cut=Image.new('L',im.size);cut.paste(im.crop((left,0,right,im.height)),(left,0));set_mask(f,ch,cut,left,im.height-1,scale,advance)
# Reference signature/brush letters include their entry and exit strokes.
for n,defs in {5:[('ru','н',[(95,670),(315,670),(309,726),(278,778),(95,778)],205,95,750,4.5),('ru','е',[(290,670),(510,670),(510,760),(290,760)],300,185,750,4.5)],7:[('ru','н',[(150,580),(340,580),(321,645),(310,701),(260,748),(195,770),(190,680)],205,120,700,4.6),('ru','е',[(320,580),(460,580),(460,710),(310,710)],325,125,700,4.6)]}.items():
 f=fonts[n,1]
 for lang,ch,poly,origin,advance,baseline,scale in defs:
  x0,y0,_,_=rec[str(n)][lang][1]['box'];x0+=670 if lang=='en' else 0
  mask=segment(n,lang,1,[(x-x0,y-y0) for x,y in poly]);set_mask(f,ch,mask,origin-x0,baseline-y0,scale,advance)
# Connected-component extraction keeps touching bounding boxes from cutting adjacent serif glyphs.
def components(im):
 pix=im.load();seen=set();out=[]
 for y in range(im.height):
  for x in range(im.width):
   if not pix[x,y] or (x,y) in seen:continue
   todo=[(x,y)];seen.add((x,y));pts=[]
   while todo:
    a,b=todo.pop();pts.append((a,b))
    for c,d in [(a-1,b),(a+1,b),(a,b-1),(a,b+1)]:
     if 0<=c<im.width and 0<=d<im.height and pix[c,d] and (c,d) not in seen:seen.add((c,d));todo.append((c,d))
   if len(pts)>15:out.append(pts)
 return out
for n in [5,6,7,10]:
 f=fonts[n,2];im=Image.open(O/f'style_{n}/en_part3_mask.png');parts=components(im)
 large=sorted(sorted(parts,key=len,reverse=True)[:4],key=lambda pts:min(x for x,y in pts))
 if len(large)!=4:raise ValueError(('letter components',n))
 cap=f['glyf'][f.getBestCmap()[ord('T')]].yMax
 for idx,(ch,pts) in enumerate(zip('YOU?',large)):
  if ch=='?':
   cx=sum(x for x,y in pts)/len(pts)
   for extra in parts:
    if extra not in large and abs(sum(x for x,y in extra)/len(extra)-cx)<im.width*.13:pts=pts+extra
  mask=Image.new('L',im.size);mp=mask.load()
  for x,y in pts:mp[x,y]=255
  bb=mask.getbbox();set_mask(f,ch,mask,bb[0],im.height,cap/im.height,bb[2]-bb[0]+10)

# Reconstruct Cyrillic serif/display bodies from connected components, not rectangular cuts.
# Rectangular cuts had included neighbouring serifs and damaged the T in style 10.
for n in [6,10]:
 f=fonts[n,2];im=Image.open(O/f'style_{n}/ru_part3_mask.png');parts=components(im)
 major=sorted(sorted(parts,key=len,reverse=True)[:4],key=lambda pts:min(x for x,y in pts))
 cap=f['glyf'][f.getBestCmap()[ord('T')]].yMax
 for ch,pts in [('Т',major[0]),('Ы',major[1]+major[2])]:
  mask=Image.new('L',im.size);mp=mask.load()
  for x,y in pts:mp[x,y]=255
  bb=mask.getbbox();set_mask(f,ch,mask,bb[0],bb[3],cap/(bb[3]-bb[1]),bb[2]-bb[0]+10)
# Style 9: retain the full question-mark curve and its separate dot.
f=fonts[9,2];im=Image.open(O/'style_9/ru_part3_mask.png');parts=components(im)
pts=[pt for component in parts if min(x for x,y in component)>460 for pt in component]
mask=Image.new('L',im.size);mp=mask.load()
for x,y in pts:mp[x,y]=255
set_mask(f,'?',mask,479,250,2.3,110)
# Source ы has a restrained sloping bowl, unlike the ornate substituted base glyph.
x0,y0,_,_=rec['9']['ru'][2]['box']
mask=segment(9,'ru',2,[(x-x0,y-y0) for x,y in [(306,743),(539,743),(539,900),(241,900),(241,860),(290,800)]])
set_mask(f,'ы',mask,285-x0,895-y0,2.3,240)

# Keep the small connective words optically light; references govern fixed headline sizes.
for (n,role),f in fonts.items():
 family=f'CF Reviewed {n} {role+1}'
 for recname in f['name'].names:
  vals={1:family,2:'Regular',3:family+' 1.0',4:family,6:family.replace(' ','')+'-Regular',16:family,17:'Regular'}
  if recname.nameID in vals:recname.string=vals[recname.nameID].encode(recname.getEncoding(),errors='replace')
 if 'glyf' in f:
  f['hhea'].ascent=max(f['hhea'].ascent,1400);f['hhea'].descent=min(f['hhea'].descent,-700);f['OS/2'].usWinAscent=max(f['OS/2'].usWinAscent,1400);f['OS/2'].usWinDescent=max(f['OS/2'].usWinDescent,700)
 path=D/(family+'.ttf');f.save(path)
 alphabet='АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюяABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789?!'
 missing=[c for c in alphabet if ord(c) not in f.getBestCmap()];assert not missing,(family,missing)
 catalog.setdefault(str(n),[]).append({'role':role,'font':path.name,'source':str(spec[n][role][0]),'coverage_missing':missing,'status':'visual_review_required'})
(O/'unicode_font_manifest.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2));print('Built 24 Unicode fonts, full RU/EN coverage; no word aliases used for review renderer.')
