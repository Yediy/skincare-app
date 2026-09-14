import React from "react";
import { ScrollView, StyleSheet, View, type ViewStyle } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";

import { useTheme } from "@/theme/theme-provider";

type ScreenProps = {
  children: React.ReactNode;
  scroll?: boolean;
  style?: ViewStyle;
};

/** Every route content view renders through this -- consistent safe
 * areas, background, and horizontal padding, scrollable by default so
 * a long form/onboarding screen never needs to re-solve that itself. */
export function Screen({ children, scroll = true, style }: ScreenProps) {
  const theme = useTheme();
  const content = (
    <View style={[styles.content, { paddingHorizontal: theme.spacing.lg }, style]}>{children}</View>
  );

  return (
    <SafeAreaView style={[styles.root, { backgroundColor: theme.colors.background }]} edges={["top", "bottom"]}>
      {scroll ? (
        <ScrollView
          contentInsetAdjustmentBehavior="automatic"
          keyboardShouldPersistTaps="handled"
          contentContainerStyle={styles.scrollContent}
        >
          {content}
        </ScrollView>
      ) : (
        content
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  scrollContent: { flexGrow: 1 },
  content: { flex: 1, paddingVertical: 24, gap: 16 },
});
