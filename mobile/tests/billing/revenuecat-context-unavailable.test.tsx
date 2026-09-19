/**
 * src/billing/revenuecat-context.tsx -- when availability isn't READY
 * (web, disabled, or missing key), the provider must never configure
 * RevenueCat or call any adapter method, even for an authenticated
 * user. `computeAvailability()` itself is mocked directly (its own
 * pure-function correctness is tested separately in
 * compute-availability.test.ts) so this file only has to prove the
 * PROVIDER respects whatever it returns.
 */
jest.mock("@/billing/revenuecat-availability", () => ({
  computeAvailability: () => "WEB_UNSUPPORTED",
}));

import { act, render } from "@testing-library/react-native";
import React from "react";
import { Text } from "react-native";

import { createFakeRevenueCatAdapter } from "./fake-adapter";

const mockMe = jest.fn();
jest.mock("@/api/auth-api", () => ({
  me: () => mockMe(),
}));

type SessionStatus = "UNKNOWN" | "SIGNED_OUT" | "AUTHENTICATED";
let setSessionStatusExternally: (status: SessionStatus) => void = () => {};
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

function Probe() {
  const rc = useRevenueCat();
  return <Text testID="availability">{rc.availability}</Text>;
}

beforeEach(() => {
  __resetRevenueCatSingletonForTests();
  mockMe.mockReset();
  mockMe.mockResolvedValue({ user_id: "11111111-1111-1111-1111-111111111111" });
});

describe("RevenueCatProvider when unavailable (e.g. web, or missing config)", () => {
  it("never configures, never calls /me, and every adapter action is a no-op", async () => {
    const { adapter, state } = createFakeRevenueCatAdapter();
    const { getByTestId } = await render(
      <SessionProvider>
        <RevenueCatProvider adapter={adapter}>
          <Probe />
        </RevenueCatProvider>
      </SessionProvider>,
    );

    await act(() => {
      setSessionStatusExternally("AUTHENTICATED");
    });

    expect(getByTestId("availability").props.children).toBe("WEB_UNSUPPORTED");
    expect(state.configureCalls).toHaveLength(0);
    expect(mockMe).not.toHaveBeenCalled();
    expect(state.presentPaywallCalls).toBe(0);
    expect(state.restoreCalls).toBe(0);
    expect(state.presentCustomerCenterCalls).toBe(0);
  });
});
