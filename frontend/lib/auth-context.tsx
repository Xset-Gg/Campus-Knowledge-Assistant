"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { api, clearToken, getToken, setToken } from "@/lib/api";
import type { User } from "@/types/api";

interface AuthState {
  user: User | null;
  capabilities: string[];
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  can: (capability: string) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const router = useRouter();

  useEffect(() => {
    // Restore the session on mount. The token alone is not trusted — the
    // server re-validates it and returns the authoritative role.
    if (!getToken()) {
      setLoading(false);
      return;
    }
    Promise.all([api.me(), api.capabilities()])
      .then(([me, caps]) => {
        setUser(me);
        setCapabilities(caps.capabilities);
      })
      .catch(() => {
        clearToken();
        setUser(null);
      })
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const result = await api.login(email, password);
    setToken(result.access_token);
    setUser(result.user);
    const caps = await api.capabilities();
    setCapabilities(caps.capabilities);
  }, []);

  const logout = useCallback(() => {
    clearToken();
    setUser(null);
    setCapabilities([]);
    router.push("/login");
  }, [router]);

  const can = useCallback(
    (capability: string) => capabilities.includes(capability),
    [capabilities],
  );

  const value = useMemo(
    () => ({ user, capabilities, loading, login, logout, can }),
    [user, capabilities, loading, login, logout, can],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside an AuthProvider");
  }
  return context;
}
