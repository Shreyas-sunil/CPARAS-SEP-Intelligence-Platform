// The theme is now platform-wide (see platform/theme.tsx). These aliases keep
// the CPARAS Bulk charts/pages' existing imports working.
export {
  ThemeProvider as GitasThemeProvider,
  ForcedTheme as GitasForcedTheme,
  useTheme as useGitasTheme,
} from '../../platform/theme';
export type { Theme as GitasTheme } from '../../platform/theme';
