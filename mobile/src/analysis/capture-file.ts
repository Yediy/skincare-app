import { File } from "expo-file-system";
import { manipulateAsync, SaveFormat } from "expo-image-manipulator";

import { JPEG_QUALITY, MAX_IMAGE_DIMENSION } from "@/constants/capture";
import { logger } from "@/utils/logger";

import { computeResizeDimensions } from "./capture-resize";

export type PreparedCapture = {
  /** Local file URI of the resized/compressed working copy -- a
   * DIFFERENT file from the original camera capture, so both are
   * independently trackable/deletable. Still just a cache-directory
   * path; never written anywhere durable (AsyncStorage/SQLite/
   * SecureStore/photo library/etc. -- see src/utils/storage-policy.ts). */
  uri: string;
  base64: string;
  width: number;
  height: number;
};

/**
 * The one place a captured photo becomes the base64 string this app
 * submits (section 8/24/25). Bounded per src/constants/capture.ts --
 * this is a first, cheap client-side filter (resolution/size), never a
 * substitute for the backend's own real capture-quality assessment
 * (and never trusted as one server-side either -- see the v2 backend's
 * own independent image-size/dimension limits).
 *
 * Resize decision is delegated to the pure, unit-tested
 * `computeResizeDimensions()` (capture-resize.ts): a `null` result
 * means the source is already within `MAX_IMAGE_DIMENSION`'s long-edge
 * bound, so no resize action is issued at all -- this never upscales a
 * small source image, and always uses the camera's own reported
 * width/height rather than guessing orientation from the URI.
 *
 * Deliberately returns base64 as a plain return value, never storing
 * it anywhere itself -- the caller (src/analysis/use-submit-
 * analysis.ts) holds it only for the duration of one submitAnalysis()
 * call and never places it in Query cache, global state, or a log
 * line.
 */
export async function prepareCaptureForSubmission(
  sourceUri: string,
  sourceWidth: number,
  sourceHeight: number,
): Promise<PreparedCapture> {
  const resizeDimensions = computeResizeDimensions({ width: sourceWidth, height: sourceHeight }, MAX_IMAGE_DIMENSION);
  const actions = resizeDimensions ? [{ resize: resizeDimensions }] : [];

  const result = await manipulateAsync(
    sourceUri,
    actions,
    { compress: JPEG_QUALITY, format: SaveFormat.JPEG, base64: true },
  );
  if (!result.base64) {
    // Should be unreachable (base64: true was requested above) --
    // fails loudly rather than silently submitting no image data. The
    // manipulator still wrote a working-copy file at result.uri even
    // though it gave us no base64 -- best-effort-delete it before
    // throwing so a repeated failure here can't accumulate abandoned
    // temp files (section 2).
    await deleteLocalCaptureFile(result.uri);
    throw new Error("Image processing did not produce image data");
  }
  return { uri: result.uri, base64: result.base64, width: result.width, height: result.height };
}

/**
 * Best-effort deletion of a local temp file (the original camera
 * capture, or a prepareCaptureForSubmission() working copy). Never
 * throws -- called from success, cancel, retake, and abandonment
 * paths alike (section 7), and none of those should ever be blocked
 * or crash on a cleanup failure. Never logs the URI itself: a
 * filesystem path here always points at image data, so it gets the
 * same treatment as the image bytes themselves under this app's
 * logging-redaction policy (src/utils/logger.ts).
 */
export async function deleteLocalCaptureFile(uri: string): Promise<void> {
  try {
    const file = new File(uri);
    if (file.exists) {
      file.delete();
    }
  } catch {
    logger.warn("failed to delete a local capture temp file; the OS cache directory will reclaim it regardless");
  }
}
