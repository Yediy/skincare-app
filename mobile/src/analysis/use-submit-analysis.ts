import { useMutation } from "@tanstack/react-query";

import { submitAnalysis } from "@/api/analysis-api";
import type { AnalysisSubmitResponse } from "@/types/domain";

import { deleteLocalCaptureFile, prepareCaptureForSubmission } from "./capture-file";

export type SubmitCaptureInput = {
  /** The original camera-captured file URI (before resize/compress). */
  capturedImageUri: string;
  requestId: string;
};

/**
 * Prepare -> submit -> best-effort-delete-on-success (section 7/8/9/10),
 * exported as a plain function so this orchestration is unit-testable
 * without rendering a hook or a QueryClient. On failure, BOTH the
 * original capture and the resized working copy are deliberately left
 * in place -- a retry (section 20: "retrying the same network
 * operation must preserve the same request ID") reuses the exact same
 * `capturedImageUri` and re-runs this function from scratch, which is
 * cheap and correct since resizing is idempotent. Cleanup for the
 * abandon/cancel/retake paths is the capture screen's own
 * responsibility (calling deleteLocalCaptureFile directly) -- this
 * function only ever deletes on confirmed success.
 */
export async function submitCapture(input: SubmitCaptureInput): Promise<AnalysisSubmitResponse> {
  const prepared = await prepareCaptureForSubmission(input.capturedImageUri);
  const response = await submitAnalysis({ image_base64: prepared.base64, request_id: input.requestId });
  await deleteLocalCaptureFile(prepared.uri);
  await deleteLocalCaptureFile(input.capturedImageUri);
  return response;
}

export function useSubmitAnalysis() {
  return useMutation<AnalysisSubmitResponse, unknown, SubmitCaptureInput>({
    mutationFn: submitCapture,
    retry: false,
  });
}
