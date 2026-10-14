"""Offline asset/manifest regression checks; no Pillow needed at test time."""
import json
from pathlib import Path
import struct
import unittest
import xml.etree.ElementTree as ET
import zlib

ROOT=Path(__file__).resolve().parents[1]
BUNDLE=ROOT/'integrations/deepseek-harness'

class AssetTests(unittest.TestCase):
    def test_svg_is_self_contained(self):
        root=ET.fromstring((ROOT/'assets/icon.svg').read_text())
        self.assertEqual(root.get('viewBox'),'0 0 1024 1024')
        allowed={'svg','title','desc','rect','path','circle'}
        for element in root.iter():
            self.assertIn(element.tag.rsplit('}',1)[-1],allowed)
            self.assertFalse(any('href' in k or k.startswith('on') for k in element.attrib))
        self.assertLess((ROOT/'assets/icon.svg').stat().st_size,256*1024)

    def test_png_sizes_and_chunk_integrity(self):
        for size in (16,32,64,128,256,512,1024):
            with self.subTest(size=size):
                data=(ROOT/f'assets/icon-{size}.png').read_bytes()
                self.assertEqual(data[:8],b'\x89PNG\r\n\x1a\n')
                self.assertEqual(struct.unpack('>II',data[16:24]),(size,size))
                self.assertEqual(data[25],6)  # RGBA, not an opaque white background.
                offset=8; last=None
                while offset<len(data):
                    length=struct.unpack('>I',data[offset:offset+4])[0]
                    chunk=data[offset+4:offset+8+length]
                    crc=struct.unpack('>I',data[offset+8+length:offset+12+length])[0]
                    self.assertEqual(zlib.crc32(chunk)&0xffffffff,crc)
                    last=chunk[:4]; offset+=length+12
                self.assertEqual(last,b'IEND')
                self.assertEqual(offset,len(data))

    def test_native_icon_metadata_resolves_inside_package(self):
        manifest=json.loads((BUNDLE/'package.json').read_text())
        self.assertEqual(manifest['exports']['./package.json'],'./package.json')
        self.assertIn('assets',manifest['files'])
        icon=(BUNDLE/manifest['icon']).resolve()
        self.assertIn(BUNDLE.resolve(),icon.parents)
        self.assertTrue(icon.is_file())
        self.assertLess(icon.stat().st_size,256*1024)
        self.assertNotIn('icon',manifest['dsh']['bundle'])

    def test_native_copies_match_brand_assets(self):
        for name in ('icon.svg','icon-256.png'):
            self.assertEqual((BUNDLE/'assets'/name).read_bytes(),(ROOT/'assets'/name).read_bytes())

    def test_ico_contains_valid_size_entries(self):
        data=(ROOT/'assets/favicon.ico').read_bytes()
        reserved,kind,count=struct.unpack('<HHH',data[:6])
        self.assertEqual((reserved,kind,count),(0,1,6))
        sizes=[]
        for index in range(count):
            w,h,colors,reserved,planes,bits,length,offset=struct.unpack('<BBBBHHII',data[6+16*index:22+16*index])
            sizes.append(w or 256)
            self.assertEqual(w,h)
            self.assertGreater(length,0)
            self.assertGreaterEqual(offset,6+count*16)
            self.assertLessEqual(offset+length,len(data))
        self.assertEqual(sizes,[16,32,48,64,128,256])

if __name__=='__main__': unittest.main()
