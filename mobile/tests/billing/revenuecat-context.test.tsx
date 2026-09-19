/**
 * src/billing/revenuecat-context.tsx -- the identified-user lifecycle
 * (Mobile C2 Parts 7-9). No real react-native-purchases/-ui call
 * anywhere in this file: RevenueCatProvider is given a fake adapter
 * (tests/billing/fake-adapter.ts) directly, and the session/`/me`
 * layer is mocked with a controllable in-memory React context so
 * tests can drive UNKNOWN -> SIGNED_OUT -> AUTHENTICATED transitions
 * deterministically via `act()`, with zero network/native calls.
 *
 * Availability-state tests (DISABLED/WEB_UNSUPPORTED/MISSING_KEY) live
 * in their own files (revenuecat-context-disabled.test.tsx etc.) --
 * `jest.mock("@/constants/config", ...)` below overrides just the
 * RevenueCat constants to "enabled, ready" for this file (plain
 * `process.env.X = ...` statements do NOT work here: Babel's ESM->CJS
 * transform hoists every `import` to the top of the file regardless
 * of where it appears in source, so config.ts would already have been
 * evaluated, with its constants cached, before any such assignment
 * runs -- `jest.mock()` calls, unlike plain statements, ARE specially
 * hoisted by babel-plugin-jest-hoist, which is why this works).
 */
import { act, fireEvent, render, waitFor } from "@testing-library/react-native";
import React, { useEffect } from "react";
import { Text } from "react-native";

jest.mock("@/constants/config", () => ({
  ...jest.requireActual("@/constants/config"),
  REVENUECAT_ENABLED: true,
  REVENUECAT_IOS_API_KEY: "test-only-ios-key-never-a-real-revenuecat-key",
  REVENUECAT_ANDROID_API_KEY: "test-only-android-key-never-a-real-revenuecat-key",
  REVENUECAT_ENTITLEMENT_ID: "premium",
}));

import { createFakeRevenueCatAdapter } from "./fake-adapter";

const mockMe = jest.fn();
jest.mock("@/api/auth-api", () => ({
  me: () => mockMe(),
}));

type SessionStatus = "UNKNOWN" | "SIGNED_OUT" | "AUTHENTICATED";
let setSessionStatusExternally: (status: SessionStatus) => void = () => {
  throw new Error("SessionProvider not mounted yet");
};

jest.mock("@/auth/session-context", () => {
  const ReactLib = require("react");
  const Ctx = ReactLib.createContext({ status: "UNKNOWN" as SessionStatus });
  return {
    useSession: () => ({ state: ReactLib.useContext(Ctx) }),
    SessionProvider: ({ children }: { children: React.ReactNode }) => {
      const [status, setStatus] = ReactLib.useState("UNKNOWN" as SessionStatus);
      setSessionStatusExternally = setStatus;
      return ReactLib.createElement(Ctx.Provider, { value: { status } }, children);
    },
  };
});

// eslint-disable-next-line import/first
import { SessionProvider } from "@/auth/session-context";
// eslint-disable-next-line import/first
import { __resetRevenueCatSingletonForTests, RevenueCatProvider, useRevenueCat } from "@/billing/revenuecat-context";

let capturedContext: ReturnType<typeof useRevenueCat> | null = null;

function Probe() {
  const rc = useRevenueCat();
  useEffect(() => {
    capturedContext = rc;
  });
  return (
    <>
      <Text testID="availability">{rc.availability}</Text>
      <Text testID="configuredUserId">{rc.configuredUserId ?? "none"}</Text>
      <Text testID="retry" onPress={() => rc.retryIdentification()}>
        retry
      </Text>
    </>
  );
}

async function renderWithProviders(adapter: ReturnType<typeof createFakeRevenueCatAdapter>["adapter"]) {
  return render(
    <SessionProvider>
      <RevenueCatProvider adapter={adapter}>
        <Probe />
      </RevenueCatProvider>
    </SessionProvider>,
  );
}

beforeEach(() => {
  __resetRevenueCatSingletonForTests();
  mockMe.mockReset();
  capturedContext = null;
});

