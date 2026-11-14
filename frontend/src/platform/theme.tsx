/** Light/dark theme for the whole platform, remembered per browser. */

import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

/** Theme identifiers. */
export type Theme = 'light' | 'dark';

const STORAGE_KEY = 'cparas_theme';
const LEGACY_KEYS = ['gitas_theme', 'sep_theme'];

function readStored(): Theme | null {
  try {
    for (const k of [STORAGE_KEY, ...LEGACY_KEYS]) {
      const v = window.localStorage.getItem(k);
      if (v === 'light' || v === 'dark') return v;
    }
  } catch {
    // storage unavailable (private mode etc.) — fall through to OS preference
  }
  return null;
}

function getInitialTheme(): Theme {
  if (typeof window === 'undefined') return 'light';
  return readStored() ?? (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}

const ThemeContext = createContext<{ theme: Theme; toggleTheme: () => void }>({
  theme: 'light',
  toggleTheme: () => {},
});

/** One theme for the whole platform — both CPARAS Bulk and CPARAS ISLD. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  useEffect(() => {
    try { window.localStorage.setItem(STORAGE_KEY, theme); } catch { /* ignore */ }
  }, [theme]);
  const toggleTheme = () => setTheme(t => (t === 'dark' ? 'light' : 'dark'));
  return <ThemeContext.Provider value={{ theme, toggleTheme }}>{children}</ThemeContext.Provider>;
}

/** Pins a subtree to a fixed theme (used for the off-screen PDF chart snapshots). */
export function ForcedTheme({ theme, children }: { theme: Theme; children: ReactNode }) {
  return <ThemeContext.Provider value={{ theme, toggleTheme: () => {} }}>{children}</ThemeContext.Provider>;
}

/** Current theme and toggle. */
export function useTheme() {
  return useContext(ThemeContext);
}
