import * as SecureStore from "expo-secure-store";

/**
 * The one seam between this app's credential storage and the actual
 * platform keychain. Every place that needs to persist a token goes
 * through this interface, never `expo-secure-store` directly -- so
 * unit tests can inject `createInMemorySecureStoreAdapter()` instead
 * of touching a real device keychain (there is no real keychain in a
 * Jest/Node environment anyway). Tests never call
 * `createExpoSecureStoreAdapter()` below -- only production wiring
 * (auth-client-singleton.ts) does.
 */
export type SecureStoreAdapter = {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  deleteItem(key: string): Promise<void>;
};

export function createExpoSecureStoreAdapter(): SecureStoreAdapter {
  return {
    async getItem(key) {
      return SecureStore.getItemAsync(key);
    },
    async setItem(key, value) {
      await SecureStore.setItemAsync(key, value);
    },
    async deleteItem(key) {
      await SecureStore.deleteItemAsync(key);
    },
  };
}

export function createInMemorySecureStoreAdapter(): SecureStoreAdapter {
  const store = new Map<string, string>();
  return {
    async getItem(key) {
      return store.has(key) ? store.get(key)! : null;
    },
    async setItem(key, value) {
      store.set(key, value);
    },
    async deleteItem(key) {
      store.delete(key);
    },
  };
}