describe("RevenueCatProvider", () => {
  it("does nothing while session status is UNKNOWN", async () => {
    const { adapter, state } = createFakeRevenueCatAdapter();
    await renderWithProviders(adapter);

    expect(state.configureCalls).toHaveLength(0);
    expect(mockMe).not.toHaveBeenCalled();
  });

  it("never configures RevenueCat while signed out", async () => {
    const { adapter, state } = createFakeRevenueCatAdapter();
    await renderWithProviders(adapter);

    await act(() => {
      setSessionStatusExternally("SIGNED_OUT");
    });

    expect(state.configureCalls).toHaveLength(0);
    expect(mockMe).not.toHaveBeenCalled();
  });

  it("waits for /me before configuring, using the backend UUID as the App User ID", async () => {
    const { adapter, state } = createFakeRevenueCatAdapter();
    mockMe.mockResolvedValue({ user_id: "11111111-1111-1111-1111-111111111111" });
    const { getByTestId } = await renderWithProviders(adapter);

    await act(() => {
      setSessionStatusExternally("AUTHENTICATED");
    });

    await waitFor(() => {
      expect(getByTestId("configuredUserId").props.children).toBe("11111111-1111-1111-1111-111111111111");
    });
    expect(state.configureCalls).toEqual([
      { apiKey: expect.any(String), appUserID: "11111111-1111-1111-1111-111111111111" },
    ]);
  });

  it("never uses email or any other non-UUID string as the App User ID", async () => {
    const { adapter, state } = createFakeRevenueCatAdapter();
    mockMe.mockResolvedValue({ user_id: "22222222-2222-2222-2222-222222222222" });
    await renderWithProviders(adapter);

    await act(() => {
      setSessionStatusExternally("AUTHENTICATED");
    });

    await waitFor(() => expect(state.configureCalls).toHaveLength(1));
    expect(state.configureCalls[0].appUserID).not.toMatch(/@/);
    expect(state.configureCalls[0].appUserID).toBe("22222222-2222-2222-2222-222222222222");
  });

  it("configures only once for repeated renders of the same authenticated user", async () => {
    const { adapter, state } = createFakeRevenueCatAdapter();
    mockMe.mockResolvedValue({ user_id: "33333333-3333-3333-3333-333333333333" });
    const { rerender } = await renderWithProviders(adapter);

    await act(() => {
      setSessionStatusExternally("AUTHENTICATED");
    });
    await waitFor(() => expect(state.configureCalls).toHaveLength(1));

    // Force a re-render of the same authenticated user -- must not
    // configure or log in again.
    await rerender(
      <SessionProvider>
        <RevenueCatProvider adapter={adapter}>
          <Probe />
        </RevenueCatProvider>
      </SessionProvider>,
    );

    expect(state.configureCalls).toHaveLength(1);
    expect(state.logInCalls).toHaveLength(0);
  });

  it("account switching: sign-out then a different user signs in uses logIn, never logOut, never an anonymous ID", async () => {
    const { adapter, state } = createFakeRevenueCatAdapter();
    mockMe.mockResolvedValueOnce({ user_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" });
    const { getByTestId } = await renderWithProviders(adapter);

    await act(() => {
      setSessionStatusExternally("AUTHENTICATED");
    });
    await waitFor(() => expect(state.configureCalls).toHaveLength(1));
    expect(state.configureCalls[0].appUserID).toBe("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");

    // Application sign-out -- the fake adapter's interface has no
    // logOut() method AT ALL (matching the real RevenueCatAdapter
    // interface -- see its own docstring), so there is nothing for
    // sign-out to even call; this assertion proves no adapter call of
    // any kind happens during the transition itself.
    const callsBeforeSignOut = state.configureCalls.length + state.logInCalls.length;
    await act(() => {
      setSessionStatusExternally("SIGNED_OUT");
    });
    // Clearing local UI state is deferred to a microtask (see the
    // provider's own comment on why) -- wait for it rather than
    // asserting synchronously.
    await waitFor(() => {
      expect(getByTestId("configuredUserId").props.children).toBe("none");
    });
    expect(state.configureCalls.length + state.logInCalls.length).toBe(callsBeforeSignOut);

    // A different user signs in.
    mockMe.mockResolvedValueOnce({ user_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" });
    await act(() => {
      setSessionStatusExternally("AUTHENTICATED");
    });

    await waitFor(() => {
      expect(getByTestId("configuredUserId").props.children).toBe("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");
    });
    expect(state.logInCalls).toEqual(["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]);
    // configure() was called exactly once, ever (for user A) -- user
    // B's transition used logIn, never a second configure().
    expect(state.configureCalls).toHaveLength(1);
  });

  it("independent-review Blocker 1: a failed account switch fails closed -- never exposes the previous user's id to the new session, never logs out, and a successful retry recovers", async () => {
    const { adapter, state } = createFakeRevenueCatAdapter();
    mockMe.mockResolvedValueOnce({ user_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" });
    const { getByTestId } = await renderWithProviders(adapter);

    // User A configures successfully.
    await act(() => {
      setSessionStatusExternally("AUTHENTICATED");
    });
    await waitFor(() => expect(getByTestId("configuredUserId").props.children).toBe(
      "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    ));
    expect(state.configureCalls).toHaveLength(1);

    // Sign out.
    await act(() => {
      setSessionStatusExternally("SIGNED_OUT");
    });
    await waitFor(() => expect(getByTestId("configuredUserId").props.children).toBe("none"));

    // User B signs in, but the SDK's logIn(B) throws.
    state.logInError = new Error("network error during logIn");
    mockMe.mockResolvedValueOnce({ user_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" });
    await act(() => {
      setSessionStatusExternally("AUTHENTICATED");
    });

    await waitFor(() => expect(state.logInCalls).toEqual(["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]));
    // Fail-closed: configuredUserId must be null, NEVER user A's id,
    // even though the SDK's own last-successful user is still A.
    expect(getByTestId("configuredUserId").props.children).toBe("none");
    // No logOut-equivalent call exists on this adapter at all (see
    // its own docstring) -- nothing to assert beyond configureCalls
    // staying at exactly the one call ever made, for user A.
    expect(state.configureCalls).toHaveLength(1);

    // While fail-closed, no purchase/restore/Customer-Center action
    // for B can execute.
    await act(async () => {
      await capturedContext!.presentPaywall();
      await capturedContext!.restorePurchases();
      await capturedContext!.presentCustomerCenter();
    });
    expect(state.presentPaywallCalls).toBe(0);
    expect(state.restoreCalls).toBe(0);
    expect(state.presentCustomerCenterCalls).toBe(0);

    // Retry succeeds this time.
    state.logInError = null;
    mockMe.mockResolvedValueOnce({ user_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" });
    await act(() => {
      fireEvent.press(getByTestId("retry"));
    });

    await waitFor(() => expect(getByTestId("configuredUserId").props.children).toBe(
      "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ));
    expect(state.logInCalls).toEqual([
      "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]);

    // B is now configured -- actions become available.
    await act(async () => {
      await capturedContext!.presentPaywall();
    });
    expect(state.presentPaywallCalls).toBe(1);
  });
});
