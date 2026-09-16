import { CameraView } from "expo-camera";
import { useRouter } from "expo-router";
import React, { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { Image, StyleSheet, Text, View } from "react-native";

import { ApiError } from "@/api/errors";
import { captureAttemptReducer, type CaptureAttempt, type CaptureAttemptAction } from "@/analysis/capture-attempt";
import {
  CAMERA_INIT_FAILURE_MESSAGE,
  CAPTURE_FAILURE_MESSAGE,
  cameraReadinessReducer,
  canStartCapture,
  decideCaptureOwnership,
  INITIAL_CAMERA_READINESS_STATE,
} from "@/analysis/camera-capture-policy";
import { useCameraPermissionState } from "@/analysis/camera-permission";
import { deleteLocalCaptureFile } from "@/analysis/capture-file";
import { useSubmitAnalysis } from "@/analysis/use-submit-analysis";
import { Button } from "@/components/button";
import { LoadingState } from "@/components/loading-state";
import { Screen } from "@/components/screen";
import { useTheme } from "@/theme/theme-provider";

/**
 * Controlled facial capture (sections 5/6/7 of the original pass;
 * sections 1/2/3/4/5/6 of the Mobile Phase B post-merge audit repair
 * pass). Local guidance only -- the oval/center-dot overlay is static
 * visual framing, not real-time face-geometry tracking.
 *
 * request_id identity is bound to the individual captured photo
 * (src/analysis/capture-attempt.ts), never to how long this screen has
 * been mounted: a fresh photo always gets a fresh request_id, and
 * resubmitting the SAME photo after a transient failure reuses it,
 * simply because no new CAPTURED action fires in between.
 */
export default function CaptureScreen() {
  const theme = useTheme();
  const router = useRouter();
  const cameraRef = useRef<CameraView>(null);
  const permission = useCameraPermissionState();
  const submission = useSubmitAnalysis();

  // Wrapped to a plain 2-arg reducer at the call site: useReducer's
  // React 19 typing requires an exact-arity reducer, while
  // captureAttemptReducer's own exported signature deliberately keeps
  // a third, injectable generateRequestId param for deterministic
  // unit tests (tests/analysis/capture-attempt.test.ts) -- the two
  // don't need to agree, since only this one call site is
  // React-typed.
  const [capture, dispatchCapture] = useReducer(
    (state: CaptureAttempt | null, action: CaptureAttemptAction) => captureAttemptReducer(state, action),
    null,
  );
  const [consentDenied, setConsentDenied] = useState(false);

  // Real-device camera readiness/concurrency state (section 4, and
  // post-merge audit repair items 1/2): the capture button is disabled
  // until the CURRENTLY MOUNTED camera instance reports ready --
  // cameraReadiness is invalidated on every path that leaves or
  // returns to the live camera, so a stale "ready" from a previous
  // CameraView instance can never leak into a newly mounted one (see
  // camera-capture-policy.ts's cameraReadinessReducer) -- and
  // capturePending blocks a second concurrent takePictureAsync() call
  // from a rapid repeated tap.
  const [cameraReadiness, dispatchCameraReadiness] = useReducer(cameraReadinessReducer, INITIAL_CAMERA_READINESS_STATE);
  const [capturePending, setCapturePending] = useState(false);
  const [captureError, setCaptureError] = useState<string | null>(null);

  // Ownership of the in-flight capture lifecycle (item 2). A native
  // takePictureAsync() promise can outlive this screen -- system Back,
  // a gesture, a deep link, or any other navigation can unmount this
  // component while the promise is still pending. When it resolves
  // after that, the returned photo must be deleted, never adopted into
  // `capture` state or dispatched anywhere -- see decideCaptureOwnership().
  const stillOwnsCaptureLifecycleRef = useRef(true);
  useEffect(() => {
    stillOwnsCaptureLifecycleRef.current = true;
    return () => {
      stillOwnsCaptureLifecycleRef.current = false;
    };
  }, []);

  // Component-cleanup deletion (section 7): whenever `capture` changes
  // away from a given value -- including on unmount (back button, deep
  // link elsewhere) -- delete that photo's temp file. Safe even when
  // it was already deleted by handleRetake()/handleCancelAfterCapture()
  // or by a successful submission: deleteLocalCaptureFile() is a no-op
  // for a file that's already gone.
  useEffect(() => {
    return () => {
      if (capture) {
        deleteLocalCaptureFile(capture.uri);
      }
    };
  }, [capture]);

  const handleCameraReady = useCallback(() => {
    dispatchCameraReadiness({ type: "CAMERA_READY" });
  }, []);

  const handleCameraMountError = useCallback(() => {
    dispatchCameraReadiness({ type: "CAMERA_INVALIDATED" });
    setCaptureError(CAMERA_INIT_FAILURE_MESSAGE);
  }, []);

  const handleCapture = useCallback(async () => {
    if (!cameraRef.current) return;
    if (!canStartCapture({ cameraReady: cameraReadiness.cameraReady, capturePending })) return;

    setCapturePending(true);
    let photo: { uri: string; width: number; height: number } | undefined;
    try {
      photo = await cameraRef.current.takePictureAsync({ quality: 1 });
    } catch {
      // Never logs the underlying exception -- it could carry a file
      // URI. Safe, nontechnical copy only.
      if (stillOwnsCaptureLifecycleRef.current) {
        setCaptureError(CAPTURE_FAILURE_MESSAGE);
      }
      return;
    } finally {
      // Only touch state if this screen still owns the capture
      // lifecycle -- an update after unmount is unsafe/pointless.
      if (stillOwnsCaptureLifecycleRef.current) {
        setCapturePending(false);
      }
    }
    if (!photo) return;

    if (decideCaptureOwnership(stillOwnsCaptureLifecycleRef.current) === "DISCARD") {
      // takePictureAsync() resolved after this screen lost ownership
      // of the capture lifecycle (unmount via Back/gesture/deep link
      // while the native promise was still in flight). The photo was
      // never entered into `capture` state, so the existing [capture]
      // cleanup effect never learns it exists -- delete it directly,
      // and never dispatch CAPTURED or touch React state. Never log
      // the URI.
      await deleteLocalCaptureFile(photo.uri);
      return;
    }

    dispatchCapture({ type: "CAPTURED", uri: photo.uri, width: photo.width, height: photo.height });
    // Invalidates readiness for the CameraView that just produced this
    // photo (item 1) -- it's about to unmount in favor of the review
    // screen, and the next CameraView (after Retake) must start NOT
    // READY rather than inherit this one's stale `true`.
    dispatchCameraReadiness({ type: "CAMERA_INVALIDATED" });
    setCaptureError(null);
  }, [cameraReadiness.cameraReady, capturePending]);

  const handleRetake = useCallback(async () => {
    if (capture) {
      await deleteLocalCaptureFile(capture.uri);
    }
    dispatchCapture({ type: "DISCARDED" });
    // Defense in depth: the CameraView we're about to remount has
    // never fired onCameraReady, so readiness must already be false by
    // the time it renders (it already is, from the CAPTURED-time
    // invalidation above, but this holds even if that invariant is
    // ever weakened elsewhere).
    dispatchCameraReadiness({ type: "CAMERA_INVALIDATED" });
    submission.reset();
  }, [capture, submission]);

  const handleCancelBeforeCapture = useCallback(() => {
    // Never races an in-flight takePictureAsync() (item 2): if a
    // capture is pending, this button is also disabled in the JSX
    // below, but the handler guards independently too.
    if (capturePending) return;
    router.replace("/(app)");
  }, [capturePending, router]);

  const handleCancelAfterCapture = useCallback(async () => {
    // Never races an active POST (section 5): this button is disabled
    // whenever submission.isPending, same guard as Retake.
    if (capture) {
      await deleteLocalCaptureFile(capture.uri);
    }
    dispatchCapture({ type: "DISCARDED" });
    submission.reset();
    router.replace("/(app)");
  }, [capture, router, submission]);

  const handleSubmit = useCallback(() => {
    if (!capture) return;
    submission.mutate(
      {
        capturedImageUri: capture.uri,
        capturedImageWidth: capture.width,
        capturedImageHeight: capture.height,
        requestId: capture.requestId,
      },
      {
        onSuccess: (response) => {
          router.replace(`/(app)/analysis/${response.analysis_id}`);
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status === 403) {
            // Consent required/withdrawn/stale mid-flow (section 22) --
            // never bypassed client-side; the backend's own denial is
            // what routes this, not a local guess.
            setConsentDenied(true);
          }
        },
      },
    );
  }, [capture, router, submission]);

  if (consentDenied) {
    router.replace("/(onboarding)/consent");
    return <LoadingState label="Redirecting…" />;
  }

  if (permission.state === "unknown" || permission.state === "requesting") {
    return <LoadingState label="Checking camera access…" />;
  }

  if (permission.state !== "granted") {
    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Camera access needed</Text>
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
          This app uses your camera only to capture a photo for skin analysis. Your photo is temporarily stored in
          this app&apos;s cache for analysis. It is not added to your photo library, and the app deletes its working
          copies after submission, retake, or cancel when those lifecycle paths run.
        </Text>
        {permission.state === "blocked" ? (
          <>
            <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
              Camera access was previously denied. You can enable it from your device Settings.
            </Text>
            <Button label="Open Settings" onPress={permission.openSettings} accessibilityHint="Opens system settings" />
          </>
        ) : (
          <Button label="Allow camera access" onPress={permission.request} />
        )}
      </Screen>
    );
  }

  if (capture) {
    const errorMessage =
      submission.error instanceof ApiError && submission.error.status !== 403
        ? submission.error.message
        : submission.isError
          ? "Something went wrong. Please try again."
          : null;
    const isQuotaExceeded = submission.error instanceof ApiError && submission.error.status === 429;

    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Review your photo</Text>
        <Image source={{ uri: capture.uri }} style={styles.preview} accessibilityLabel="Captured photo preview" />

        {isQuotaExceeded ? (
          <Text style={[theme.typography.body, { color: theme.colors.danger }]} accessibilityLiveRegion="polite">
            You&apos;ve reached your current analysis limit.
          </Text>
        ) : errorMessage ? (
          <Text style={[theme.typography.body, { color: theme.colors.danger }]} accessibilityLiveRegion="polite">
            {errorMessage}
          </Text>
        ) : null}

        <Button
          label="Use this photo"
          onPress={handleSubmit}
          loading={submission.isPending}
          disabled={isQuotaExceeded}
          accessibilityHint="Submits the photo for analysis"
        />
        <Button
          label="Retake"
          variant="secondary"
          onPress={handleRetake}
          disabled={submission.isPending}
          accessibilityHint="Discards this photo and returns to the camera"
        />
        <Button
          label="Cancel"
          variant="secondary"
          onPress={handleCancelAfterCapture}
          disabled={submission.isPending}
          accessibilityHint="Discards this photo and exits skin analysis"
        />
      </Screen>
    );
  }

  return (
    <View style={styles.cameraContainer}>
      <CameraView
        ref={cameraRef}
        style={StyleSheet.absoluteFill}
        facing="front"
        // Stills only -- this app never records video, so it never
        // requests microphone permission (see app.json's
        // recordAudioAndroid: false, and camera-permission.ts's own
        // "camera access only" contract).
        mode="picture"
        onCameraReady={handleCameraReady}
        onMountError={handleCameraMountError}
      />

      {/* Static visual framing guidance -- not real-time face tracking. */}
      <View style={styles.overlay} pointerEvents="none">
        <View style={[styles.faceOval, { borderColor: theme.colors.accentForeground }]} />
        <View style={[styles.centerDot, { backgroundColor: theme.colors.accentForeground }]} />
        <Text style={[theme.typography.body, styles.overlayText]}>
          Center your face in the oval, hold steady, and use even lighting
        </Text>
        {captureError ? (
          <Text style={[theme.typography.body, styles.overlayError]} accessibilityLiveRegion="polite">
            {captureError}
          </Text>
        ) : null}
      </View>

      <View style={styles.controls}>
        <Button
          label="Capture"
          onPress={handleCapture}
          disabled={!canStartCapture({ cameraReady: cameraReadiness.cameraReady, capturePending })}
          loading={capturePending}
          accessibilityHint="Takes a photo for skin analysis"
        />
        <Button
          label="Cancel"
          variant="secondary"
          onPress={handleCancelBeforeCapture}
          disabled={capturePending}
          accessibilityHint="Exits skin analysis without taking a photo"
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  preview: { width: "100%", aspectRatio: 3 / 4, borderRadius: 16 },
  cameraContainer: { flex: 1, backgroundColor: "black" },
  overlay: { flex: 1, alignItems: "center", justifyContent: "center", gap: 16 },
  faceOval: { width: 240, height: 320, borderRadius: 160, borderWidth: 3, opacity: 0.85 },
  centerDot: { position: "absolute", width: 8, height: 8, borderRadius: 4 },
  overlayText: { color: "white", textAlign: "center", paddingHorizontal: 32, textShadowColor: "black", textShadowRadius: 4 },
  overlayError: { color: "#ffb4b4", textAlign: "center", paddingHorizontal: 32, textShadowColor: "black", textShadowRadius: 4 },
  controls: { position: "absolute", bottom: 40, left: 0, right: 0, alignItems: "center", gap: 12 },
});
