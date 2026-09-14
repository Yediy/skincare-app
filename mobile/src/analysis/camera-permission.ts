import { useCallback, useState } from "react";
import { Linking } from "react-native";
import { useCameraPermissions } from "expo-camera";

export type CameraPermissionState = "unknown" | "requesting" | "granted" | "denied" | "blocked";

/** The subset of expo-camera's PermissionResponse this module actually
 * reasons about -- a local, minimal type so the pure decision function
 * below needs no runtime dependency on expo-camera at all (trivially
 * unit-testable with a plain object literal). */
export type PermissionResponseLike = {
  granted: boolean;
  canAskAgain: boolean;
};

/**
 * Camera access is never implicit (section 3): this is the one place
 * a raw expo-camera PermissionResponse is translated into the five
 * states this app actually branches on. `null` (permission not yet
 * queried) and an in-flight `requestPermission()` call are both real,
 * distinct states from "the user was asked and said no" --
 * "unknown"/"requesting" render a loading state, "denied" offers a
 * retry, "blocked" (denied AND the OS says asking again would be a
 * no-op) offers Settings instead of a request that can only silently
 * fail.
 */
export function resolveCameraPermissionState(
  response: PermissionResponseLike | null,
  isRequesting: boolean,
): CameraPermissionState {
  if (isRequesting) return "requesting";
  if (response === null) return "unknown";
  if (response.granted) return "granted";
  return response.canAskAgain ? "denied" : "blocked";
}

export type UseCameraPermissionResult = {
  state: CameraPermissionState;
  /** Triggers the OS permission prompt (a no-op, resolving to the
   * still-denied state, if the OS has already decided asking again
   * would be silently ignored -- "blocked" callers should use
   * openSettings() instead). */
  request: () => Promise<void>;
  /** Deep-links to this app's OS Settings page -- the only recovery
   * path once a permission is "blocked". */
  openSettings: () => void;
};

/** Requests ONLY camera access -- never microphone, contacts,
 * location, or Bluetooth (section 3: no unrelated permissions). */
export function useCameraPermissionState(): UseCameraPermissionResult {
  const [permission, requestPermission] = useCameraPermissions();
  const [isRequesting, setIsRequesting] = useState(false);

  const request = useCallback(async () => {
    setIsRequesting(true);
    try {
      await requestPermission();
    } finally {
      setIsRequesting(false);
    }
  }, [requestPermission]);

  const openSettings = useCallback(() => {
    Linking.openSettings();
  }, []);

  return {
    state: resolveCameraPermissionState(permission, isRequesting),
    request,
    openSettings,
  };
}
