import React, { createContext, useContext, useMemo } from "react";
import { useColorScheme } from "react-native";

import { darkColors, lightColors, radii, spacing, typography, type ColorTokens } from "./tokens";

type Theme = {
  colors: ColorTokens;
  spacing: typeof spacing;
  radii: typeof radii;
  typography: typeof typography;
  scheme: "light" | "dark";
};

const ThemeContext = createContext<Theme | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const value = useMemo<Theme>(
    () => ({
      colors: scheme === "dark" ? darkColors : lightColors,
      spacing,
      radii,
      typography,
      scheme,
    }),
    [scheme],
  );
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): Theme {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    throw new Error("useTheme() must be used within a ThemeProvider");
  }
  return ctx;
}
