import { useLocalSearchParams, useRouter } from "expo-router";
import React from "react";
import { StyleSheet, Text, View } from "react-native";

import {
  deriveAnalysisQualityLabel,
  describeAnalysisErrorCode,
  describeCaptureFailureReason,
  describeCaptureQualityStatus,
  describeMetricStatus,
  formatMetricValueForDisplay,
  isRetakeableError,
  prettifyMetricName,
} from "@/analysis/analysis-presenter";
import { useAnalysisSession } from "@/analysis/analysis-session";
import { useAnalysisPolling } from "@/analysis/use-analysis-polling";
import { Button } from "@/components/button";
import { ErrorState } from "@/components/error-state";
import { LoadingState } from "@/components/loading-state";
import { Screen } from "@/components/screen";
import { useTheme } from "@/theme/theme-provider";
import type { MetricResult, RoutineStep } from "@/types/domain";

/**
 * Handles both "processing" and "result" display for one analysis
 * (section 27 -- folded into a single screen rather than a separate
 * processing.tsx: there is no analysis identity distinct from "this
 * analysisId hasn't completed yet," so a dedicated intermediate route
 * would just duplicate this screen's own polling state). Section
 * 12/13's polling lifecycle lives in
 * src/analysis/use-analysis-polling.ts; this file only renders what
 * that hook reports.
 */
export default function AnalysisResultScreen() {
  const theme = useTheme();
  const router = useRouter();
  const { analysisId } = useLocalSearchParams<{ analysisId: string }>();
  const { startNewAttempt } = useAnalysisSession();
  const polling = useAnalysisPolling(analysisId ?? null);

  const handleAnalyzeAgain = () => {
    startNewAttempt();
    router.replace("/(app)/analysis");
  };

  if (!analysisId) {
    return <ErrorState error={null} />;
  }

  if (polling.phase === "queued" || polling.phase === "processing") {
    return (
      <Screen>
        <LoadingState label={polling.phase === "queued" ? "Waiting to start…" : "Analyzing your photo…"} />
        {polling.isPollingError ? (
          <View style={styles.pollingErrorBanner} accessibilityLiveRegion="polite">
            <Text style={[theme.typography.body, { color: theme.colors.muted, textAlign: "center" }]}>
              We&apos;re having trouble checking the analysis status.
            </Text>
            <Button label="Retry" variant="secondary" onPress={polling.refetch} />
          </View>
        ) : null}
      </Screen>
    );
  }

  if (polling.phase === "failed") {
    const errorCode = polling.data?.error_code ?? null;
    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Analysis didn&apos;t complete</Text>
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>{describeAnalysisErrorCode(errorCode)}</Text>
        {isRetakeableError(errorCode) ? (
          <Button label="Retake photo" onPress={handleAnalyzeAgain} />
        ) : (
          <Button label="Start over" onPress={handleAnalyzeAgain} />
        )}
      </Screen>
    );
  }

  // completed
  const result = polling.data?.result;
  const metrics = polling.data?.metric_results ?? [];
  if (!result) {
    return <LoadingState label="Loading results…" />;
  }

  const qualityLabel = deriveAnalysisQualityLabel(result.capture_assessment, metrics);

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Your analysis</Text>

      <Card theme={theme}>
        <Text style={[theme.typography.subtitle, { color: theme.colors.foreground }]}>Analysis quality</Text>
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>{qualityLabel}</Text>
        <Text style={[theme.typography.caption, { color: theme.colors.muted }]}>
          {describeCaptureQualityStatus(result.capture_assessment.quality_status)}
        </Text>
        {result.capture_assessment.failure_reasons.length > 0 ? (
          <View style={styles.list}>
            {result.capture_assessment.failure_reasons.map((reason) => (
              <Text key={reason} style={[theme.typography.caption, { color: theme.colors.muted }]}>
                • {describeCaptureFailureReason(reason)}
              </Text>
            ))}
          </View>
        ) : null}
      </Card>

      {result.plan.top_priorities.length > 0 ? (
        <Card theme={theme}>
          <Text style={[theme.typography.subtitle, { color: theme.colors.foreground }]}>Top priorities</Text>
          {result.plan.top_priorities.map((priority, index) => (
            <View key={priority.id} style={styles.priorityRow}>
              <Text style={[theme.typography.body, { color: theme.colors.accent }]}>{index + 1}.</Text>
              <View style={{ flex: 1 }}>
                <Text style={[theme.typography.body, { color: theme.colors.foreground }]}>{priority.label}</Text>
                <Text style={[theme.typography.caption, { color: theme.colors.muted }]}>{priority.description}</Text>
              </View>
            </View>
          ))}
        </Card>
      ) : null}

      {metrics.length > 0 ? (
        <Card theme={theme}>
          <Text style={[theme.typography.subtitle, { color: theme.colors.foreground }]}>Measurements</Text>
          {metrics.map((metric) => (
            <MetricRow key={metric.metric_name} metric={metric} theme={theme} />
          ))}
        </Card>
      ) : null}

      {result.plan.am_routine.length > 0 ? (
        <RoutineCard title="Morning routine" steps={result.plan.am_routine} theme={theme} />
      ) : null}
      {result.plan.pm_routine.length > 0 ? (
        <RoutineCard title="Evening routine" steps={result.plan.pm_routine} theme={theme} />
      ) : null}

      {result.plan.disclaimers && result.plan.disclaimers.length > 0 ? (
        <Card theme={theme}>
          {result.plan.disclaimers.map((disclaimer) => (
            <Text key={disclaimer} style={[theme.typography.caption, { color: theme.colors.muted }]}>
              {disclaimer}
            </Text>
          ))}
        </Card>
      ) : null}

      <Button label="Analyze again" onPress={handleAnalyzeAgain} />
    </Screen>
  );
}

