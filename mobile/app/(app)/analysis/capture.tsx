import { CameraView } from "expo-camera";
import { useRouter } from "expo-router";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Image, StyleSheet, Text, View } from "react-native";

import { ApiError } from "@/api/errors";
import { useAnalysisSession } from "@/analysis/analysis-session";
import { useCameraPermissionState } from "@/analysis/camera-permission";
import { deleteLocalCaptureFile } from "@/analysis/capture-file";
import { useSubmitAnalysis } from "@/analysis/use-submit-analysis";
import { Button } from "@/components/button";
import { LoadingState } from "@/components/loading-state";
import { Screen } from "@/components/screen";
import { useTheme } from "@/theme/theme-provider";

/**
 * Controlled facial capture (sections 5/6/7). Local guidance only --
 * the oval/center-dot overlay is static visual framing, not real-time
 * face-geometry tracking (this pass does not add an on-device ML
 * pipeline to back it -- see the module docstring in
 * src/analysis/camera-permission.ts and capture-file.ts for what
 * mobile actually validates vs. what stays the backend's job).
 */
export default function CaptureScreen() {
  const theme = useTheme();
  const router = useRouter();
  const cameraRef = useRef<CameraView>(null);
  const permission = useCameraPermissionState();
  const { requestId } = useAnalysisSession();
  const submission = useSubmitAnalysis();

  const [capturedUri, setCapturedUri] = useState<string | null>(null);
  const [consentDenied, setConsentDenied] = useState(false);

  // Component-cleanup deletion (section 7): whenever `capturedUri`
  // changes away from a given value -- including on unmount (back
  // button, deep link elsewhere) -- delete that photo's temp file.
  // Safe even when it was already deleted by handleRetake() or by a
  // successful submitCapture(): deleteLocalCaptureFile() is a no-op
  // for a file that's already gone.
  useEffect(() => {
    return () => {
      if (capturedUri) {
        deleteLocalCaptureFile(capturedUri);
      }
    };
  }, [capturedUri]);

  const handleCapture = useCallback(async () => {
    if (!cameraRef.current) return;
    const photo = await cameraRef.current.takePictureAsync({ quality: 1 });
    if (photo) {
      setCapturedUri(photo.uri);
    }
  }, []);

  const handleRetake = useCallback(async () => {
    if (capturedUri) {
      await deleteLocalCaptureFile(capturedUri);
    }
    setCapturedUri(null);
    submission.reset();
  }, [capturedUri, submission]);

  const handleSubmit = useCallback(() => {
    if (!capturedUri) return;
    submission.mutate(
      { capturedImageUri: capturedUri, requestId },
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
  }, [capturedUri, requestId, router, submission]);

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
          This app uses your camera only to capture a photo for skin analysis. Photos are processed temporarily
          and are never saved to your device or shared without your action.
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

  if (capturedUri) {
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
        <Image source={{ uri: capturedUri }} style={styles.preview} accessibilityLabel="Captured photo preview" />

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
      </Screen>
    );
  }

  return (
    <View style={styles.cameraContainer}>
      <CameraView ref={cameraRef} style={StyleSheet.absoluteFill} facing="front" />

      {/* Static visual framing guidance -- not real-time face tracking. */}
      <View style={styles.overlay} pointerEvents="none">
        <View style={[styles.faceOval, { borderColor: theme.colors.accentForeground }]} />
        <View style={[styles.centerDot, { backgroundColor: theme.colors.accentForeground }]} />
        <Text style={[theme.typography.body, styles.overlayText]}>
          Center your face in the oval, hold steady, and use even lighting
        </Text>
      </View>

      <View style={styles.controls}>
        <Button label="Capture" onPress={handleCapture} accessibilityHint="Takes a photo for skin analysis" />
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
  controls: { position: "absolute", bottom: 40, left: 0, right: 0, alignItems: "center" },
});
