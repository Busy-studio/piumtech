# Premium SMK alignment patch

## What was fixed

1. **Premium AI output normalization**
   - `gpt-image-2` output was generated at `1024x1536`, while the internal premium overlay system used a `1240x1754` coordinate base.
   - A new normalization step now fits the generated image to `1240x1754` **before** logo / header / IP / footer corrections are applied.

2. **Section validation hook enabled**
   - `apply_section_validation_and_corrections()` is now actually connected in the premium generation pipeline.
   - If text-heavy sections are too weak or malformed, they are replaced with the stable reference section.

3. **Header/IP/footer overlay coverage widened**
   - The cleanup boxes for the header, IP table, and footer were expanded slightly to reduce ghost text, misalignment, and boundary artifacts.

## Main modified file
- `app.py`

## Expected result
- More accurate premium infographic alignment
- Better header placement
- Cleaner lower IP/footer area
- More stable representative drawing / section layout after premium generation
