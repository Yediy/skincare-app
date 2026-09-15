import { CameraView } from "expo-camera";
import { useRouter } from "expo-router";
import React, { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { Image, StyleSheet, Text, View } from "react-native";

import { ApiError } from "@/api/errors";
import { captureAttemptReducer, type CaptureAttempt, type CaptureAttemptAction } from "@/analysis/capture-attempt";
import { CAMERA_INIT_FAILURE_MESSAGE, CAPTURE_FAILURE_MESSAGE, canStartCapture } from "@/analysis/camera-capture-policy";
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

  // Real-device camera readiness/concurrency state (section 4): the
  // capture button is disabled until the camera itself reports ready,
  // and capturePending blocks a second concurrent takePictureAsync()
  // call from a rapid repeated tap.
  const [cameraReady, setCameraReady] = useState(false);
  const [capturePending, setCapturePending] = useState(false);
  const [captureError, setCaptureError] = useState<string | null>(null);

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
    setCameraReady(true);
  }, []);

  const handleCameraMountError = useCallback(() => {
    setCameraReady(false);
    setCaptureError(CAMERA_INIT_FAILURE_MESSAGE);
  }, []);

  const handleCapture = useCallback(async () => {
    if (!cameraRef.current) return;
    if (!canStartCapture({ cameraReady, capturePending })) return;

    setCapturePending(true);
    try {
      const photo = await cameraRef.current.takePictureAsync({ quality: 1 });
      if (photo) {
        dispatchCapture({ type: "CAPTURED", uri: photo.uri, width: photo.width, height: photo.height });
        setCaptureError(null);
      }
    } catch {
      // Never logs the underlying exception -- it could carry a file
      // URI. Safe, nontechnical copy only.
      setCaptureError(CAPTURE_FAILURE_MESSAGE);
    } finally {
      setCapturePending(false);
    }
  }, [cameraReady, capturePending]);

  const handleRetake = useCallback(async () => {
    if (capture) {
      await deleteLocalCaptureFile(capture.uri);
    }
    dispatchCapture({ type: "DISCARDED" });
    submission.reset();
  }, [capture, submission]);

  const handleCancelBeforeCapture = useCallback(() => {
    router.replace("/(app)");
  }, [router]);

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
          disabled={!canStartCapture({ cameraReady, capturePending })}
          loading={capturePending}
          accessibilityHint="Takes a photo for skin analysis"
        />
        <Button
          label="Cancel"
          variant="secondary"
          onPress={handleCancelBeforeCapture}
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
