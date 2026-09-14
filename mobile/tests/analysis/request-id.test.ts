import { randomUUID } from "expo-crypto";

import { generateAnalysisRequestId } from "@/analysis/request-id";

jest.mock("expo-crypto", () => ({ randomUUID: jest.fn() }));

const mockRandomUUID = randomUUID as jest.Mock;

describe("generateAnalysisRequestId", () => {
  it("delegates to a cryptographically strong random UUID generator", () => {
    mockRandomUUID.mockReturnValue("11111111-1111-1111-1111-111111111111");

    expect(generateAnalysisRequestId()).toBe("11111111-1111-1111-1111-111111111111");
    expect(mockRandomUUID).toHaveBeenCalledTimes(1);
  });

  it("produces a different id on each call (a new deliberate analysis each time)", () => {
    mockRandomUUID.mockReturnValueOnce("id-1").mockReturnValueOnce("id-2");

    expect(generateAnalysisRequestId()).toBe("id-1");
    expect(generateAnalysisRequestId()).toBe("id-2");
  });
});
