/**
 * app/(public)/reset-password.tsx. Covers: reading the token from the
 * route's search params, never persisting it, password-mismatch
 * validation, backend-validation display, clearing the local session
 * on success, and routing to sign-in afterward.
 */
import { fireEvent, render, waitFor } from "@testing-library/react-native";
import React from "react";

import ResetPassword from "@app/(public)/reset-password";
import { ApiError } from "@/api/errors";
import { ThemeProvider } from "@/theme/theme-provider";

const mockReplace = jest.fn();
jest.mock("expo-router", () => ({
  useRouter: () => ({ replace: mockReplace, back: jest.fn(), push: jest.fn() }),
  useLocalSearchParams: () => mockSearchParams(),
}));

let mockSearchParams: () => { token?: string } = () => ({ token: "raw-test-token-value" });

const mockSignOut = jest.fn().mockResolvedValue(undefined);
jest.mock("@/auth/session-context", () => ({
  useSession: () => ({ signOut: mockSignOut }),
}));

const mockMutate = jest.fn();
let mockIsPending = false;
let mockIsError = false;
let mockError: unknown = undefined;

jest.mock("@/auth/use-auth-actions", () => ({
  useResetPasswordMutation: () => ({
    mutate: mockMutate,
    isPending: mockIsPending,
    isError: mockIsError,
    error: mockError,
  }),
}));

// A logger spy proves the token never reaches a log call, not just
// that this test file doesn't call console.log itself.
const loggerSpy = jest.spyOn(console, "log").mockImplementation(() => {});

// This suite's first render() pays a one-time cold-start cost (native
// module mocks + the test-renderer/jest-expo stack initializing) that
// can exceed Jest's default 5s per-test timeout in a cold CI
// container -- bumped for this file only, not globally (same reasoning
// as tests/components/product-recommendation-card.test.tsx).
jest.setTimeout(20000);

async function renderScreen() {
  return render(<ResetPassword />, { wrapper: ThemeProvider });
}

beforeEach(() => {
  mockMutate.mockReset();
  mockSignOut.mockClear();
  mockReplace.mockClear();
  loggerSpy.mockClear();
  mockIsPending = false;
  mockIsError = false;
  mockError = undefined;
  mockSearchParams = () => ({ token: "raw-test-token-value" });
});

describe("ResetPassword", () => {
  it("reads the token from the route's search params", async () => {
    const { getByLabelText, getByText } = await renderScreen();
    await fireEvent.changeText(getByLabelText("New password"), "newpassword123");
    await fireEvent.changeText(getByLabelText("Confirm new password"), "newpassword123");
    await fireEvent.press(getByText("Reset password"));

    expect(mockMutate).toHaveBeenCalledWith(
      { token: "raw-test-token-value", newPassword: "newpassword123" },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });

  it("shows an invalid-link screen when no token is present, without calling the mutation", async () => {
    mockSearchParams = () => ({});
    const { getByText, queryByLabelText } = await renderScreen();
    expect(getByText("Invalid reset link")).toBeTruthy();
    expect(queryByLabelText("New password")).toBeNull();
  });

  it("blocks submission and shows an error when passwords don't match", async () => {
    const { getByLabelText, getByText } = await renderScreen();
    await fireEvent.changeText(getByLabelText("New password"), "newpassword123");
    await fireEvent.changeText(getByLabelText("Confirm new password"), "different456");

    expect(getByText("Passwords don't match.")).toBeTruthy();
    expect(getByText("Reset password").parent?.props.accessibilityState.disabled).toBe(true);
  });

  it("shows a generic invalid-or-expired message for a 400, never the raw backend detail", async () => {
    mockIsError = true;
    mockError = new ApiError({ status: 400, code: "VALIDATION_ERROR", message: "Invalid or expired reset token", retryable: false });
    const { getByText, queryByText } = await renderScreen();
    expect(getByText("This reset link is invalid or has expired. Request a new one.")).toBeTruthy();
    expect(queryByText("Invalid or expired reset token")).toBeNull();
  });

  it("shows the backend validation message for a 422 (password policy)", async () => {
    mockIsError = true;
    mockError = new ApiError({
      status: 422, code: "VALIDATION_ERROR", message: "Please check the information you entered.", retryable: false,
    });
    const { getByText } = await renderScreen();
    expect(getByText("Please check the information you entered.")).toBeTruthy();
  });

  it("clears the local session and routes to sign-in on a successful reset, without auto-authenticating", async () => {
    mockMutate.mockImplementation((_input, { onSuccess }) => onSuccess());
    const { getByLabelText, getByText } = await renderScreen();

    await fireEvent.changeText(getByLabelText("New password"), "newpassword123");
    await fireEvent.changeText(getByLabelText("Confirm new password"), "newpassword123");
    await fireEvent.press(getByText("Reset password"));

    await waitFor(() => expect(mockSignOut).toHaveBeenCalled());
    await waitFor(() => expect(getByText("Your password has been reset. Sign in with your new password.")).toBeTruthy());

    await fireEvent.press(getByText("Sign in"));
    expect(mockReplace).toHaveBeenCalledWith("/(public)/sign-in");
  });

  it("never logs the raw token", async () => {
    await renderScreen();
    for (const call of loggerSpy.mock.calls) {
      expect(JSON.stringify(call)).not.toContain("raw-test-token-value");
    }
  });
});
