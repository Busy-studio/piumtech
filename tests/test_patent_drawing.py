import os
import tempfile
import unittest
from pathlib import Path
from io import BytesIO

import fitz
from PIL import Image, ImageDraw

from patent_drawing import extract_drawing_result, parse_representative_figure_number, is_target_figure_page


def box_image(size=(480, 600)):
    image = Image.new('RGB', size, 'white')
    d = ImageDraw.Draw(image)
    for y in (40, 210, 400):
        d.rectangle((40, y, size[0] - 40, y + 100), outline='black', width=3)
    d.line((size[0] // 2, 140, size[0] // 2, 400), fill='black', width=3)
    stream = BytesIO(); image.save(stream, format='PNG')
    return stream.getvalue()


def korean(page, text, position=(40, 40)):
    page.insert_text(position, text, fontname='korea', fontsize=12)


class DrawingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, doc, name='test.pdf'):
        p = self.root / name
        doc.save(p); doc.close()
        return str(p)

    def test_representative_notation(self):
        for text in ('【대표도】\n도 1', '[대표도]\n도 1', '대표도 - 도1', '대 표 도 : 도 1', '［대표도］ 도 1'):
            with self.subTest(text=text):
                self.assertEqual(parse_representative_figure_number(text), '1')
        self.assertEqual(parse_representative_figure_number('【대표도】\n도 2(a)'), '2(a)')
        self.assertIsNone(parse_representative_figure_number('도면의 간단한 설명\n도 1은 ...'))

    def test_metadata_and_prose_are_not_captions(self):
        for text in ('【대표도】\n도 1', '도 1은 시스템을 설명한다.', '【도면의 간단한 설명】\n도 1은 ...'):
            self.assertFalse(is_target_figure_page(text, '1'))
        self.assertTrue(is_target_figure_page('【도면】\n【도 1】\n42-36', '1'))
        self.assertFalse(is_target_figure_page('【도 10】', '1'))

    def test_explicit_figure_wins_over_cover_image(self):
        doc = fitz.open(); p = doc.new_page()
        p.insert_image(fitz.Rect(100, 100, 400, 500), stream=box_image())
        korean(p, '【대표도】\n도 2', (40, 50))
        p = doc.new_page(); korean(p, '【도 1】')
        p.insert_image(fitz.Rect(70, 100, 370, 500), stream=box_image())
        p = doc.new_page(); korean(p, '【도 2】')
        p.insert_image(fitz.Rect(70, 100, 450, 600), stream=box_image())
        # Cover declaration is deliberately ABOVE an unrelated cover image: for a
        # non-gazette it must not override an explicit, labelled figure page.
        result = extract_drawing_result(self.save(doc))
        self.assertEqual(result.page, 3)
        self.assertEqual(result.figure, '2')

    def test_first_unlabelled_before_later_labelled(self):
        doc = fitz.open(); p = doc.new_page(); korean(p, '발명 설명 본문')
        p = doc.new_page(); p.insert_image(fitz.Rect(60, 60, 520, 700), stream=box_image())
        p = doc.new_page(); korean(p, '【도 1】')
        p.insert_image(fitz.Rect(70, 100, 450, 600), stream=box_image())
        self.assertEqual(extract_drawing_result(self.save(doc)).page, 2)

    def test_two_figures_on_one_page(self):
        doc = fitz.open(); p = doc.new_page(); korean(p, '【대표도】\n도 2')
        p = doc.new_page(); korean(p, '【도 1】', (40, 40)); korean(p, '【도 2】', (40, 430))
        p.insert_image(fitz.Rect(100, 65, 300, 355), stream=box_image())
        p.insert_image(fitz.Rect(60, 460, 500, 700), stream=box_image((600, 510)))
        result = extract_drawing_result(self.save(doc))
        self.assertEqual(result.figure, '2')
        self.assertGreater(result.image.width, result.image.height)

    def test_vector_figure(self):
        doc = fitz.open(); p = doc.new_page(); korean(p, '【도 1】')
        p.draw_rect(fitz.Rect(100, 150, 350, 250))
        p.draw_line((225, 250), (225, 330))
        p.draw_rect(fitz.Rect(100, 330, 350, 430))
        result = extract_drawing_result(self.save(doc))
        self.assertIsNotNone(result.image)
        self.assertEqual(result.page, 1)

    def test_no_drawing_with_references_and_formula(self):
        doc = fitz.open(); p = doc.new_page()
        korean(p, '【도면의 간단한 설명】\n도 1은 시스템을 설명한다.\n' + '본문 설명만 존재합니다.\n' * 20)
        p.insert_image(fitz.Rect(80, 650, 400, 680), stream=box_image())
        result = extract_drawing_result(self.save(doc))
        self.assertIsNone(result.image)

    def test_declared_but_missing_is_blank_not_first_other_figure(self):
        doc = fitz.open(); p = doc.new_page(); korean(p, '【대표도】\n도 9')
        p = doc.new_page(); korean(p, '【도 1】')
        p.insert_image(fitz.Rect(70, 100, 450, 600), stream=box_image())
        result = extract_drawing_result(self.save(doc))
        self.assertIsNone(result.image)
        self.assertEqual(result.status, 'review_required')

    def test_empty_figure_caption_is_not_a_drawing(self):
        doc = fitz.open(); korean(doc.new_page(), '【도 1】')
        result = extract_drawing_result(self.save(doc))
        self.assertIsNone(result.image)
        self.assertEqual(result.status, 'review_required')

    def test_text_scan_is_not_a_drawing(self):
        image = Image.new('RGB', (600, 800), 'white'); d = ImageDraw.Draw(image)
        for y in range(40, 720, 24): d.text((30, y), 'Plain text specification without any drawing.', fill='black')
        bio = BytesIO(); image.save(bio, format='PNG')
        doc = fitz.open(); p = doc.new_page(); p.insert_image(p.rect, stream=bio.getvalue())
        self.assertIsNone(extract_drawing_result(self.save(doc), ocr_used=True).image)

    def test_no_leak_between_documents(self):
        doc = fitz.open(); p = doc.new_page(); korean(p, '【도 1】')
        p.insert_image(fitz.Rect(70, 100, 450, 600), stream=box_image())
        first = extract_drawing_result(self.save(doc, 'first.pdf'))
        doc = fitz.open(); korean(doc.new_page(), '도면이 없는 설명서')
        second = extract_drawing_result(self.save(doc, 'second.pdf'))
        self.assertIsNotNone(first.image); self.assertIsNone(second.image)
        self.assertNotEqual(first.source_sha256, second.source_sha256)

    def test_invalid_pdf_is_explicit_error(self):
        p = self.root / 'bad.pdf'; p.write_bytes(b'not a PDF')
        result = extract_drawing_result(str(p))
        self.assertEqual(result.status, 'error'); self.assertIsNone(result.image)

    def test_ocr_guided_figure_scan(self):
        doc = fitz.open(); doc.new_page()
        p = doc.new_page(); p.insert_image(fitz.Rect(60, 60, 520, 700), stream=box_image())
        result = extract_drawing_result(self.save(doc), ['【대표도】\n도 2', '【도 2】'], ocr_used=True)
        self.assertEqual(result.page, 2)
        self.assertEqual(result.figure, '2')

    def test_gazette_inline_representative(self):
        doc = fitz.open(); p = doc.new_page()
        korean(p, '등록특허공보\n대표도 - 도 3')
        p.insert_image(fitz.Rect(70, 140, 450, 620), stream=box_image())
        result = extract_drawing_result(self.save(doc))
        self.assertEqual(result.page, 1)
        self.assertEqual(result.figure, '3')

    def test_numberless_vector_diagram(self):
        doc = fitz.open(); p = doc.new_page()
        p.draw_rect(fitz.Rect(100, 150, 350, 250))
        p.draw_line((225, 250), (225, 330))
        p.draw_rect(fitz.Rect(100, 330, 350, 430))
        result = extract_drawing_result(self.save(doc))
        self.assertIsNotNone(result.image)
        self.assertEqual(result.page, 1)

    def test_real_samples_when_supplied(self):
        # Never commit unpublished patent PDFs into the repository.
        root = os.environ.get('PIUM_DRAWING_SAMPLE_DIR')
        if not root: self.skipTest('Optional private fixtures are not supplied')
        for name, expected in [('10-2026-0061085_명세서 (2).pdf', 36), ('P2026-0250-KR00_임시명세서 (1).pdf', 5)]:
            with self.subTest(name=name):
                result = extract_drawing_result(str(Path(root) / name))
                self.assertEqual(result.page, expected)
                self.assertIsNotNone(result.image)


if __name__ == '__main__': unittest.main()
