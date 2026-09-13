const expoConfig = require("eslint-config-expo/flat");

module.exports = [
  ...expoConfig,
  {
    ignores: ["dist/*", "node_modules/*", ".expo/*"],
  },
  {
    rules: {
      "no-console": ["warn", { allow: ["warn", "error"] }],
    },
  },
];
