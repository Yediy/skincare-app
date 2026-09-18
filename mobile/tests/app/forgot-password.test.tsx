/**
 * app/(public)/forgot-password.tsx -- account-enumeration-safe by
 * construction (see the screen's own docstring). These tests mock the
 * mutation hook, not fetch/network -- the enumeration-safety guarantee
 * itself is a backend property (see backend/tests/auth/
 * test_password_reset.py); this suite only proves the screen renders
 * one identical confirmation regardless of what the mutation resolved
 * with, never a distinguishing branch.
 */
import { fireEvent, render, waitFor } from "@testing-library/react-native";
import React from "react";

import ForgotPassword from "@app/(public)/forgot-password";
import { ApiError } from "@/api/errors";
import { ThemeProvider } from "@/theme/theme-provider";

const mockReplace = jest.fn();
const mockBack = jest.fn();
jest.mock("expo-router", () => ({
  useRouter: () => ({ replace: mockReplace, back: mockBack, push: jest.fn() }),
}));

const mockMutate = jest.fn();
let mockIsPending = false;
let mockIsError = false;
let mockError: unknown = undefined;

jest.mock("@/auth/use-auth-actions", () => ({
  useForgotPasswordMutation: () => ({
    mutate: mockMutate,
    isPending: mockIsPending,
    isError: mockIsError,
    error: mockError,
  }),
}));

async function renderScreen() {
  return render(<ForgotPassword />, { wrapper: ThemeProvider });
}

beforeEach(() => {
  mockMutate.mockReset();
  mockIsPending = false;
  mockIsError = false;
  mockError = undefined;
});

describe("ForgotPassword", () => {
  it("submits the entered email", async () => {
    const { getByLabelText, getByText } = await renderScreen();
    await fireEvent.changeText(getByLabelText("Email"), "someone@test.com");
    await fireEvent.press(getByText("Send reset instructions"));

    expect(mockMutate).toHaveBeenCalledWith(
      { email: "someone@test.com" },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });

  it("shows the same generic confirmation regardless of what the backend actually did", async () => {
    mockMutate.mockImplementation((_input, { onSuccess }) => onSuccess());
    const { getByLabelText, getByText, queryByText } = await renderScreen();

    await fireEvent.changeText(getByLabelText("Email"), "anything@test.com");
    await fireEvent.press(getByText("Send reset instructions"));

    await waitFor(() => expect(getByText("Check your email")).toBeTruthy());
    expect(
      queryByText("If an eligible account exists for that email, password reset instructions will be sent."),
    ).toBeTruthy();
    // No account-existence-specific text is ever rendered.
    expect(queryByText(/not found/i)).toBeNull();
    expect(queryByText(/already/i)).toBeNull();
  });

  it("shows a generic error for a genuine failure (not an enumeration signal)", async () => {
    mockIsError = true;
    mockError = new ApiError({
      status: 429,
      code: "RATE_LIMITED",
      message: "Too many attempts. Please wait a moment and try again.",
      retryable: true,
    });
    const { getByText } = await renderScreen();
    expect(getByText("Too many attempts. Please wait a moment and try again.")).toBeTruthy();
  });

  it("disables submission until an email is entered", async () => {
    const { getByText } = await renderScreen();
    expect(getByText("Send reset instructions").parent?.props.accessibilityState.disabled).toBe(true);
  });
});
