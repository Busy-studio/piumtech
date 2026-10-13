from pathlib import Path
import ast

p=Path('app.py')
s=p.read_text(encoding='utf-8')
import hashlib
assert hashlib.sha1(b'blob '+str(len(p.read_bytes())).encode()+b'\0'+p.read_bytes()).hexdigest() == '67cb66ec118b4378dbbdc8bbddd295f0a93a88e1', 'app.py base changed; do not overwrite concurrent work'


def replace(old, new):
 global s
 if s.count(old) != 1: raise ValueError((s.count(old), old[:140]))
 s=s.replace(old,new)

def function(name,new):
 global s
 node=next(n for n in ast.parse(s).body if isinstance(n,ast.FunctionDef) and n.name==name)
 lines=s.splitlines(keepends=True)
 s=''.join(lines[:node.lineno-1])+new.rstrip()+'\n'+''.join(lines[node.end_lineno:])

replace('import shutil\n','import shutil\nimport hashlib\nimport logging\n\nfrom patent_drawing import (\n    extract_drawing_result, extract_representative_drawing,\n    parse_representative_figure_number as _parse_representative_figure_number,\n    is_target_figure_page as _is_target_figure_page,\n)\nfrom smk_drawing_assets import (\n    DRAWING_POLICY_VERSION, reset_document_state, make_drawing_slot_reference,\n    reference_manifest, drawing_slot_instructions, composite_verified_drawing,\n)\n')
# Remove obsolete selection functions. Compatibility names are imported above.
for name in ('_parse_representative_figure_number','_is_target_figure_page','extract_representative_drawing'):
 function(name,'# Representative drawing selection is implemented in patent_drawing.py.')
# Only actual drawing branches change; unrelated image rendering stays strict.
for old in [
 '    rep = fit_image(rep_img, (rep_w_box-74, 214), bg=(255,255,255), trim=True)\n    im.paste(rep, (X+37, y+82))',
 '    rep = fit_image(rep_img, (420, 228), bg=(255,255,255), trim=True)\n    im.paste(rep, (24 + (490-rep.width)//2, comp_y+92))',
 '    slide.shapes.add_picture(img_bytes(fit_image(rep_img, (rep_w_box-74,214), trim=True)), px(X+37), px(y+82), width=px(rep_w_box-74), height=px(214))',
]:
 replace(old,'    if rep_img is not None:\n'+ '\n'.join('    '+line for line in old.splitlines()))
