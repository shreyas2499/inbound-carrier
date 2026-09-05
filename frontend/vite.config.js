import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { viteSingleFile } from 'vite-plugin-singlefile'

// Builds to a single self-contained dist/index.html (JS + CSS inlined), so the
// app can be served by any static host — `serve -s dist`, `vite preview`, nginx —
// and previews correctly when opened as a file. The API base is injected at build
// time via VITE_ADAPTER_BASE (the adapter's public URL); empty = same origin.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  server: {
    // Local dev: proxy /otp to a locally-running adapter so there's no CORS in dev.
    proxy: { '/otp': 'http://localhost:8000' }
  }
})
