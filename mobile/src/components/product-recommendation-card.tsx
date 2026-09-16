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
 * Fail-closed requirement (this phase's own invariant, defense in
 * depth on top of the backend's SAFE/RESTRICTED-only guarantee): only
 * a KNOWN safety status (presentation.isKnownStatus, i.e. SAFE or
 * RESTRICTED -- see isKnownSafetyStatus()) is ever rendered as a
 * concrete "Recommended match." Any other value -- UNSAFE,
 * INSUFFICIENT_DATA, or a status this app hasn't been taught about
 * yet -- renders UnconfirmedProductMatchNotice instead, below. This
 * app is not re-deciding safety by branching on the raw status
 * string; it is refusing to *present* a product as recommended when
 * it cannot confirm the status means that.
 *
 * Accessibility: rendered as one accessibility element in reading
 * order (brand/product -> compatibility status -> considerations ->
 * verification), with the compatibility status always carried as
 * text, never color alone.
 */
export function ProductRecommendationCard({ recommendation }: { recommendation: ProductRecommendation }) {
  const theme = useTheme();
  const presentation = presentProductRecommendation(recommendation);

  if (!presentation.isKnownStatus) {
    return <UnconfirmedProductMatchNotice />;
  }

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

/**
 * Mobile V1 Phase C1 fail-closed presentation path: rendered by
 * ProductRecommendationCard instead of a normal card whenever
 * presentation.isKnownStatus is false -- the backend returned a
 * concrete product record, but with a safety_status this app doesn't
 * recognize as SAFE or RESTRICTED. Deliberately does NOT render:
 * brand/product identity, "Recommended match" (or any label implying
 * this app is presenting it as a recommendation), verification
 * provenance, or interpreted restrictions/reasons -- none of those can
 * be shown honestly when the safety meaning of the status itself
 * isn't confirmed. Only the generic notice below, so the routine step
 * this card sits under still reads coherently. Never crashes: this is
 * exactly the "any other status" catch-all, so it must handle
 * anything.
 */
export function UnconfirmedProductMatchNotice() {
  const theme = useTheme();
  return (
    <Text
      style={[theme.typography.caption, { color: theme.colors.muted, fontStyle: "italic" }]}
      accessibilityRole="text"
    >
      Specific product match unavailable. Compatibility for the product returned with this analysis could not be
      confirmed.
    </Text>
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