s=s.replace('rep_img: Image.Image,','rep_img: Image.Image | None,')
# A deterministic premium fallback must fit the drawing fully *inside* its card.
replace('(420, 228), bg=(255,255,255), trim=True)', '(420, 204), bg=(255,255,255), trim=True)')
# Dynamic manifest instead of fixed A/B/C/D/E that omitted the brand palette.
start=s.index('def build_premium_infographic_ai_prompt(')
end=s.index('\ndef _extract_generated_image_bytes', start)
part=s[start:end]
part=part.replace('university_logo: Image.Image | None = None)', 'university_logo: Image.Image | None = None, reference_descriptions: List[str] | None = None, has_drawing: bool = True)')
a=part.index('You are given multiple reference images:')
b=part.index('\nCreate ONE premium',a)
part=part[:a]+'''You are given these reference images in their exact upload order:
{reference_manifest(reference_descriptions or [])}
Only the current SMK and the content below are factual sources. The sanitized design
reference supplies layout/style only, never patent data, diagrams, titles or numbers.
{drawing_slot_instructions(has_drawing)}
'''+part[b:]
part=part.replace("Image A's layout quality and Image B's actual content", "the design reference's layout quality and the current SMK's actual content")
part=part.replace("Image A in layout quality", "the design reference in layout quality")
part=part.replace("Image A's blue color palette", "the design reference's blue color palette")
part=part.replace('Image C brand color', 'the supplied university logo brand color')
part=part.replace('- Preserve the representative drawing faithfully in the representative drawing section. Do not replace it with a different diagram.', '- Do not draw the representative drawing; reserve the solid placeholder exactly as specified above.')
part=part.replace('- The final premium image itself must already contain the logo, PIUM+QR block, and representative drawing correctly. Do not rely on any later overlay or post-compositing step.', '- Preserve available logos/PIUM assets. ONLY the representative drawing is inserted deterministically after generation.')
s=s[:start]+part+s[end:]
# Keep premium styling but validate and fill an exact drawing slot, never return raw AI drawings.
function('generate_reference_based_premium_infographic', '''def generate_reference_based_premium_infographic(
    data: Dict[str, Any],
    rep_img: Image.Image | None,
    app_imgs: List[Image.Image],
    contact: str,
    university_logo: Image.Image | None = None,
    pium_logo: Image.Image | None = None,
    qr_img: Image.Image | None = None,
    piumlink_logo: Image.Image | None = None,
    base_smk_img: Image.Image | None = None,
) -> Image.Image:
    """Sanitized references + dynamic manifest + exact drawing/blank composition.

    If the generated drawing slot cannot be identified safely, use the current
    document's rule-based renderer instead of returning an unverified AI diagram.
    """
    temp_paths: List[str] = []
    descriptions: List[str] = []
    def add_reference(image, prefix, description):
        temp_paths.append(_save_temp_png(image, prefix))
        descriptions.append(description)
    try:
        client = get_client()
        with Image.open(resolve_premium_reference_path()) as source:
            add_reference(make_drawing_slot_reference(source), 'premium_style_',
                          'Sanitized DESIGN/STYLE reference with a solid magenta drawing slot. Not a source of patent facts.')
        if base_smk_img is not None:
            add_reference(base_smk_img, 'premium_base_smk_', 'CURRENT SMK: factual content and information architecture. Do not redraw its drawing.')
        if university_logo is not None:
            add_reference(university_logo, 'premium_univ_logo_', 'Original university logo; preserve this exact asset.')
        add_reference(make_brand_palette_reference_card(university_logo), 'premium_brand_palette_',
                      'University BRAND COLOR PALETTE, not a logo or drawing.')
        add_reference(make_pium_qr_reference_card(pium_logo, qr_img, piumlink_logo), 'premium_pium_qr_',
                      'PIUM/QR asset card; use only the assets actually present, never invent a QR.')
        if rep_img is not None:
            add_reference(rep_img, 'premium_rep_drawing_',
                          'Verified CURRENT PDF drawing, for context only. Code inserts these pixels after generation; do not redraw or duplicate it.')
        prompt = build_premium_infographic_ai_prompt(
            data, contact, university_logo=university_logo,
            reference_descriptions=descriptions, has_drawing=rep_img is not None,
        )
        with ExitStack() as stack:
            files = [stack.enter_context(open(p, 'rb')) for p in temp_paths]
            result = client.images.edit(model=PREMIUM_IMAGE_MODEL_FIXED, image=files,
                                        prompt=prompt, size='1024x1536', quality=PREMIUM_IMAGE_QUALITY)
        generated = Image.open(BytesIO(_extract_generated_image_bytes(result))).convert('RGB')
        return composite_verified_drawing(generated, rep_img)
    except Exception as exc:
        logging.getLogger(__name__).warning('Premium drawing validation/generation failed: %s', type(exc).__name__)
        st.warning('프리미엄 도면 영역을 안전하게 확인하지 못해 원본 도면을 보존하는 규칙 기반 출력으로 전환했습니다.')
        return make_high_quality_infographic_image(
            data, rep_img, app_imgs, contact, university_logo=university_logo,
            pium_logo=pium_logo, qr_img=qr_img, piumlink_logo=piumlink_logo,
        )
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except OSError:
                pass
''')
replace('    "hq_image": None, "hq_png_bytes": None, "hq_pdf_bytes": None\n', '    "hq_image": None, "hq_png_bytes": None, "hq_pdf_bytes": None,\n    "drawing_meta": None, "source_pdf_sha256": None, "drawing_policy_version": None,\n')
replace('if generate_btn:\n', '''# Bind every result to PDF bytes, not the filename. Also invalidate pre-fix sessions.
current_pdf_sha256 = hashlib.sha256(uploaded_pdf.getvalue()).hexdigest() if uploaded_pdf is not None else None
if (st.session_state.source_pdf_sha256 != current_pdf_sha256
        or st.session_state.drawing_policy_version != DRAWING_POLICY_VERSION):
    reset_document_state(st.session_state, current_pdf_sha256)

if generate_btn:
    # Clear before validation/API calls so failed generation cannot expose old data.
    reset_document_state(st.session_state, current_pdf_sha256)
''')
replace('        rep_img = extract_representative_drawing(pdf_path, page_texts=page_texts, ocr_used=ocr_used)', '''        drawing_result = extract_drawing_result(pdf_path, page_texts=page_texts, ocr_used=ocr_used)
        rep_img = drawing_result.image
        st.session_state.drawing_meta = drawing_result.metadata()''')
replace('        st.session_state.data = data\n        st.session_state.edit_version += 1', '        # Publish data only after every output has been built successfully.\n        st.session_state.edit_version += 1')
replace('        data = localize_smk_data(data, output_language, university, department, professor)\n', '        data = localize_smk_data(data, output_language, university, department, professor)\n        data["drawing"] = st.session_state.drawing_meta\n')
replace('        st.session_state.pptx_bytes = make_pptx_bytes(data, rep_img, app_imgs, contact, university_logo, pium_logo, st.session_state.qr_img, piumlink_logo)', '        st.session_state.pptx_bytes = make_pptx_bytes(data, rep_img, app_imgs, contact, university_logo, pium_logo, st.session_state.qr_img, piumlink_logo)\n        st.session_state.data = data')
replace('        st.subheader("SMK 미리보기")', '''        st.subheader("SMK 미리보기")
        drawing_meta = st.session_state.drawing_meta or {}
        if drawing_meta.get("status") == "selected":
            st.caption(f"대표도면: 원본 PDF {drawing_meta['page']}페이지 · {drawing_meta.get('reason', '')}")
        elif drawing_meta.get("status") in ("review_required", "error"):
            st.warning(drawing_meta.get("reason", "도면 확인이 필요합니다. 현재 출력은 공란입니다."))
        else:
            st.caption("대표도면: 원본에서 확인된 도면이 없어 공란으로 출력합니다.")''')
replace('                rep_img = st.session_state.rep_img or extract_representative_drawing(st.session_state.pdf_path)', '''                # None is a valid, completed no-drawing result; never re-extract or reuse another PDF.
                rep_img = st.session_state.rep_img
                edited["drawing"] = st.session_state.drawing_meta''')
old=next(line for line in s.splitlines() if '별도의 후합성 없이 생성 결과 자체를 그대로 사용하며' in line)
replace(old, '            st.caption("디자인 참고 이미지의 예시 도면은 제거하며, 실제 업로드 순서에 맞춰 참조 이미지를 전달합니다. 대표도면은 현재 PDF의 원본을 직접 삽입하고, 도면이 없으면 공란으로 유지합니다. 생성 결과의 도면 영역을 확인하지 못하면 규칙 기반 고품질 출력으로 전환합니다.")')
ast.parse(s)
p.write_text(s, encoding='utf-8')
