import { logger } from "@/utils/logger";

describe("logger redaction", () => {
  let errorSpy: jest.SpyInstance;

  beforeEach(() => {
    errorSpy = jest.spyOn(console, "error").mockImplementation(() => undefined);
  });

  afterEach(() => {
    errorSpy.mockRestore();
  });

  it("redacts access/refresh tokens and Authorization headers", () => {
    logger.error("auth failure", {
      access_token: "super-secret-access",
      refresh_token: "super-secret-refresh",
      Authorization: "Bearer super-secret-access",
    });

    const [, loggedObject] = errorSpy.mock.calls[0];
    expect(JSON.stringify(loggedObject)).not.toContain("super-secret");
    expect(loggedObject.access_token).toBe("[redacted]");
    expect(loggedObject.refresh_token).toBe("[redacted]");
    expect(loggedObject.Authorization).toBe("[redacted]");
  });

  it("redacts password, allergy, and pregnancy/nursing fields even nested", () => {
    logger.error("profile debug", {
      user: { password: "hunter2", allergies: ["nuts"], is_pregnant: true, is_nursing: false },
    });

    const [, loggedObject] = errorSpy.mock.calls[0];
    expect(loggedObject.user.password).toBe("[redacted]");
    expect(loggedObject.user.allergies).toBe("[redacted]");
    expect(loggedObject.user.is_pregnant).toBe("[redacted]");
    expect(loggedObject.user.is_nursing).toBe("[redacted]");
  });

  it("leaves non-sensitive fields untouched", () => {
    logger.error("info", { experience_level: "beginner", count: 3 });

    const [, loggedObject] = errorSpy.mock.calls[0];
    expect(loggedObject).toEqual({ experience_level: "beginner", count: 3 });
  });
});
