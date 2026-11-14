/**
 * Browser entry point: loads the self-hosted IBM Plex fonts (no CDN - the app works offline), the
 * three stylesheets (platform shell, CPARAS Bulk, CPARAS ISLD) and mounts <App/> (routing, theme,
 * authentication).
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/ibm-plex-sans/latin-400.css'
import '@fontsource/ibm-plex-sans/latin-500.css'
import '@fontsource/ibm-plex-sans/latin-600.css'
import '@fontsource/ibm-plex-sans/latin-700.css'
import '@fontsource/ibm-plex-mono/latin-400.css'
import '@fontsource/ibm-plex-mono/latin-500.css'
import './platform/styles/platform.css'
import './gitas/styles/gitas.css'
import './cparas/isld.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
