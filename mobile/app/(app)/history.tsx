import { useRouter } from "expo-router";
import React from "react";
import { FlatList, Pressable, Text, View } from "react-native";

import { describeAnalysisErrorCode, describeAnalysisRequestStatus } from "@/analysis/analysis-presenter";
import { Button } from "@/components/button";
import { ErrorState } from "@/components/error-state";
import { LoadingState } from "@/components/loading-state";
import { Screen } from "@/components/screen";
import { useAnalysisHistoryQuery } from "@/query/use-analysis-history";
import { useTheme } from "@/theme/theme-provider";
import type { AnalysisHistoryItem } from "@/types/domain";

/**
 * Mobile C3 (history): a read-only, paginated list of the user's own
 * past analyses, newest first. Server-authoritative throughout --
 * GET /api/v2/analyses is RLS-scoped to the caller, so this screen
 * never filters/derives ownership client-side, and never renders
 * anything beyond what that response returns. Tapping a row opens the
 * existing analysis detail/result screen -- this screen itself shows
 * only a summary row per analysis, never the full result.
 */
export default function History() {
  const theme = useTheme();
  const router = useRouter();
  const query = useAnalysisHistoryQuery({ enabled: true });

  if (query.isPending) {
    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>History</Text>
        <LoadingState label="Loading your history…" />
      </Screen>
    );
  }

  if (query.isError) {
    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>History</Text>
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      </Screen>
    );
  }

  const items = query.data.pages.flatMap((page) => page.items);

  if (items.length === 0) {
    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>History</Text>
        <View style={{ flex: 1, alignItems: "center", justifyContent: "center", gap: 12, paddingVertical: 32 }}>
          <Text style={[theme.typography.body, { color: theme.colors.muted, textAlign: "center" }]}>
            You haven&apos;t run an analysis yet. Once you do, it will show up here.
          </Text>
          <Button label="Start skin analysis" onPress={() => router.push("/(app)/analysis")} />
        </View>
      </Screen>
    );
  }

  return (
    <Screen scroll={false}>
      <Text style={[theme.typography.title, { color: theme.colors.foreground, paddingTop: 24, paddingHorizontal: theme.spacing.lg }]}>
        History
      </Text>
      <FlatList
        testID="history-list"
        data={items}
        keyExtractor={(item) => item.analysis_id}
        contentContainerStyle={{ paddingHorizontal: theme.spacing.lg, paddingBottom: 24, gap: 8 }}
        renderItem={({ item }) => (
          <HistoryRow item={item} onPress={() => router.push(`/(app)/analysis/${item.analysis_id}`)} />
        )}
        onEndReachedThreshold={0.5}
        onEndReached={() => {
          if (query.hasNextPage && !query.isFetchingNextPage) {
            query.fetchNextPage();
          }
        }}
        ListFooterComponent={
          query.isFetchingNextPage ? (
            <View style={{ paddingVertical: 16 }}>
              <LoadingState label="Loading more…" />
            </View>
          ) : null
        }
      />
    </Screen>
  );
}

function HistoryRow({ item, onPress }: { item: AnalysisHistoryItem; onPress: () => void }) {
  const theme = useTheme();
  const isFailed = item.status === "FAILED";
  const date = new Date(item.created_at);

  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`Analysis from ${Number.isNaN(date.getTime()) ? item.created_at : date.toLocaleDateString()}, ${describeAnalysisRequestStatus(item.status)}`}
      style={({ pressed }) => ({
        padding: 16,
        borderWidth: 1,
        borderRadius: theme.radii.md,
        borderColor: theme.colors.border,
        backgroundColor: theme.colors.surface,
        gap: 4,
        opacity: pressed ? 0.85 : 1,
      })}
    >
      <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
        <Text style={[theme.typography.body, { color: theme.colors.foreground }]}>
          {Number.isNaN(date.getTime()) ? item.created_at : date.toLocaleDateString()}
        </Text>
        <Text
          style={[
            theme.typography.caption,
            { color: isFailed ? theme.colors.danger : theme.colors.muted },
          ]}
        >
          {describeAnalysisRequestStatus(item.status)}
        </Text>
      </View>
      {isFailed ? (
        <Text style={[theme.typography.caption, { color: theme.colors.muted }]}>
          {describeAnalysisErrorCode(item.error_code)}
        </Text>
      ) : null}
    </Pressable>
  );
}
