// Global test setup. Deliberately minimal -- most modules under test
// take their dependencies (SecureStoreAdapter, fetch, etc.) as plain
// function arguments, so there is little to wire up globally.

process.env.EXPO_PUBLIC_API_BASE_URL = "https://test.invalid";

// jest-expo installs `global.fetch` as a lazy, self-replacing getter
// (expo/src/winter/installGlobal.ts) that resolves the real Expo
// fetch polyfill on first access. When that first access happens to
// land near a test FILE's own teardown (observed here as a
// nondeterministic, worker-pool-scheduling-dependent race across
// multiple files run together -- never reproduced running any single
// file alone), the resolution's own internal logging can fire after
// that file's console buffer has already closed, which Jest treats
// as a hard failure ("Cannot log after tests are done", forcing
// `process.exitCode = 1` even with zero failing assertions -- see
// jest-runner's own runTest.js). Touching the getter once here,
// synchronously, at the very start of every test file's setup (this
// file runs once per file, before any of that file's own code),
// forces the resolution to complete deterministically up front
// instead of racing against an arbitrary later point.
void globalThis.fetch;
