// Global test setup. Deliberately minimal -- most modules under test
// take their dependencies (SecureStoreAdapter, fetch, etc.) as plain
// function arguments, so there is little to wire up globally.

process.env.EXPO_PUBLIC_API_BASE_URL = "https://test.invalid";
