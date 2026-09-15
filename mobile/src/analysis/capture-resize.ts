/**
 * Pure resize-decision helper (section 3 of the Mobile Phase B
 * post-merge audit repair pass). Previously `prepareCaptureForSubmission`
 * called `manipulateAsync` with `resize: { width: MAX_IMAGE_DIMENSION }`
 * unconditionally -- that forces every image to exactly that width
 * regardless of its own dimensions, which both upscales small source
 * images (corrupting the backend's resolution-quality signal,
 * `CaptureAssessor._assess_resolution()`) and applies the wrong
 * dimension for a portrait capture (constraining width, not the true
 * long edge, so a portrait photo's height could still exceed 1600px).
 *
 * `computeResizeDimensions` decides, from the camera's own reported
 * width/height (never guessed from a URI), whether resizing is needed
 * at all and what the resulting dimensions must be to cap the LONG
 * EDGE at `maxLongEdge` while preserving aspect ratio exactly.
 * Returns `null` when the source is already within bounds -- the
 * caller must skip the resize action entirely in that case, never
 * upscale.
 */
export type ImageDimensions = {
  width: number;
  height: number;
};

export function computeResizeDimensions(
  source: ImageDimensions,
  maxLongEdge: number,
): ImageDimensions | null {
  const longEdge = Math.max(source.width, source.height);
  if (longEdge <= maxLongEdge) {
    // Already within bounds (including a source smaller than the cap)
    // -- never upscale.
    return null;
  }
  const scale = maxLongEdge / longEdge;
  return {
    width: Math.round(source.width * scale),
    height: Math.round(source.height * scale),
  };
}
