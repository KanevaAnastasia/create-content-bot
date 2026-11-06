"""Typography review artifact builder; never changes the running bot."""
from pathlib import Path
import sys,json,subprocess,html
from PIL import Image,ImageDraw,ImageFont,ImageFilter
sys.path.insert(0,'/opt/content_fabric')
exec((Path(__file__).parent/'render_unicode_review.py').read_text())
IDS=[1,2,3,5,6,7,9,10]
ROWS={'ru':[['Почему','не','ты?'],['У меня','нет','энергии'],['Я','','ленивый'],['У меня','нет','времени'],['Начну','с','понедельника']], 'en':[['Why','not','you?'],['I have','no','energy'],["I'm",'','lazy'],['I have','no','time'],["I'll start",'on','Monday']]}
def phrase_layer(n,lang,i,text):
 l=Image.new('RGBA',(W,H))
 if not text:return l
 r=rec[str(n)][lang][i]
 original=r['text']
 if original.isupper():text=text.upper()
 elif original[0].islower():text=text.lower()
 text=shape_text(text,n,i);f=ImageFont.truetype(str(FD/manifest[str(n)][i]['font']),600);bb=f.getbbox(text)
 a=Image.new('RGBA',(bb[2]-bb[0]+24,bb[3]-bb[1]+24));ImageDraw.Draw(a).text((12-bb[0],12-bb[1]),text,font=f,fill=tuple(r['color']));a=a.crop(a.getbbox())
 x,y,mw,mh=r['box'];x=round(x*W/660);y=round(y*H/1182);mw=round(mw*W/660);mh=round(mh*H/1182)
 if n==1:
  x,y,mw,mh=[(40,560,640,240),(40,825,140,65),(40,930,640,230)][i]
 # Long semantic chunks may use the full safe width. Keep proportions, with limited display compression.
 if i==2 and len(text)>5:x,mw=40,640
 if i==0 and len(text)>5:x,mw=40,640
 if i==1 and len(text)>len(original):x=max(35,x-20);mw=min(220,W-x-35)
 if i==0 and n in (3,7,9) and len(text)<=3:x,mw=50,180
 aspect_floor=.72 if i==2 else .9
 original_bb=f.getbbox(shape_text(original,n,i))
 preferred=mh/max(1,original_bb[3]-original_bb[1])
 if i==1:preferred=max(preferred,mh*.65/a.height)
 scale=min(preferred,mw/a.width/aspect_floor)
 a=a.resize((min(mw,max(1,round(a.width*scale))),max(1,round(a.height*scale))),Image.Resampling.LANCZOS)
 if n==1 and i==0:
  a=a.rotate(9,Image.Resampling.BICUBIC,expand=True)
  outline=Image.new('RGBA',a.size,'#36251f');outline.putalpha(a.getchannel('A').filter(ImageFilter.MaxFilter(3)));outline.alpha_composite(a);a=outline
  if a.width>mw:a=a.resize((mw,round(a.height*mw/a.width)),Image.Resampling.LANCZOS)
 l.alpha_composite(a,(x+(mw-a.width)//2,y+(mh-a.height)//2));return l

def compose(bg,layers,n,k=0,count=3):
 im=bg.copy()
 for i,l in enumerate(layers[:count]):
  im.alpha_composite(l)
  if i==0 and n in (3,7,9):im.paste(bg,(0,0),headmask(k,bg))
 return im
for n in IDS:
 for lang in ['ru','en']:
  strip=Image.new('RGB',(1800,710),'#eee6d7');d=ImageDraw.Draw(strip)
  for row,words in enumerate(ROWS[lang]):
   layers=ALL[n,lang] if row==0 else [phrase_layer(n,lang,i,t) for i,t in enumerate(words)]
   frame=compose(BG,layers,n).convert('RGB');frame.save(OUT/f'style_{n}/{lang}_phrase_{row+1}.jpg',quality=94)
   d.text((row*360+12,15),' '.join(t for t in words if t),font=ImageFont.truetype(str(R/'fonts/CF-Manrope.ttf'),17),fill='#29231e');strip.paste(frame.resize((360,640)),(row*360,60))
  strip.save(OUT/f'style_{n}/{lang}_phrases.jpg',quality=94)
print('80 phrase cards rendered',flush=True)
if '--video' in sys.argv:
 from app.ai_approval_service import _resolve_ffmpeg
 frames=[]
 for k in range(72):
  bg=Image.open(B/f'eight_styles_v2/video_frames/{k+1:03d}.png').convert('RGBA');bg=Image.alpha_composite(bg,Image.new('RGBA',(W,H),(0,0,0,82)));frames.append(bg);headmask(k,bg)
 for n in IDS:
  for lang in ['ru','en']:
   sd=OUT/f'style_{n}';timing=json.loads((B/f'timed_title/{lang}.json').read_text());starts=[w[1] for w in timing['words']]
   cmd=[_resolve_ffmpeg(),'-y','-f','rawvideo','-pix_fmt','rgb24','-s','720x1280','-r','24','-i','-','-i',str(B/f'timed_title/{lang}.mp3'),'-filter_complex','[1:a]apad=whole_dur=3[a]','-map','0:v','-map','[a]','-t','3','-c:v','libx264','-preset','fast','-crf','19','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart',str(sd/f'{lang}_preview.mp4')]
   with open(sd/f'{lang}_encode.log','wb') as log:
    proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=log,stderr=log)
    for k,bg in enumerate(frames):
     frame=compose(bg,ALL[n,lang],n,k,sum(k/24>=s for s in starts)).convert('RGB');proc.stdin.write(frame.tobytes())
     if k in [0,12,24,71]:frame.save(sd/f'qa_{lang}_{k}.jpg',quality=93)
    proc.stdin.close();assert proc.wait()==0
   print(f'Video {n} {lang}',flush=True)
page=['''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Типографика — ревью 4</title><style>body{margin:0;background:#eee6d7;color:#29231e;font:17px system-ui}main{max-width:1440px;margin:auto;padding:24px}nav{position:sticky;top:0;background:#eee6d7ee;padding:15px;z-index:3;display:flex;gap:20px}a{color:inherit}img{width:100%;height:auto}section{padding:24px 0;border-bottom:1px solid #aaa;scroll-margin-top:70px}summary{cursor:pointer;padding:15px}video{width:min(360px,44vw)}.videos{display:flex;gap:20px}p{line-height:1.5;max-width:1050px}</style><main><h1>Ревью 4 · интервалы и контраст штрихов</h1><p>№5: плотнее набор и толще буквы. №9: восстановлен контраст основных штрихов и тонких перекладин. Исправлены контуры нижних букв №6/10 и цельный росчерк №9.</p><details open><summary>№5 · эскиз / было / стало</summary><img src="style_5/change_comparison.jpg"></details><details open><summary>№9 · эскиз / было / стало</summary><img src="style_9/change_comparison.jpg"></details><p>Исходный эскиз слева, новый набор справа. Цвета и размеры «Почему не ты / Why not you» взяты отдельно из каждой языковой версии. №1 — сохранённый светлый вариант с обводкой. Затемнение 32%. Это материалы для визуального согласования, а не заявление о точном совпадении всех букв.</p><nav>''']
page += [f'<a href="#s{n}">№{n}</a>' for n in IDS];page+=['</nav>']
for n in IDS:
 page += [f'<section id="s{n}"><h2>№{n}</h2><img loading="lazy" src="style_{n}/ru_compare.jpg"><details><summary>Сравнение английской версии</summary><img loading="lazy" src="style_{n}/en_compare.jpg"></details><div class="videos"><video controls preload="metadata" poster="style_{n}/ru_new.jpg" src="style_{n}/ru_preview.mp4"></video><video controls preload="metadata" poster="style_{n}/en_new.jpg" src="style_{n}/en_preview.mp4"></video></div><h3>Проверка на пяти фразах</h3><img loading="lazy" src="style_{n}/ru_phrases.jpg"><details><summary>Пять фраз на английском</summary><img loading="lazy" src="style_{n}/en_phrases.jpg"></details><details><summary>Алфавиты и проверочная фраза</summary><img loading="lazy" src="style_{n}/alphabet.jpg"></details></section>']
page += ['<p>В наборе — адаптированные шрифтовые основы и перерисованные отдельные буквы. Полное совпадение новых фраз с нарисованным эскизом ещё требует согласования. В №1 эталонная надпись сохранена отдельными контурами; новые фразы используют алфавит. Маска персонажа настроена для этого фрагмента, не для произвольного видео. Голос: P1bg08DkjqiVEzOn76yG; использована уже записанная озвучка.</p></main></html>']
(OUT/'index.html').write_text('\n'.join(page))

subprocess.run([sys.executable,str(Path(__file__).with_name("compare_revisions.py"))],check=True)
