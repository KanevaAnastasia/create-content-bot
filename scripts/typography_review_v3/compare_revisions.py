"""Same-crop reference/before/after sheets for the current typography review."""
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
R=Path('/opt/content_fabric');B=R/'artifacts/editorial_titles';O=B/'review_v4'
font=ImageFont.truetype(str(R/'fonts/CF-Manrope.ttf'),25)
for n,box in [(5,(0,600,720,1140)),(9,(90,240,680,610))]:
 for lang in ['ru','en']:
  x=0 if lang=='ru' else 670
  ref=Image.open(B/f'eight_real_drafts/original_{n}.png').crop((x,0,x+660,1182)).resize((720,1280))
  images=[ref,Image.open(B/f'review_v3/style_{n}/{lang}_new.jpg'),Image.open(O/f'style_{n}/{lang}_new.jpg')]
  w,h=box[2]-box[0],box[3]-box[1];sheet=Image.new('RGB',(w*3,h+65),'#eee6d7');d=ImageDraw.Draw(sheet)
  for i,(title,im) in enumerate(zip(['Эскиз','Было','Стало'],images)):
   d.text((i*w+18,18),f'№{n} · {title}',font=font,fill='#29231e');sheet.paste(im.crop(box),(i*w,65))
  sheet.save(O/f'style_{n}/change_comparison{ "" if lang=="ru" else "_en"}.jpg',quality=95)
print('Reference / before / after comparisons saved')
