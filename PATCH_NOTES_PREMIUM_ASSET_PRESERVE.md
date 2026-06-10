# Premium SMK asset-preserve patch

## Changed direction
This patch removes the previous approach that re-overlaid logo / QR / representative drawing / header / IP / footer on top of the generated premium image.

## What changed
1. **Removed post-overlay usage in premium generation flow**
   - Premium generation no longer applies the automatic post-compositing correction pipeline on top of the generated image.
   - The premium result is now returned directly from the model output.

2. **Strengthened asset-preservation references**
   - Premium generation now passes these source assets as explicit reference images:
     - current normal SMK image
     - university logo
     - university brand palette card
     - PIUM+QR card
     - representative drawing
     - application/product images used in the normal SMK

3. **Prompt updated for exact asset reuse**
   - The premium prompt now explicitly instructs the model to keep the supplied assets faithful and not replace them with newly generated substitutes.
   - This applies especially to:
     - university logo
     - PIUM+QR block
     - representative drawing
     - application/product images

4. **UI caption updated**
   - The app description now reflects that post-overlay compositing is no longer used.

## Main modified file
- `app.py`

## Note
Because the premium result is still image-model generated, exact pixel-perfect preservation cannot be guaranteed in every case. However, this patch shifts the behavior toward using the original supplied assets directly during generation rather than correcting them afterward.
