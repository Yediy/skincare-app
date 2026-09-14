/**
 * Compact design-token system. Calm, premium, beauty-focused --
 * deliberately small: a handful of scales, not a full design-system
 * project. Every screen should reach for these rather than inventing
 * a one-off color/spacing value.
 */

export const spacing = {
  xs: 4,
  sm: 8,
  md: 16,
  lg: 24,
  xl: 32,
  xxl: 48,
} as const;

export const radii = {
  sm: 8,
  md: 14,
  lg: 20,
  pill: 999,
} as const;

export const typography = {
  displayLarge: { fontSize: 32, lineHeight: 40, fontWeight: "600" as const },
  title: { fontSize: 22, lineHeight: 28, fontWeight: "600" as const },
  subtitle: { fontSize: 17, lineHeight: 24, fontWeight: "500" as const },
  body: { fontSize: 15, lineHeight: 22, fontWeight: "400" as const },
  caption: { fontSize: 13, lineHeight: 18, fontWeight: "400" as const },
  label: { fontSize: 13, lineHeight: 16, fontWeight: "600" as const },
};

export type ColorTokens = {
  background: string;
  surface: string;
  surfaceMuted: string;
  foreground: string;
  muted: string;
  border: string;
  accent: string;
  accentForeground: string;
  danger: string;
  dangerForeground: string;
  success: string;
};

export const lightColors: ColorTokens = {
  background: "#FBF9F7",
  surface: "#FFFFFF",
  surfaceMuted: "#F3EEEA",
  foreground: "#241F1C",
  muted: "#7A716B",
  border: "#E7E0DA",
  accent: "#A9714A",
  accentForeground: "#FFFFFF",
  danger: "#B3413B",
  dangerForeground: "#FFFFFF",
  success: "#3E7A57",
};

export const darkColors: ColorTokens = {
  background: "#17140F",
  surface: "#221D18",
  surfaceMuted: "#2B241E",
  foreground: "#F3ECE4",
  muted: "#B2A79D",
  border: "#3A322A",
  accent: "#D9A876",
  accentForeground: "#1B140C",
  danger: "#E08A85",
  dangerForeground: "#2A0E0C",
  success: "#8FCB9E",
};
