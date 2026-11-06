from pathlib import Path
import sys
sys.path.insert(0,'/tmp/advice-font-tools')
from fontTools.ttLib import TTFont
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.cu2quPen import Cu2QuPen
from PIL import ImageFont,Image,ImageDraw
root=Path('/opt/content_fabric');out=root/'artifacts/editorial_titles/review_v4';out.mkdir(parents=True,exist_ok=True)
f=TTFont(root/'fonts/CF-Oswald.ttf');cmap=f.getBestCmap();gs=f.getGlyphSet();changed={}
# Narrow, heavy uprights with flared tips; keep the source's substantial crossbars.
for n in f.getGlyphOrder():
 pen=TTGlyphPen(None);record=DecomposingRecordingPen(gs);gs[n].draw(record);record.replay(TransformPen(pen,(.57,0,0,1,0,0)));changed[n]=pen.glyph()
for n,g in changed.items():
 f['glyf'][n]=g;g.recalcBounds(f['glyf']);adv,_=f['hmtx'][n];f['hmtx'][n]=(round(adv*.57),getattr(g,'xMin',0))
# Custom reference-specific capitals. All outlines are real individual Unicode glyphs.
def glyph(ch,width,fn):
 p=TTGlyphPen(None);t=TransformPen(Cu2QuPen(p,1.0),(.81,0,0,.81,25,0));fn(t)
 g=p.glyph();g.recalcBounds(f['glyf']);f['glyf'][cmap[ord(ch)]]=g;f['hmtx'][cmap[ord(ch)]]=(round(width*.81+50),g.xMin)
def poly(p,pts):
 p.moveTo(pts[0]);[p.lineTo(x) for x in pts[1:]];p.closePath()
def stem(p,x,w):
 p.moveTo((x-22,0));p.qCurveTo((x,10),(x,38));p.lineTo((x,962));p.qCurveTo((x,992),(x-22,1000));p.lineTo((x+w+22,1000));p.qCurveTo((x+w,992),(x+w,962));p.lineTo((x+w,38));p.qCurveTo((x+w,10),(x+w+22,0));p.closePath()
def pi(p):
 stem(p,0,100);stem(p,205,100);poly(p,[(70,1000),(235,1000),(235,930),(70,930)])
def oh(p):
 p.moveTo((155,1010));p.curveTo((40,1010),(0,958),(0,840));p.lineTo((0,160));p.curveTo((0,42),(40,-10),(155,-10));p.curveTo((270,-10),(310,42),(310,160));p.lineTo((310,840));p.curveTo((310,958),(270,1010),(155,1010));p.closePath()
 p.moveTo((155,965));p.curveTo((188,965),(192,941),(192,880));p.lineTo((192,120));p.curveTo((192,59),(188,35),(155,35));p.curveTo((122,35),(118,59),(118,120));p.lineTo((118,880));p.curveTo((118,941),(122,965),(155,965));p.closePath()
def che(p):
 stem(p,215,100);p.moveTo((0,1000));p.lineTo((100,1000));p.lineTo((100,660));p.qCurveTo((100,574),(154,574));p.qCurveTo((199,574),(235,613));p.lineTo((235,555));p.qCurveTo((171,500),(113,500));p.qCurveTo((0,500),(0,650));p.closePath()
def ee(p):
 stem(p,0,100);poly(p,[(90,1000),(300,1000),(300,855),(285,855),(273,930),(90,930)])
 poly(p,[(90,537),(240,537),(256,610),(268,610),(268,385),(256,385),(240,467),(90,467)])
 poly(p,[(90,70),(273,70),(289,155),(303,155),(303,0),(90,0)])
def em(p):
 stem(p,0,65);stem(p,330,65);poly(p,[(60,1000),(142,1000),(249,243),(190,173)]);poly(p,[(325,1000),(345,1000),(249,243),(190,173)])
def why(p):
 poly(p,[(-10,1000),(135,1000),(115,972),(220,470),(193,255),(27,962)])
 poly(p,[(282,1000),(430,1000),(408,963),(273,403),(273,38),(297,0),(146,0),(170,38),(170,403),(301,960)])
def hh(p):
 stem(p,0,100);stem(p,235,100);poly(p,[(80,570),(255,570),(255,495),(80,495)])
def ww(p):
 poly(p,[(-22,1000),(124,1000),(105,962),(197,216),(155,0),(128,0),(0,962)])
 poly(p,[(199,1000),(284,1000),(370,214),(332,0),(307,0),(218,731),(155,0),(128,0)])
 poly(p,[(410,1000),(462,1000),(442,962),(332,0),(307,0),(428,962)])
for ch,w,fn in [('П',305,pi),('О',310,oh),('O',310,oh),('Ч',315,che),('Е',305,ee),('E',305,ee),('М',395,em),('M',395,em),('У',430,why),('Y',430,why),('H',335,hh),('Н',335,hh),('W',460,ww)]:glyph(ch,w,fn)
family='CF Architectural Reference'
for rec in f['name'].names:
 vals={1:family,2:'Regular',3:family+' 1.0',4:family,6:'CFArchitecturalReference-Regular',16:family,17:'Regular'}
 if rec.nameID in vals:rec.string=vals[rec.nameID].encode(rec.getEncoding(),errors='replace')
f.save(out/'CFArchitecturalReference.ttf')
print(out/'CFArchitecturalReference.ttf')