function Card({ children, theme }: { children: React.ReactNode; theme: ReturnType<typeof useTheme> }) {
  return (
    <View
      style={[
        styles.card,
        { backgroundColor: theme.colors.surface, borderColor: theme.colors.border, borderRadius: theme.radii.md },
      ]}
    >
      {children}
    </View>
  );
}

function MetricRow({ metric, theme }: { metric: MetricResult; theme: ReturnType<typeof useTheme> }) {
  const displayValue = formatMetricValueForDisplay(metric);
  const isUncertain = metric.status !== "VALID";
  return (
    <View style={styles.metricRow} accessibilityRole="text">
      <Text style={[theme.typography.body, { color: theme.colors.foreground, flex: 1 }]}>
        {prettifyMetricName(metric.metric_name)}
      </Text>
      <View style={{ alignItems: "flex-end" }}>
        <Text
          style={[
            theme.typography.body,
            { color: isUncertain ? theme.colors.muted : theme.colors.foreground, fontStyle: isUncertain ? "italic" : "normal" },
          ]}
        >
          {displayValue ?? describeMetricStatus(metric.status)}
        </Text>
        {metric.status !== "VALID" && displayValue !== null ? (
          <Text style={[theme.typography.caption, { color: theme.colors.muted }]}>{describeMetricStatus(metric.status)}</Text>
        ) : null}
      </View>
    </View>
  );
}

function RoutineCard({ title, steps, theme }: { title: string; steps: RoutineStep[]; theme: ReturnType<typeof useTheme> }) {
  return (
    <Card theme={theme}>
      <Text style={[theme.typography.subtitle, { color: theme.colors.foreground }]}>{title}</Text>
      {steps.map((step) => (
        <View key={step.step_number} style={styles.priorityRow}>
          <Text style={[theme.typography.body, { color: theme.colors.accent }]}>{step.step_number}.</Text>
          <View style={{ flex: 1 }}>
            <Text style={[theme.typography.body, { color: theme.colors.foreground }]}>{step.action}</Text>
            <Text style={[theme.typography.caption, { color: theme.colors.muted }]}>{step.why}</Text>
          </View>
        </View>
      ))}
    </Card>
  );
}

const styles = StyleSheet.create({
  card: { padding: 16, borderWidth: 1, gap: 8 },
  list: { gap: 4, marginTop: 4 },
  priorityRow: { flexDirection: "row", gap: 10 },
  metricRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  pollingErrorBanner: { gap: 8, alignItems: "center", paddingTop: 16 },
});
