from PIL import Image, ImageDraw, ImageChops, ImageFilter, ImageOps
W,H=720,1280
def contours_from_mask(mask):
 pix=mask.load();w,h=mask.size;edges={}
 def edge(a,b):edges.setdefault(a,[]).append(b)
 for y in range(h):
  for x in range(w):
   if not pix[x,y]:continue
   if y==0 or not pix[x,y-1]:edge((x,y),(x+1,y))
   if x==w-1 or not pix[x+1,y]:edge((x+1,y),(x+1,y+1))
   if y==h-1 or not pix[x,y+1]:edge((x+1,y+1),(x,y+1))
   if x==0 or not pix[x-1,y]:edge((x,y+1),(x,y))
 result=[]
 while edges:
  start=next(iter(edges));point=start;loop=[]
  while True:
   loop.append(point);nxt=edges[point].pop()
   if not edges[point]:del edges[point]
   point=nxt
   if point==start:break
  simple=[]
  for i,point in enumerate(loop):
   prev=loop[i-1];nxt=loop[(i+1)%len(loop)]
   if (point[0]-prev[0])*(nxt[1]-point[1])!=(point[1]-prev[1])*(nxt[0]-point[0]):simple.append(point)
  if len(simple)>=3:result.append(simple)
 return result

HEAD0=[(291,383),(306,368),(328,370),(343,375),(362,389),(380,390),(399,378),(410,379),(420,396),(421,411),(438,417),(449,428),(457,449),(453,470),(467,479),(456,488),(447,493),(443,511),(430,526),(418,530),(411,553),(412,579),(415,599),(400,620),(381,633),(356,637),(333,626),(312,608),(297,588),(283,561),(267,561),(253,553),(249,540),(238,534),(234,521),(239,506),(233,496),(224,486),(232,477),(227,463),(235,449),(245,438),(241,427),(241,414),(254,403),(270,403),(276,389)]
MASK_CACHE={}
def headmask(frame=0,bg=None):
 if bg is not None and frame in MASK_CACHE:return MASK_CACHE[frame]
 # Head camera scale and vertical drift measured from first and last frame. The upper text is behind hair only.
 t=frame/71;scale=1+.13*t;cx,cy=345,500
 pts=[(cx+(x-cx)*scale-7*t,cy+(y-cy)*scale-32*t) for x,y in HEAD0]
 mask=Image.new('L',(W,H));ImageDraw.Draw(mask).polygon(pts,fill=255)
 if bg is not None:
  r,g,b=bg.convert('RGB').split()
  warm=ImageChops.multiply(ImageChops.subtract(r,g).point(lambda x:255 if x>5 else 0),ImageChops.subtract(r,b).point(lambda x:255 if x>10 else 0))
  dark=bg.convert('L').point(lambda x:255 if x<48 else 0)
  silhouette=ImageChops.lighter(warm,dark).filter(ImageFilter.MaxFilter(3))
  mask=ImageChops.multiply(mask,silhouette)
  # Keep enclosed highlights/eyes inside the head, but exclude connected blue wall outside.
  box=mask.getbbox();cut=mask.crop(box);small=cut.resize((cut.width//2,cut.height//2),Image.Resampling.NEAREST)
  filled=ImageOps.expand(small,border=2,fill=0);ImageDraw.floodfill(filled,(0,0),128,thresh=0)
  filled=filled.point(lambda x:0 if x==128 else 255).crop((2,2,filled.width-2,filled.height-2)).resize(cut.size,Image.Resampling.BILINEAR)
  mask=Image.new('L',(W,H));mask.paste(filled,box[:2])
 result=mask.filter(ImageFilter.GaussianBlur(.65))
 if bg is not None:MASK_CACHE[frame]=result
 return result
