#!/usr/bin/env python3
"""Build deterministic vector and raster brand assets. Developer-only dependency: Pillow."""
from pathlib import Path
from xml.sax.saxutils import escape
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets'
BUNDLE = ROOT / 'integrations/deepseek-harness/assets'
NAVY = '#082B63'
EDGE = '#235BB4'
AMBER = '#FFB72B'
WHITE = '#FFFFFF'
# The same cubic geometry drives SVG and supersampled raster exports.
BELL = [('M',512,278),('C',402,278,314,366,314,474),('L',314,536),
        ('C',314,592,294,626,253,656),('C',223,679,239,716,273,716),
        ('L',751,716),('C',785,716,801,679,771,656),
        ('C',730,626,710,592,710,536),('L',710,474),
        ('C',710,366,622,278,512,278),('Z',)]
CLAPPER = [('M',438,744),('L',586,744),('C',584,786,555,818,512,818),
           ('C',469,818,440,786,438,744),('Z',)]

def path_data(commands):
    return ' '.join(c[0]+' '.join(str(v) for v in c[1:]) for c in commands)

def polygon(commands):
    points=[]; current=None
    for c in commands:
        if c[0] in ('M','L'):
            current=(c[1],c[2]); points.append(current)
        elif c[0]=='C':
            p0=current; p1=(c[1],c[2]); p2=(c[3],c[4]); p3=(c[5],c[6])
            for i in range(1,49):
                t=i/48; u=1-t
                points.append(tuple(u**3*p0[j]+3*u*u*t*p1[j]+3*u*t*t*p2[j]+t**3*p3[j] for j in (0,1)))
            current=p3
    return points

def main():
    for folder in (ASSETS,BUNDLE): folder.mkdir(parents=True,exist_ok=True)
    svg=f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" role="img" aria-labelledby="title desc">
  <title id="title">Agent Action Reminder</title>
  <desc id="desc">Notification bell with an amber pending-action indicator.</desc>
  <rect x="48" y="48" width="928" height="928" rx="208" fill="{NAVY}"/>
  <rect x="60" y="60" width="904" height="904" rx="196" fill="none" stroke="{EDGE}" stroke-width="8"/>
  <rect x="466" y="216" width="92" height="130" rx="46" fill="{WHITE}"/>
  <path d="{path_data(BELL)}" fill="{WHITE}"/>
  <path d="{path_data(CLAPPER)}" fill="{WHITE}"/>
  <circle cx="704" cy="334" r="118" fill="{NAVY}"/>
  <circle cx="704" cy="334" r="88" fill="{AMBER}"/>
</svg>
'''
    (ASSETS/'icon.svg').write_text(svg,encoding='utf-8')
    (BUNDLE/'icon.svg').write_text(svg,encoding='utf-8')
    scale=4
    image=Image.new('RGBA',(1024*scale,1024*scale),(0,0,0,0)); draw=ImageDraw.Draw(image)
    def box(values): return tuple(v*scale for v in values)
    draw.rounded_rectangle(box((48,48,976,976)),radius=208*scale,fill=NAVY)
    # Match SVG stroke center: raster rounded-rectangle outline is drawn inside.
    draw.rounded_rectangle(box((56,56,968,968)),radius=200*scale,outline=EDGE,width=8*scale)
    draw.rounded_rectangle(box((466,216,558,346)),radius=46*scale,fill=WHITE)
    for commands in (BELL,CLAPPER):
        draw.polygon([(x*scale,y*scale) for x,y in polygon(commands)],fill=WHITE)
    draw.ellipse(box((586,216,822,452)),fill=NAVY)
    draw.ellipse(box((616,246,792,422)),fill=AMBER)
    for size in (16,32,64,128,256,512,1024):
        image.resize((size,size),Image.Resampling.LANCZOS).save(ASSETS/f'icon-{size}.png',optimize=True)
    (BUNDLE/'icon-256.png').write_bytes((ASSETS/'icon-256.png').read_bytes())
    image.resize((256,256),Image.Resampling.LANCZOS).save(ASSETS/'favicon.ico',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
    print('Built SVG, 7 PNG sizes, ICO, and native bundle assets.')

if __name__=='__main__': main()
