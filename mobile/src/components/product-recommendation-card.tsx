import React from "react";
import { StyleSheet, Text, View } from "react-native";

import { presentProductRecommendation } from "@/analysis/product-recommendation-presenter";
import { useTheme } from "@/theme/theme-provider";
import type { ProductRecommendation } from "@/types/domain";

/**
 * Mobile V1 Phase C1: a purely presentational card for one concrete
 * product match. It owns none of: API fetching, safety decisions,
 * ranking, or navigation -- every piece of copy it renders comes from
 * presentProductRecommendation() (src/analysis/product-recommendation-
 * presenter.ts), which itself only reshapes what the backend already
 * decided. This component never has an interactive element in Phase
 * C1 (no purchase/shopping links exist yet -- see this phase's own
 * scope boundary), so touch-target sizing does not apply.
 *
 * Accessibility: rendered as one accessibility element in reading
 * order (brand/product -> compatibility status -> considerations ->
 * verification), with the compatibility status always carried as
 * text, never color alone.
 */
export function ProductRecommendationCard({ recommendation }: { recommendation: ProductRecommendation }) {
  const theme = useTheme();
  const presentation = presentProductRecommendation(recommendation);

  const statusColor =
    presentation.safetyStatus.tone === "neutral"
      ? theme.colors.success
      : presentation.safetyStatus.tone === "caution"
        ? theme.colors.accent
        : theme.colors.danger;

  return (
    <View
      style={[styles.container, { backgroundColor: theme.colors.surfaceMuted, borderRadius: theme.radii.sm }]}
      accessible
      accessibilityRole="summary"
      accessibilityLabel={`Recommended match: ${presentation.displayLabel}. ${presentation.safetyStatus.label}.${
        presentation.considerations.length > 0 ? ` ${presentation.considerations.join(" ")}` : ""
      }`}
    >
      <Text style={[theme.typography.label, { color: theme.colors.muted }]}>Recommended match</Text>

      {presentation.brand ? (
        <Text style={[theme.typography.body, { color: theme.colors.foreground, fontWeight: "600" }]}>
          {presentation.brand}
        </Text>
      ) : null}
      <Text style={[theme.typography.body, { color: theme.colors.foreground }]}>
        {presentation.productName ?? presentation.displayLabel}
      </Text>

      <Text style={[theme.typography.caption, { color: statusColor }]}>
        Compatibility: {presentation.safetyStatus.label}
      </Text>

      {presentation.considerations.length > 0 ? (
        <View style={styles.considerations}>
          {presentation.considerations.map((line, index) => (
            <Text key={`${index}-${line}`} style={[theme.typography.caption, { color: theme.colors.muted }]}>
              • {line}
            </Text>
          ))}
        </View>
      ) : null}

      {presentation.verificationProvenance ? (
        <Text style={[theme.typography.caption, { color: theme.colors.muted, fontStyle: "italic" }]}>
          {presentation.verificationProvenance}
        </Text>
      ) : null}
    </View>
  );
}

/** Shown in place of a card when the backend returned no concrete
 * product match for a step (Phase 11's documented fallback behavior --
 * this app never fabricates one client-side). */
export function NoProductMatchNotice() {
  const theme = useTheme();
  return (
    <Text
      style={[theme.typography.caption, { color: theme.colors.muted, fontStyle: "italic" }]}
      accessibilityRole="text"
    >
      No specific product match is available for this step yet.
    </Text>
  );
}

const styles = StyleSheet.create({
  container: { padding: 12, gap: 4, marginTop: 8 },
  considerations: { gap: 2, marginTop: 2 },
});
