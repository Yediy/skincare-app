import { useMutation } from "@tanstack/react-query";

import { submitAnalysis } from "@/api/analysis-api";
import type { AnalysisSubmitResponse } from "@/types/domain";

import { deleteLocalCaptureFile, prepareCaptureForSubmission } from "./capture-file";

export type SubmitCaptureInput = {
  /** The original camera-captured file URI (before resize/compress). */
  capturedImageUri: string;
  /** Dimensions as reported by the camera capture itself -- see
   * src/analysis/capture-resize.ts. */
  capturedImageWidth: number;
  capturedImageHeight: number;
  requestId: string;
};

/**
 * Prepare -> submit -> cleanup (section 2/7/8/9/10 of the Mobile Phase
 * B post-merge audit repair pass), exported as a plain function so
 * this orchestration is unit-testable without rendering a hook or a
 * QueryClient.
 *
 * Cleanup invariant (see tests/analysis/use-submit-analysis.test.ts):
 *   - The PREPARED/resized working copy is deleted after EVERY
 *     submission attempt, success or failure (the `finally` below) --
 *     it must never accumulate across retries, since
 *     prepareCaptureForSubmission() produces a brand-new working file
 *     on each call.
 *   - The ORIGINAL capture is deleted only on confirmed success. On
 *     failure it is deliberately left in place -- a retry (section 1:
 *     "retrying the same network operation must preserve the same
 *     request_id") reuses the exact same `capturedImageUri` and
 *     re-runs this function from scratch, which is cheap and correct
 *     since resizing is idempotent. Cleanup for the abandon/cancel/
 *     retake paths is the capture screen's own responsibility (calling
 *     deleteLocalCaptureFile directly) -- this function only ever
 *     deletes the original on confirmed success.
 */
export async function submitCapture(input: SubmitCaptureInput): Promise<AnalysisSubmitResponse> {
  const prepared = await prepareCaptureForSubmission(
    input.capturedImageUri,
    input.capturedImageWidth,
    input.capturedImageHeight,
  );
  try {
    const response = await submitAnalysis({ image_base64: prepared.base64, request_id: input.requestId });
    await deleteLocalCaptureFile(input.capturedImageUri);
    return response;
  } finally {
    await deleteLocalCaptureFile(prepared.uri);
  }
}

export function useSubmitAnalysis() {
  return useMutation<AnalysisSubmitResponse, unknown, SubmitCaptureInput>({
    mutationFn: submitCapture,
    retry: false,
  });
}
